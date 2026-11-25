import base64
import io
import json
import socket
import ipaddress
from urllib.parse import urlparse
import requests
import streamlit as st
from openai import OpenAI
from pypdf import PdfReader
from pdf2image import convert_from_bytes
from docx import Document as DocxDocument
from ddgs import DDGS
import trafilatura
import auth
import convenience
import database as db

# Cuántas páginas de un PDF se rasterizan como imagen (además de extraerle
# el texto completo, que no tiene este límite). Cada página-imagen consume
# bastantes tokens de visión; 5 es un tope conservador para no disparar el
# costo en PDFs largos. El texto completo igual se manda siempre entero.
PDF_MAX_PAGES_AS_IMAGES = 5

WEB_SEARCH_MAX_RESULTS = 5
WEB_PAGE_MAX_CHARS = 8000  # tope de texto por página leída, para no comerse todo el contexto con un solo artículo

REASONING_INSTRUCTIONS = {
    "low": "Respondé de forma directa y concisa, sin explicar tu razonamiento.",
    "medium": "Respondé de forma clara, con el nivel de detalle que la pregunta merezca.",
    "high": "Antes de responder, pensá el problema paso a paso: considerá distintas interpretaciones, posibles matices o contradicciones, y razoná internamente antes de dar tu respuesta final. No muestres ese razonamiento intermedio, solo la conclusión.",
}

# --- Compresión de contexto (inspirada en el algoritmo real de Qwen
# Code/Gemini CLI, no en "borrar los primeros N mensajes" que ya
# descartamos por agresivo) ---
# Umbral (fracción del ctx_size del modelo activo) a partir del cual se
# dispara la compresión automática antes del próximo request.
COMPRESSION_TRIGGER_RATIO = 0.7
# Fracción de los mensajes MÁS RECIENTES que se preservan textuales, sin
# resumir. El resto ("la cabeza" vieja) se resume vía LLM, no se borra.
COMPRESSION_PRESERVE_RATIO = 0.3


def _image_to_content_block(file_bytes, mime_type):
    """Codifica una imagen a base64 y arma el content block que espera la
    API de visión (formato OpenAI-compatible que sirve llama-server con
    --mmproj)."""
    b64 = base64.b64encode(file_bytes).decode("utf-8")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{b64}"},
    }


def _process_pdf(file_bytes, filename):
    """Devuelve (texto_extraido, content_blocks_de_imagenes) para un PDF.
    Siempre extrae el texto completo (barato, confiable). Además rasteriza
    hasta PDF_MAX_PAGES_AS_IMAGES páginas como imágenes para que el modelo
    también pueda "ver" diagramas/tablas/contenido visual, no solo leer
    texto plano."""
    reader = PdfReader(io.BytesIO(file_bytes))
    extracted_text = "\n\n".join(page.extract_text() or "" for page in reader.pages)

    image_blocks = []
    try:
        pages_as_images = convert_from_bytes(file_bytes, dpi=150)
        total_pages = len(pages_as_images)
        for page_img in pages_as_images[:PDF_MAX_PAGES_AS_IMAGES]:
            buf = io.BytesIO()
            page_img.save(buf, format="JPEG", quality=85)
            image_blocks.append(_image_to_content_block(buf.getvalue(), "image/jpeg"))
    except Exception as e:
        # Si poppler/pdf2image falla por lo que sea, no tiramos toda la
        # request abajo: nos quedamos solo con el texto extraído.
        print(f"[WARN] No se pudo rasterizar '{filename}' a imágenes: {e}", flush=True)
        total_pages = len(reader.pages)

    note = f"[PDF adjunto: {filename}, {len(reader.pages)} página(s)"
    if len(image_blocks) < total_pages:
        note += f" — se muestran las primeras {len(image_blocks)} como imagen, texto completo incluido"
    note += "]"

    return extracted_text, image_blocks, note


def _extract_txt(file_bytes):
    """Decodifica un .txt a string. Con errors='replace' para no reventar
    si el archivo no es UTF-8 estricto (ej. Windows-1252 con tildes)."""
    return file_bytes.decode("utf-8", errors="replace")


