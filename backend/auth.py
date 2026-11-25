import time
from datetime import datetime, timedelta
import streamlit as st
from streamlit_cookies_controller import CookieController
import database as db

COOKIE_NAME = "session_id"
SESSION_TTL_DAYS = 30


def _cookies():
    # Se instancia por-llamada a propósito: es lo que espera la librería
    # (cachea sola en st.session_state por dentro), no hay estado propio
    # que mantener acá.
    #
    # Defensa: se observó (reproducible tras un logout) que la librería
    # puede dejar st.session_state['cookies'] en None en vez de un dict --
    # un bug de streamlit-cookies-controller, no de esta app. Una vez que
    # pasa eso queda roto para el resto de la sesión del browser, porque
    # CookieController.__init__ solo vuelve a pedirle las cookies reales al
    # frontend cuando la key TODAVÍA NO está en session_state; si ya está
    # (aunque sea en None), se la cree. Si lo detectamos, la borramos para
    # forzar un fetch fresco en la siguiente instanciación.
    if st.session_state.get("cookies") is None and "cookies" in st.session_state:
        del st.session_state["cookies"]
    return CookieController()


def get_current_user():
    """Devuelve el dict del usuario logueado, o None. Primero mira
    st.session_state -- evita pegarle a la DB en cada rerun de Streamlit
    (que re-ejecuta todo el script ante cualquier interacción) -- y solo
    si no está cae a leer la cookie de sesión, típico de un tab/browser
    nuevo o de un refresh (F5) que resetea el session_state en memoria."""
    if "user" in st.session_state:
        return st.session_state["user"]

    try:
        session_id = _cookies().get(COOKIE_NAME)
    except Exception as e:
        # No dejamos que un problema del componente de cookies (de
        # terceros) tire abajo toda la app -- en el peor caso, se le pide
        # loguearse de nuevo.
        print(f"[WARN] No se pudo leer la cookie de sesión: {e}", flush=True)
        return None
    if not session_id:
        return None

    user = db.get_session_user(session_id)
    if user is None:
        return None

    st.session_state["user"] = user
    st.session_state["_session_id"] = session_id
    return user


def login(username, password):
    """Intenta loguear. Devuelve (user, error_message) -- exactamente uno
    de los dos es no-None."""
    user = db.get_user_by_username(username)
    if user is None or not db.verify_password(password, user["password_hash"]):
        return None, "Usuario o contraseña incorrectos."
    if user["locked"]:
        return None, "Esta cuenta está bloqueada. Contactá al administrador."

    session_id = db.create_session(user["id"], ttl_days=SESSION_TTL_DAYS)
    _cookies().set(
        COOKIE_NAME, session_id,
        expires=datetime.now() + timedelta(days=SESSION_TTL_DAYS),
    )
    # El componente de la cookie manda el 'set' al browser de forma async
    # (monta un iframe nuevo y le pasa el comando). Si se llama a
    # st.rerun() inmediatamente después, es una carrera real: el rerun
    # puede tirar abajo el script antes de que el iframe llegue a cargar y
    # ejecutar el document.cookie = ... (confirmado empíricamente: sin este
    # sleep, la cookie nunca llega a settear en el browser). Es un
    # workaround conocido de esta librería, no algo específico de esta app.
    time.sleep(0.75)
    st.session_state["user"] = user
    st.session_state["_session_id"] = session_id
    return user, None


def logout():
    session_id = st.session_state.get("_session_id")
    if session_id:
        db.delete_session(session_id)
    controller = _cookies()
    if controller.get(COOKIE_NAME) is not None:
        controller.remove(COOKIE_NAME)
    st.session_state.pop("user", None)
    st.session_state.pop("_session_id", None)


def render_change_password_section(user):
    """Sección reusable de 'cambiar mi propia contraseña', usada tanto en
    el modal de opciones del admin como en el de un usuario normal. Pide
    la contraseña actual (no solo la nueva) para que una sesión abierta
    dejada sin atender no alcance para tomar la cuenta."""
    st.subheader("🔑 Cambiar contraseña")
    current_password = st.text_input("Contraseña actual", type="password", key="change_pw_current")
    new_password = st.text_input("Contraseña nueva", type="password", key="change_pw_new")
    if st.button("Cambiar contraseña", use_container_width=True, key="change_pw_submit"):
        if not current_password or not new_password:
            st.error("Completá ambos campos.")
        elif not db.verify_password(current_password, user["password_hash"]):
            st.error("La contraseña actual no es correcta.")
        else:
            db.set_user_password(user["id"], new_password)
            st.success("Contraseña actualizada.")


def render_logout_button():
    if st.button("🚪 Cerrar sesión", use_container_width=True, key="logout_button"):
        logout()
        # A diferencia de las otras acciones dentro de un st.dialog (que a
        # propósito NO llaman a st.rerun() para que el modal no se cierre,
        # ver chat_ui._user_options_dialog), acá SÍ hace falta: logout tiene
        # que efectivamente sacar a la app.py de este dialog/página y
        # volver a evaluar la sesión desde cero para mostrar el login. Sin
        # este rerun explícito, la sesión ya murió del lado del servidor
        # pero la UI se queda en la pantalla actual hasta la próxima
        # interacción del usuario.
        st.rerun()


def render_login_form():
    st.markdown("<div style='height: 8vh'></div>", unsafe_allow_html=True)
    _, col, _ = st.columns([1, 1.2, 1])
    with col:
        st.title("🤖 Qwen Chat")
        with st.form("login_form"):
            username = st.text_input("Usuario")
            password = st.text_input("Contraseña", type="password")
            submitted = st.form_submit_button("Ingresar", use_container_width=True)
        if submitted:
            user, error = login(username.strip(), password)
            if error:
                st.error(error)
            else:
                st.rerun()
