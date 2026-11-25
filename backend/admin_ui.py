import streamlit as st
import auth
import database as db


@st.dialog("⚙️ Opciones de usuario")
def _admin_options_dialog(user):
    # El admin no chatea, así que acá no hay nivel de razonamiento, tools
    # ni recuerdos que configurar -- solo la cuenta.
    auth.render_change_password_section(user)
    st.write("---")
    auth.render_logout_button()


def render(user):
    with st.sidebar:
        if st.button(f"👤 {user['full_name']}", use_container_width=True):
            _admin_options_dialog(user)

    st.title("🛠️ Consola de administración")

    tab_users, tab_models = st.tabs(["👥 Usuarios", "🧠 Modelos"])
    with tab_users:
        _render_users_tab()
    with tab_models:
        _render_models_tab()


# ---------------------------------------------------------------------------
# Tab Usuarios
# ---------------------------------------------------------------------------

def _render_users_tab():
    if st.session_state.get("_new_password_info"):
        info = st.session_state["_new_password_info"]
        with st.container(border=True):
            st.success(
                f"Usuario **{info['username']}** creado — contraseña: `{info['password']}`\n\n"
                "⚠️ Copiala ahora, no se va a volver a mostrar."
            )
            if st.button("Entendido, cerrar aviso"):
                del st.session_state["_new_password_info"]
                st.rerun()

    all_models = db.list_models()
    users = [u for u in db.list_users() if not u["is_admin"]]

    st.subheader("Usuarios existentes")
    if not users:
        st.caption("Todavía no hay usuarios creados.")
    for u in users:
        with st.expander(f"{'🔒 ' if u['locked'] else ''}{u['full_name']} ({u['username']})"):
            _render_edit_user_form(u, all_models)

    st.write("---")
    st.subheader("Crear usuario")
    if not all_models:
        st.caption("No hay modelos dados de alta todavía — podés crear el usuario igual y habilitarle modelos después.")
    with st.form("create_user_form", clear_on_submit=True):
        username = st.text_input("Usuario")
        full_name = st.text_input("Nombre completo")
        password = st.text_input("Contraseña inicial (vacío = se genera una automática)", type="password")
        locked = st.checkbox("Bloqueado", value=False)
        st.caption("Modelos habilitados:")
        selected_model_ids = []
        for m in all_models:
            if st.checkbox(m["label"], key=f"create_model_{m['id']}"):
                selected_model_ids.append(m["id"])
        submitted = st.form_submit_button("➕ Crear usuario")

    if submitted:
        username = username.strip()
        full_name = full_name.strip()
        if not username or not full_name:
            st.error("Usuario y nombre completo son obligatorios.")
        elif db.get_user_by_username(username):
            st.error("Ya existe un usuario con ese nombre.")
        else:
            final_password = password if password else db.generate_password()
            user_id = db.create_user(username, full_name, final_password, locked)
            db.set_user_model_access(user_id, selected_model_ids)
            st.session_state["_new_password_info"] = {"username": username, "password": final_password}
            st.rerun()


def _render_edit_user_form(u, all_models):
    current_ids = db.get_user_model_ids(u["id"])
    with st.form(f"edit_user_{u['id']}"):
        full_name = st.text_input("Nombre completo", value=u["full_name"], key=f"fn_{u['id']}")
        locked = st.checkbox("Bloqueado", value=u["locked"], key=f"lk_{u['id']}")
        new_password = st.text_input(
            "Nueva contraseña (vacío = no cambiar)", type="password", key=f"pw_{u['id']}"
        )
        st.caption("Modelos habilitados:")
        selected_model_ids = []
        for m in all_models:
            checked = st.checkbox(
                m["label"], value=m["id"] in current_ids, key=f"edit_model_{u['id']}_{m['id']}"
            )
            if checked:
                selected_model_ids.append(m["id"])

        confirm_delete = st.checkbox(
            "Confirmar: quiero eliminar este usuario y todos sus datos (chats, recuerdos)",
            key=f"confirm_del_{u['id']}",
        )
        col_save, col_del = st.columns(2)
        save = col_save.form_submit_button("💾 Guardar cambios", use_container_width=True)
        delete = col_del.form_submit_button("🗑️ Eliminar usuario", use_container_width=True)

    if save:
        db.update_user(u["id"], full_name.strip(), locked)
        db.set_user_model_access(u["id"], selected_model_ids)
        if new_password:
            db.set_user_password(u["id"], new_password)
        st.success("Guardado.")
        st.rerun()

    if delete:
        if not confirm_delete:
            st.error("Marcá la confirmación antes de eliminar.")
        else:
            db.delete_user(u["id"])
            st.rerun()