def _extract_docx(file_bytes):
    """Extrae texto de un .docx: párrafos en orden + texto de tablas."""
    doc = DocxDocument(io.BytesIO(file_bytes))
    parts = [p.text for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            row_text = " | ".join(cell.text.strip() for cell in row.cells)
            if row_text.strip(" |"):
                parts.append(row_text)
    return "\n".join(parts)


def _compress_history(llm, model_name, messages_db, existing_summary):
    """Resume la parte vieja ('cabeza') de una conversación larga, en vez
    de simplemente descartarla. Inspirado en el algoritmo real de Qwen
    Code/Gemini CLI (ChatCompressionService): se preserva el último
    COMPRESSION_PRESERVE_RATIO de los mensajes tal cual, y se le pide al
    propio modelo que resuma todo lo anterior a eso. Si ya había un
    resumen previo (de una compresión anterior en esta misma charla), se
    lo incluye como parte de lo que hay que resumir, para no perder lo ya
    condensado.

    Devuelve (nuevo_resumen, mensajes_tail_preservados).
    """
    split_idx = int(len(messages_db) * (1 - COMPRESSION_PRESERVE_RATIO))
    # Alineamos el corte a un mensaje "user", igual que findCompressSplitPoint
    # en el original — evita cortar justo después de la pregunta del
    # usuario, dejando la respuesta del modelo huérfana de su contexto.
    while split_idx > 0 and split_idx < len(messages_db) and messages_db[split_idx]["role"] != "user":
        split_idx -= 1

    head = messages_db[:split_idx]
    tail = messages_db[split_idx:]

    if not head:
        return existing_summary, tail

    head_text = "\n\n".join(f"{m['role']}: {m['content']}" for m in head)
    if existing_summary:
        head_text = f"[Resumen previo de mensajes aún más viejos]\n{existing_summary}\n\n---\n\n{head_text}"

    summarization_prompt = (
        "Resumí la siguiente conversación de forma concisa pero preservando "
        "toda la información importante: hechos concretos, nombres, decisiones "
        "tomadas, preguntas sin responder, y cualquier dato que haga falta para "
        "poder continuar la charla con naturalidad. No agregues opiniones ni "
        "comentarios tuyos, solo el resumen.\n\n"
        f"{head_text}"
    )

    response = llm.chat.completions.create(
        model=model_name,
        messages=[{"role": "user", "content": summarization_prompt}],
        max_tokens=1024,
        temperature=0.3,
    )
    new_summary = (response.choices[0].message.content or "").strip()
    return new_summary, tail


def _is_safe_url(url):
    """Bloquea esquemas que no sean http/https y cualquier host que
    resuelva a una IP privada/loopback/link-local — evita que el modelo
    (por accidente o por un prompt injection leído de una página externa)
    pueda usar esta tool para pegarle a servicios internos de tu propia
    red de Docker (db, llm_server, metadata de cloud, etc.)."""
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        if hostname.lower() in ("localhost", "db", "llm_server", "app", "openhands"):
            return False
        ip = socket.gethostbyname(hostname)
        ip_obj = ipaddress.ip_address(ip)
        if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local or ip_obj.is_reserved:
            return False
        return True
    except Exception:
        return False


def _web_search(query):
    """Búsqueda de texto vía DuckDuckGo (no requiere API key ni login)."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=WEB_SEARCH_MAX_RESULTS))
    except Exception as e:
        return f"Error al buscar en internet: {e}"

    if not results:
        return "No se encontraron resultados para esa búsqueda."

    lines = []
    for r in results:
        lines.append(f"- {r.get('title', '(sin título)')}\n  URL: {r.get('href', '')}\n  {r.get('body', '')}")
    return "\n\n".join(lines)


def _read_webpage(url):
    """Descarga una URL pública y extrae el contenido principal (sin
    menús/publicidad/boilerplate) vía trafilatura."""
    if not _is_safe_url(url):
        return (
            "URL no permitida: solo se admiten direcciones públicas http/https. "
            "No se puede acceder a direcciones internas, privadas o localhost."
        )
    try:
        resp = requests.get(
            url,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0 (compatible; QwenChatBot/1.0)"},
        )
        resp.raise_for_status()
    except Exception as e:
        return f"Error al descargar la página: {e}"

    extracted = trafilatura.extract(resp.text, url=url) or ""
    if not extracted.strip():
        return "No se pudo extraer contenido legible de esa página (puede requerir JavaScript o login)."

    if len(extracted) > WEB_PAGE_MAX_CHARS:
        extracted = extracted[:WEB_PAGE_MAX_CHARS] + "\n\n[...contenido truncado por longitud...]"
    return extracted


INTERNET_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "buscar_en_internet",
            "description": (
                "Busca en internet (vía DuckDuckGo) y devuelve una lista de resultados "
                "con título, URL y un breve resumen de cada uno. Usala cuando necesites "
                "información actual o que no tengas en tu conocimiento. Después podés "
                "usar leer_pagina_web con la URL de un resultado si necesitás el "
                "contenido completo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Términos de búsqueda."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "leer_pagina_web",
            "description": (
                "Descarga una URL pública (http/https) y devuelve el texto principal "
                "del artículo o página, sin menús ni publicidad. Solo funciona con "
                "páginas públicas, sin login. El contenido puede truncarse si es muy largo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL completa a leer, incluyendo http:// o https://."}
                },
                "required": ["url"],
            },
        },
    },
]

# Tool que le permite al modelo "volver a pedir" el contenido completo de
# un archivo adjuntado en un turno anterior de la misma conversación, sin
# que el usuario tenga que resubirlo. Solo se ofrece cuando la conversación
# tiene al menos un adjunto de texto (ver uso más abajo).
ATTACHMENT_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "leer_archivo_adjunto",
            "description": (
                "Devuelve el texto completo de un archivo (PDF, TXT o DOCX) que el "
                "usuario adjuntó anteriormente en esta conversación. Usala cuando "
                "necesites datos concretos de ese archivo que ya no tenés a mano en "
                "el contexto actual — no asumas ni inventes su contenido de memoria. "
                "El resultado puede ser largo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "nombre_archivo": {
                        "type": "string",
                        "description": "Nombre exacto del archivo, tal como aparece en la lista de adjuntos disponibles.",
                    }
                },
                "required": ["nombre_archivo"],
            },
        },
    }
]


@st.cache_resource
def _get_llm_client(base_url):
    # El modelo ya NO se carga en este proceso: vive en el servicio
    # llm_server correspondiente (llama-server), potencialmente compartido
    # también con OpenHands. Acá solo armamos un cliente HTTP
    # OpenAI-compatible. cache_resource cachea por valor de base_url, así
    # que cambiar de modelo no reconstruye el cliente ya usado antes.
    return OpenAI(base_url=base_url, api_key="no-key")


@st.dialog("⚙️ Opciones de usuario")
def _user_options_dialog(user):
    settings = db.get_user_settings(user["id"])

    st.subheader("Nivel de razonamiento")
    reasoning_level = st.select_slider(
        "Nivel de Razonamiento:",
        options=["low", "medium", "high"],
        value=settings["reasoning_level"],
        key="dialog_reasoning_level",
        help="'low': respuestas directas y cortas. 'medium': balance normal. 'high': el modelo razona internamente con más cuidado antes de responder.",
    )

    st.subheader("Herramientas")
    web_tools_enabled = st.checkbox(
        "🌐 Permitir acceso a internet (búsqueda + lectura de páginas)",
        value=settings["web_tools_enabled"],
        key="dialog_web_tools",
    )

    if st.button("💾 Guardar preferencias", use_container_width=True):
        db.save_user_settings(user["id"], reasoning_level, web_tools_enabled)
        st.success("Guardado.")

    st.write("---")
    st.subheader("🧠 Recuerdos")
    st.caption("Cosas que el asistente sabe sobre vos y usa para personalizar sus respuestas.")
    memories = db.list_memories(user["id"])
    if not memories:
        st.caption("Todavía no hay recuerdos guardados.")
    for mem in memories:
        col_text, col_del = st.columns([0.85, 0.15])
        col_text.write(f"- {mem['content']}")
        if col_del.button("🗑️", key=f"del_mem_{mem['id']}"):
            db.delete_memory(mem["id"], user["id"])
            # Nota: sin st.rerun() a propósito -- el propio click ya
            # dispara un rerun natural de Streamlit. Un st.rerun() EXPLÍCITO
            # acá cierra el diálogo (comportamiento observado en Streamlit
            # 1.63, no documentado): st.dialog solo sobrevive a reruns
            # "naturales" disparados por una interacción, no a los forzados.
    with st.form("add_memory_form", clear_on_submit=True):
        new_memory = st.text_input("Nuevo recuerdo", label_visibility="collapsed", placeholder="Agregar un recuerdo manualmente...")
        if st.form_submit_button("➕ Agregar"):
            if new_memory.strip():
                db.add_memory(user["id"], new_memory.strip())

    st.write("---")
    auth.render_change_password_section(user)

    st.write("---")
    auth.render_logout_button()


def render(user):
    st.session_state.setdefault("current_conv", None)

    models_for_user = db.get_models_for_user(user["id"])

    with st.sidebar:
        st.title("🤖 Qwen Chat")

        if st.button("➕ Nuevo Chat", use_container_width=True, disabled=not models_for_user):
            new_id = db.create_conversation(user["id"])
            st.session_state.current_conv = new_id
            st.rerun()

        st.write("---")

        if models_for_user:
            if "active_model_id" not in st.session_state:
                st.session_state.active_model_id = db.get_user_settings(user["id"])["active_model_id"]
            valid_ids = [m["id"] for m in models_for_user]
            if st.session_state.active_model_id not in valid_ids:
                st.session_state.active_model_id = valid_ids[0]

            selected_id = st.selectbox(
                "Modelo",
                options=valid_ids,
                index=valid_ids.index(st.session_state.active_model_id),
                format_func=lambda mid: next(m["label"] for m in models_for_user if m["id"] == mid),
            )
            if selected_id != st.session_state.active_model_id:
                st.session_state.active_model_id = selected_id
                db.set_active_model(user["id"], selected_id)
                st.rerun()

        st.write("---")
        st.subheader("Historial de conversaciones")

        conversations = db.get_conversations(user["id"])
        for conv in conversations:
            col1, col2 = st.columns([0.8, 0.2])
            button_type = "primary" if st.session_state.current_conv == conv["id"] else "secondary"

            if col1.button(f"💬 {conv['title']}", key=f"select_{conv['id']}", type=button_type, use_container_width=True):
                st.session_state.current_conv = conv["id"]
                st.rerun()

            if col2.button("🗑️", key=f"del_{conv['id']}"):
                db.delete_conversation(conv["id"], user["id"])
                if st.session_state.current_conv == conv["id"]:
                    st.session_state.current_conv = None
                st.rerun()

        st.write("---")
        if st.button(f"👤 {user['full_name']}", use_container_width=True):
            _user_options_dialog(user)

    if not models_for_user:
        st.info("Tu usuario no tiene modelos habilitados todavía. Contactá al administrador.")
        return

    active_model = next(m for m in models_for_user if m["id"] == st.session_state.active_model_id)

    # Defensa en profundidad: si current_conv quedó apuntando a una
    # conversación que ya no existe o no es del usuario logueado (borrada
    # por otra sesión, o un id corrupto), la descartamos.
    current_conv = st.session_state.current_conv
    if current_conv and not db.get_conversation(current_conv, user["id"]):
        current_conv = None
        st.session_state.current_conv = None

    if not current_conv:
        st.info("Seleccioná un chat de la barra lateral o creá uno nuevo para empezar.")
        return

    messages_db = db.get_messages(current_conv)
    user_settings = db.get_user_settings(user["id"])

    # Reservamos este container ANTES del form: Streamlit respeta la posición
    # en el layout en la que un container fue declarado, sin importar en qué
    # momento de la ejecución del script se escribe contenido dentro de él.
    # Así, tanto el historial como el mensaje nuevo (usuario + respuesta del
    # modelo) quedan siempre arriba del form, aunque el código que los pinta
    # corra más abajo.
    chat_container = st.container()

    with chat_container:
        for msg in messages_db:
            with st.chat_message(msg["role"]):
                st.write(msg["content"])

    # Usamos un st.form + st.text_area en vez de st.chat_input: este último
    # está pensado para mensajes cortos de una línea y no es cómodo (ni
    # confiable) para pegar bloques de texto largos. El text_area no tiene
    # límite práctico de caracteres y crece verticalmente. clear_on_submit
    # vacía el campo después de enviar, imitando el comportamiento del
    # chat_input original.
    with st.form(key="prompt_form", clear_on_submit=True):
        prompt = st.text_area(
            "Escribí tu mensaje acá...",
            key="prompt_input",
            height=150,
            label_visibility="collapsed",
            placeholder="Escribí tu mensaje acá... (podés pegar textos largos sin problema)",
        )
        uploaded_files = st.file_uploader(
            "Adjuntar archivos",
            type=["jpg", "jpeg", "png", "pdf", "txt", "docx"],
            accept_multiple_files=True,
            label_visibility="collapsed",
            help="JPG/PNG: el modelo los analiza directamente (requiere --mmproj en llm_server). "
            "PDF: se le extrae el texto completo + se le mandan como imagen las primeras "
            f"{PDF_MAX_PAGES_AS_IMAGES} páginas. TXT/DOCX: se les extrae el texto completo.",
        )
        col_send, _ = st.columns([0.15, 0.85])
        with col_send:
            submitted = st.form_submit_button("Enviar ➤", use_container_width=True)

    if submitted and ((prompt and prompt.strip()) or uploaded_files):
        # --- Procesamiento de adjuntos ---
        # Nota importante de diseño: lo que se GUARDA en la base (y se
        # vuelve a mandar en turnos futuros como "historial") es solo texto
        # (el prompt + notas descriptivas de los adjuntos). Las imágenes
        # (tanto JPG/PNG directos como páginas de PDF rasterizadas) se usan
        # SOLO para este request puntual al modelo, no se persisten ni se
        # re-envían en turnos siguientes — si no, cada turno nuevo volvería
        # a mandar todas las imágenes de toda la conversación, disparando
        # el consumo de tokens de visión sin necesidad.
        image_blocks = []
        attachment_notes = []
        extra_text_parts = []

        for f in uploaded_files or []:
            file_bytes = f.getvalue()
            name_lower = f.name.lower()
            if f.type in ("image/jpeg", "image/png"):
                image_blocks.append(_image_to_content_block(file_bytes, f.type))
                attachment_notes.append(f"[Imagen adjunta: {f.name}]")
            elif f.type == "application/pdf":
                pdf_text, pdf_image_blocks, pdf_note = _process_pdf(file_bytes, f.name)
                image_blocks.extend(pdf_image_blocks)
                attachment_notes.append(pdf_note)
                if pdf_text.strip():
                    extra_text_parts.append(f"--- Contenido de {f.name} ---\n{pdf_text}")
                    db.save_attachment(current_conv, f.name, pdf_text)
            elif name_lower.endswith(".txt"):
                txt_text = _extract_txt(file_bytes)
                attachment_notes.append(f"[Archivo de texto adjunto: {f.name}]")
                if txt_text.strip():
                    extra_text_parts.append(f"--- Contenido de {f.name} ---\n{txt_text}")
                    db.save_attachment(current_conv, f.name, txt_text)
            elif name_lower.endswith(".docx"):
                docx_text = _extract_docx(file_bytes)
                attachment_notes.append(f"[Documento Word adjunto: {f.name}]")
                if docx_text.strip():
                    extra_text_parts.append(f"--- Contenido de {f.name} ---\n{docx_text}")
                    db.save_attachment(current_conv, f.name, docx_text)

        prompt_text = (prompt or "").strip()
        display_text = "\n".join([prompt_text] + attachment_notes) if attachment_notes else prompt_text

        with chat_container:
            with st.chat_message("user"):
                st.write(display_text)
                for f in uploaded_files or []:
                    if f.type in ("image/jpeg", "image/png"):
                        st.image(f.getvalue(), width=200)

        is_first_message = db.count_messages(current_conv) == 0
        db.save_message(current_conv, "user", display_text)

        if is_first_message:
            title = convenience.maybe_generate_title(prompt_text) if prompt_text else None
            if not title:
                title = display_text[:30] + "..." if len(display_text) > 30 else display_text
            db.update_conversation_title(current_conv, title)

        llm = _get_llm_client(active_model["base_url"])
        model_name = active_model["model_name"]
        active_max_tokens = active_model["max_tokens"]
        active_temperature = active_model["temperature"]
        ctx_size = active_model["ctx_size"]

        reasoning_instruction = REASONING_INSTRUCTIONS[user_settings["reasoning_level"]]

        # --- Compresión de contexto ---
        # Se dispara ANTES de armar el request, si la última medición real
        # de prompt_tokens (guardada después del turno anterior) ya superó
        # COMPRESSION_TRIGGER_RATIO del ctx_size del modelo activo. No
        # borra nada de la base — solo arma una versión resumida de la
        # "cabeza" vieja de la charla para ESTE request, guardando el
        # resumen en session_state para reusarlo en turnos futuros sin
        # tener que volver a resumir cada vez.
        conv_id = current_conv
        st.session_state.setdefault("last_prompt_tokens", {})
        st.session_state.setdefault("compression_cache", {})

        last_known_tokens = st.session_state["last_prompt_tokens"].get(conv_id, 0)
        cache_entry = st.session_state["compression_cache"].get(conv_id)
        existing_summary = cache_entry["summary"] if cache_entry else None

        effective_messages_db = messages_db
        if cache_entry:
            # Ya hay un resumen de una compresión anterior: solo mandamos
            # los mensajes posteriores a lo que ese resumen ya cubre.
            effective_messages_db = messages_db[cache_entry["covered_count"]:]

        if last_known_tokens >= COMPRESSION_TRIGGER_RATIO * ctx_size and len(effective_messages_db) > 1:
            with chat_container:
                st.info(
                    f"🗜️ Esta conversación superó el {COMPRESSION_TRIGGER_RATIO*100:.0f}% del contexto "
                    f"de {active_model['label']} (~{last_known_tokens:,} tokens). Resumiendo los "
                    "mensajes más viejos antes de continuar — no se borra nada de la base, solo se "
                    "resume lo que se le manda al modelo."
                )
            new_summary, tail = _compress_history(llm, model_name, effective_messages_db, existing_summary)
            covered_count = len(messages_db) - len(tail)
            st.session_state["compression_cache"][conv_id] = {
                "summary": new_summary,
                "covered_count": covered_count,
            }
            existing_summary = new_summary
            effective_messages_db = tail

        # Adjuntos de texto disponibles en ESTA conversación (incluye el que
        # se acaba de subir en este mismo turno, si lo hay, porque ya se
        # guardó en la base un poco más arriba). Solo si hay al menos uno
        # habilitamos la tool de "leer_archivo_adjunto" — así no se le suma
        # overhead de tools a conversaciones que nunca adjuntaron nada.
        attachment_filenames = db.get_attachment_filenames(current_conv)

        system_content = f"{reasoning_instruction} {active_model['system_prompt']}"

        memories = db.list_memories(user["id"])
        if memories:
            memory_lines = "\n".join(f"- {m['content']}" for m in memories)
            system_content += (
                "\n\nCosas que sabés sobre el usuario (de conversaciones anteriores, "
                "usalas para personalizar tu respuesta si son relevantes):\n"
                f"{memory_lines}"
            )

        if existing_summary:
            system_content += (
                "\n\nResumen de mensajes anteriores de esta conversación (más viejos, "
                "condensados para no exceder el contexto):\n"
                f"{existing_summary}"
            )
        if attachment_filenames:
            file_list = "\n".join(f"- {name}" for name in attachment_filenames)
            system_content += (
                "\n\nTenés adjuntos disponibles en esta conversación (subidos en "
                "turnos anteriores, su contenido ya NO está en tu contexto actual):\n"
                f"{file_list}\n\n"
                "Si necesitás datos concretos de alguno para responder con precisión, "
                "usá la herramienta leer_archivo_adjunto en vez de asumir o inventar "
                "su contenido de memoria."
            )

        messages_for_template = [{"role": "system", "content": system_content}]
        for msg in effective_messages_db:
            messages_for_template.append({"role": msg["role"], "content": msg["content"]})

        # El texto que ve el modelo en ESTE turno incluye, además del
        # prompt, el texto extraído de cualquier PDF adjunto (siempre
        # completo, sin límite de páginas).
        model_text = "\n\n".join([prompt_text] + extra_text_parts) if extra_text_parts else prompt_text

        if image_blocks:
            # Con imágenes de por medio, el content del mensaje pasa a ser
            # una lista de content blocks (formato multimodal OpenAI-
            # compatible), no un string plano.
            user_content = [{"type": "text", "text": model_text}] + image_blocks
        else:
            user_content = model_text

        messages_for_template.append({"role": "user", "content": user_content})

        with chat_container, st.chat_message("assistant"):
            response_placeholder = st.empty()
            response_placeholder.markdown("*Pensando...*")

            # --- Tool calling (adjuntos + internet) ---
            # Se combinan las tools que correspondan según el estado de
            # esta conversación/configuración. Hacemos una llamada NO
            # streameada primero, para poder inspeccionar si el modelo
            # pide alguna herramienta antes de responder. Si no pide nada,
            # esa misma respuesta YA es la final (se pierde el streaming
            # token-por-token para ese mensaje puntual, trade-off aceptado
            # a cambio de no duplicar el costo de generación). Si pide
            # algo, se lo resolvemos y recién ahí se hace el request final,
            # que sí puede streamear normalmente.
            need_streaming_call = True
            response_text = ""
            reasoning_text = ""
            finish_reason = None
            usage = None

            active_tools = []
            if attachment_filenames:
                active_tools += ATTACHMENT_TOOLS
            if user_settings["web_tools_enabled"]:
                active_tools += INTERNET_TOOLS

            if active_tools:
                MAX_TOOL_ROUNDS = 4  # tope de seguridad ante un modelo que insista en pedir tools en loop
                for _ in range(MAX_TOOL_ROUNDS):
                    preflight = llm.chat.completions.create(
                        model=model_name,
                        messages=messages_for_template,
                        max_tokens=active_max_tokens,
                        temperature=active_temperature,
                        tools=active_tools,
                        tool_choice="auto",
                    )
                    preflight_message = preflight.choices[0].message

                    if not preflight_message.tool_calls:
                        # No pidió ninguna herramienta: esta ES la respuesta final.
                        response_text = (preflight_message.content or "").strip()
                        reasoning_text = getattr(preflight_message, "reasoning_content", "") or ""
                        finish_reason = preflight.choices[0].finish_reason
                        usage = preflight.usage
                        need_streaming_call = False
                        break

                    # Pidió una o más herramientas: las ejecutamos y le
                    # devolvemos el resultado para que pueda seguir.
                    messages_for_template.append({
                        "role": "assistant",
                        "content": preflight_message.content,
                        "tool_calls": [tc.model_dump() for tc in preflight_message.tool_calls],
                    })
                    for tc in preflight_message.tool_calls:
                        try:
                            args = json.loads(tc.function.arguments)
                        except (ValueError, TypeError):
                            args = {}

                        tool_name = tc.function.name
                        if tool_name == "leer_archivo_adjunto":
                            nombre = args.get("nombre_archivo", "")
                            contenido = db.get_attachment_content(current_conv, nombre)
                            if contenido is None:
                                disponibles = ", ".join(attachment_filenames)
                                resultado = f"No encontré un adjunto llamado '{nombre}'. Los disponibles son: {disponibles}"
                            else:
                                resultado = contenido
                            caption = f"📎 El modelo consultó el archivo adjunto: {nombre}"
                        elif tool_name == "buscar_en_internet":
                            query = args.get("query", "")
                            resultado = _web_search(query)
                            caption = f"🔎 El modelo buscó en internet: {query}"
                        elif tool_name == "leer_pagina_web":
                            url = args.get("url", "")
                            resultado = _read_webpage(url)
                            caption = f"🌐 El modelo leyó: {url}"
                        else:
                            resultado = f"Herramienta desconocida: {tool_name}"
                            caption = None

                        messages_for_template.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": resultado,
                        })
                        if caption:
                            with chat_container:
                                st.caption(caption)
                else:
                    # Se agotó MAX_TOOL_ROUNDS sin que el modelo se decidiera
                    # a responder — cortamos y forzamos el request final sin
                    # tools, para no quedar en loop infinito.
                    need_streaming_call = True

            if need_streaming_call:
                # El servidor (llama-server) aplica el chat template embebido
                # en el GGUF del modelo automáticamente, igual que antes.
                stream = llm.chat.completions.create(
                    model=model_name,
                    messages=messages_for_template,
                    max_tokens=active_max_tokens,
                    temperature=active_temperature,
                    stream=True,
                    stream_options={"include_usage": True},
                )

                for chunk in stream:
                    # El último chunk de un stream con include_usage no trae
                    # "choices" (o viene vacío), solo el campo "usage".
                    if getattr(chunk, "usage", None):
                        usage = chunk.usage

                    if not chunk.choices:
                        continue

                    choice = chunk.choices[0]
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason

                    delta = choice.delta

                    # En llama-server, el "pensamiento" del modelo llega en un
                    # campo separado (reasoning_content), NO mezclado con
                    # <think>...</think> dentro de content. Lo separamos para
                    # poder diagnosticar cuánto budget de tokens se está yendo
                    # en razonamiento interno vs. respuesta real.
                    reasoning_piece = getattr(delta, "reasoning_content", None)
                    if reasoning_piece:
                        reasoning_text += reasoning_piece

                    token = delta.content
                    if not token:
                        continue

                    response_text += token
                    response_placeholder.markdown(response_text + "▌")

                response_text = response_text.strip()

            if not response_text and finish_reason == "length" and reasoning_text:
                # Caso detectado: el modelo gastó todo el max_tokens pensando
                # (reasoning_content) y el stream se cortó por "length" antes
                # de escribir una sola letra de la respuesta real.
                response_text = (
                    "⚠️ El modelo agotó el presupuesto de tokens (`max_tokens`) razonando "
                    "internamente y no llegó a escribir la respuesta final "
                    "(`finish_reason: length`). Probá subir el **'Máx. tokens de respuesta'** "
                    "de este modelo desde la consola de admin, o bajar el **Nivel de "
                    "Razonamiento** en tus opciones de usuario para este tipo de prompt."
                )
                response_placeholder.warning(response_text)
            else:
                response_placeholder.markdown(response_text if response_text else "*(respuesta vacía)*")

            # --- Diagnóstico ---
            # Se imprime a stdout (visible con `docker logs`) y se muestra
            # también en un expander en la UI, para saber si el problema es
            # que el modelo se quedó sin tokens (finish_reason == "length")
            # y/o si el razonamiento interno se comió todo el presupuesto.
            debug_info = {
                "prompt_chars": len(prompt),
                "finish_reason": finish_reason,
                "prompt_tokens": getattr(usage, "prompt_tokens", None) if usage else None,
                "completion_tokens": getattr(usage, "completion_tokens", None) if usage else None,
                "total_tokens": getattr(usage, "total_tokens", None) if usage else None,
                "reasoning_chars": len(reasoning_text),
                "response_chars": len(response_text),
                "max_tokens_configurado": active_max_tokens,
                "mensajes_enviados_al_modelo": len(effective_messages_db) + 1,
                "compresion_activa": existing_summary is not None,
            }
            print(f"[DEBUG chat] {debug_info}", flush=True)

            with st.expander("🔍 Info de diagnóstico (último mensaje)"):
                st.json(debug_info)

            # Guardamos el prompt_tokens real de ESTE turno para poder
            # decidir, en el próximo mensaje, si hace falta disparar la
            # compresión — más preciso que estimar con una heurística de
            # caracteres, ya que es la medición real que devuelve la API.
            if usage and getattr(usage, "prompt_tokens", None):
                st.session_state["last_prompt_tokens"][conv_id] = usage.prompt_tokens

        db.save_message(current_conv, "assistant", response_text)

        if prompt_text:
            new_memories = convenience.maybe_extract_memories(user["id"], prompt_text)
            for mem in new_memories:
                with chat_container:
                    st.caption(f"🧠 Nuevo recuerdo guardado: {mem}")