# ---------------------------------------------------------------------------
# Tab Modelos
# ---------------------------------------------------------------------------

def _render_models_tab():
    st.subheader("Modelos existentes")
    models = db.list_models()
    if not models:
        st.caption("Todavía no hay modelos dados de alta.")
    if sum(1 for m in models if m["is_convenience"]) > 1:
        st.caption("⚠️ Hay más de un modelo marcado como 'convenience' — se usa el más antiguo.")
    for m in models:
        badge = " · 🧩 convenience" if m["is_convenience"] else ""
        with st.expander(f"{m['label']}{badge} — `{m['model_name']}`"):
            _render_edit_model_form(m)

    st.write("---")
    st.subheader("Dar de alta un modelo")
    with st.form("create_model_form", clear_on_submit=True):
        label = st.text_input("Nombre (lo que ve el usuario en el selector)")
        model_name = st.text_input("model_name (el id real que se manda a la API)")
        base_url = st.text_input("URL base", placeholder="http://llm_server:8080/v1")
        ctx_size = st.number_input("Context size", min_value=1024, step=1024, value=32768)
        system_prompt = st.text_area("System prompt", value="Respondé siempre en el idioma del usuario.")
        temperature = st.slider("Temperature", 0.0, 1.5, 0.7, 0.05)
        max_tokens = st.slider("Máx. tokens de respuesta", 64, 8192, 512, 64)
        is_convenience = st.checkbox(
            "Este modelo es para tareas de convenience de la app (titular chats, sugerir recuerdos)"
        )
        submitted = st.form_submit_button("➕ Crear modelo")

    if submitted:
        label, model_name, base_url = label.strip(), model_name.strip(), base_url.strip()
        if not label or not model_name or not base_url:
            st.error("Nombre, model_name y URL base son obligatorios.")
        else:
            db.create_model(label, model_name, base_url, int(ctx_size), system_prompt, temperature, int(max_tokens), is_convenience)
            st.rerun()


def _render_edit_model_form(m):
    with st.form(f"edit_model_{m['id']}"):
        label = st.text_input("Nombre", value=m["label"], key=f"ml_{m['id']}")
        model_name = st.text_input("model_name", value=m["model_name"], key=f"mn_{m['id']}")
        base_url = st.text_input("URL base", value=m["base_url"], key=f"mu_{m['id']}")
        ctx_size = st.number_input("Context size", min_value=1024, step=1024, value=m["ctx_size"], key=f"mc_{m['id']}")
        system_prompt = st.text_area("System prompt", value=m["system_prompt"], key=f"msp_{m['id']}")
        temperature = st.slider("Temperature", 0.0, 1.5, float(m["temperature"]), 0.05, key=f"mt_{m['id']}")
        max_tokens = st.slider("Máx. tokens de respuesta", 64, 8192, m["max_tokens"], 64, key=f"mmt_{m['id']}")
        is_convenience = st.checkbox(
            "Este modelo es para tareas de convenience de la app", value=m["is_convenience"], key=f"mcv_{m['id']}"
        )
        confirm_delete = st.checkbox("Confirmar: quiero eliminar este modelo", key=f"mcd_{m['id']}")
        col_save, col_del = st.columns(2)
        save = col_save.form_submit_button("💾 Guardar cambios", use_container_width=True)
        delete = col_del.form_submit_button("🗑️ Eliminar modelo", use_container_width=True)

    if save:
        db.update_model(
            m["id"], label.strip(), model_name.strip(), base_url.strip(),
            int(ctx_size), system_prompt, temperature, int(max_tokens), is_convenience,
        )
        st.success("Guardado.")
        st.rerun()

    if delete:
        if not confirm_delete:
            st.error("Marcá la confirmación antes de eliminar.")
        else:
            db.delete_model(m["id"])
            st.rerun()
