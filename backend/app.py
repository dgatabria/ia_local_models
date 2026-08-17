import os
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
import database as db

st.set_page_config(page_title="Qwen Chat", layout="wide")

db.ensure_tables()

# Cuántas páginas de un PDF se rasterizan como imagen (además de extraerle
# el texto completo, que no tiene este límite). Cada página-imagen consume
# bastantes tokens de visión; 5 es un tope conservador para no disparar el
# costo en PDFs largos. El texto completo igual se manda siempre entero.
PDF_MAX_PAGES_AS_IMAGES = 5

WEB_SEARCH_MAX_RESULTS = 5
WEB_PAGE_MAX_CHARS = 8000  # tope de texto por página leída, para no comerse todo el contexto con un solo artículo

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

REASONING_INSTRUCTIONS = {
    "low": "Respondé de forma directa y concisa, sin explicar tu razonamiento.",
    "medium": "Respondé de forma clara, con el nivel de detalle que la pregunta merezca.",
    "high": "Antes de responder, pensá el problema paso a paso: considerá distintas interpretaciones, posibles matices o contradicciones, y razoná internamente antes de dar tu respuesta final. No muestres ese razonamiento intermedio, solo la conclusión.",
}

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

# Backends disponibles. "url" se puede overridear por env var (útil si en
# algún momento corrés esto fuera de docker-compose); si no, apunta al
# nombre de servicio de cada contenedor en la red interna de Docker.
BACKENDS = {
    "standard": {
        "label": "Qwen3.8 (estándar)",
        "base_url": os.environ.get("LLM_SERVER_URL_QWEN38", "http://llm_server_qwen38_27b:8080/v1"),
        "model": "qwen3.8-27b",
    },
    "uncensored": {
        "label": "Qwen3.6 Uncensored",
        "base_url": os.environ.get("LLM_SERVER_URL_UNCENSORED", "http://llm_server_uncensored:8080/v1"),
        "model": "qwen3.6-35b-a3b-uncensored",
    },
}


@st.cache_resource
def get_llm_client(base_url):
    # El modelo ya NO se carga en este proceso: vive en el servicio
    # llm_server correspondiente (llama-server), potencialmente compartido
    # también con OpenHands. Acá solo armamos un cliente HTTP
    # OpenAI-compatible. cache_resource cachea por valor de base_url, así
    # que cambiar de backend no reconstruye el cliente ya usado antes.
    return OpenAI(base_url=base_url, api_key="no-key")

if "settings_loaded" not in st.session_state:
    _g = db.get_global_settings()
    st.session_state.web_tools_enabled = _g["web_tools_enabled"] == "true"
    st.session_state.backend_choice    = _g["backend_choice"] if _g["backend_choice"] in BACKENDS else "standard"

    # Cada backend tiene su propio system_prompt/temperature/max_tokens/
    # reasoning_level, cargados independientemente — cambiar de modelo NO
    # pisa la configuración del otro.
    for _bk in BACKENDS:
        _b = db.get_backend_settings(_bk)
        st.session_state[f"system_prompt_{_bk}"] = _b["system_prompt"]
        st.session_state[f"temperature_{_bk}"]   = float(_b["temperature"])
        st.session_state[f"max_tokens_{_bk}"]    = int(_b["max_tokens"])
        st.session_state[f"reasoning_level_{_bk}"] = _b["reasoning_level"]

    st.session_state.current_conv    = None
    st.session_state.settings_loaded = True

with st.sidebar:
    st.title("🤖 Qwen3.6 Chat")

    if st.button("➕ Nuevo Chat", use_container_width=True):
        new_id = db.create_conversation()
        st.session_state.current_conv = new_id
        st.rerun()

    st.write("---")

    backend_keys = list(BACKENDS.keys())
    st.session_state.backend_choice = st.selectbox(
        "Modelo / backend",
        options=backend_keys,
        index=backend_keys.index(st.session_state.backend_choice),
        format_func=lambda k: BACKENDS[k]["label"],
        help="Elegí a qué llm_server le habla el chat. 'Uncensored' necesita el contenedor llm_server_uncensored levantado. El system prompt, temperatura, máx. tokens y nivel de razonamiento son independientes por backend.",
    )
    bc = st.session_state.backend_choice  # backend activo para el resto de esta corrida

    st.subheader("Configuración")
    st.session_state[f"reasoning_level_{bc}"] = st.select_slider(
        "Nivel de Razonamiento:",
        options=["low", "medium", "high"],
        value=st.session_state[f"reasoning_level_{bc}"],
        key=f"reasoning_level_widget_{bc}",
        help="'low': respuestas directas y cortas. 'medium': balance normal. 'high': el modelo razona internamente con más cuidado antes de responder (más lento, pero más preciso en preguntas complejas). Independiente por backend."
    )

    st.write("---")
    st.subheader("Historial de conversaciones")

    conversations = db.get_conversations()
    for conv in conversations:
        col1, col2 = st.columns([0.8, 0.2])
        button_type = "primary" if st.session_state.current_conv == conv["id"] else "secondary"

        if col1.button(f"💬 {conv['title']}", key=f"select_{conv['id']}", type=button_type, use_container_width=True):
            st.session_state.current_conv = conv["id"]
            st.rerun()

        if col2.button("🗑️", key=f"del_{conv['id']}"):
            db.delete_conversation(conv["id"])
            if st.session_state.current_conv == conv["id"]:
                st.session_state.current_conv = None
            st.rerun()

header_col1, header_col2 = st.columns([0.92, 0.08])
with header_col1:
    if st.session_state.current_conv:
        conv_title = next(
            (c["title"] for c in db.get_conversations() if c["id"] == st.session_state.current_conv),
            "Chat",
        )
        st.subheader(f"💬 {conv_title}")
with header_col2:
    with st.popover("⚙️ Settings", use_container_width=True):
        st.markdown(f"**Configuración de: {BACKENDS[bc]['label']}**")
        st.caption("Estos 4 campos son independientes por backend — elegí el modelo en el selector de la barra lateral.")

        st.session_state[f"system_prompt_{bc}"] = st.text_area(
            "System prompt",
            value=st.session_state[f"system_prompt_{bc}"],
            key=f"system_prompt_widget_{bc}",
            help="Instrucciones que se le pasan al modelo antes de cada conversación. Independiente por backend.",
            height=120,
        )

        st.session_state[f"temperature_{bc}"] = st.slider(
            "Temperatura",
            min_value=0.0,
            max_value=1.5,
            value=st.session_state[f"temperature_{bc}"],
            step=0.05,
            key=f"temperature_widget_{bc}",
            help="Más alto = respuestas más creativas/aleatorias. Más bajo = más determinísticas. Independiente por backend.",
        )

        st.session_state[f"max_tokens_{bc}"] = st.slider(
            "Máx. tokens de respuesta",
            min_value=64,
            max_value=8192,
            value=st.session_state[f"max_tokens_{bc}"],
            step=64,
            key=f"max_tokens_widget_{bc}",
            help="Límite de longitud de cada respuesta del modelo. Con Nivel de Razonamiento 'high' y prompts largos, el modelo puede gastar buena parte de este presupuesto solo 'pensando' antes de escribir la respuesta final: si ves respuestas vacías con finish_reason=length, subí este valor. Independiente por backend.",
        )

        st.write("---")

        st.session_state.web_tools_enabled = st.checkbox(
            "🌐 Permitir acceso a internet (búsqueda + lectura de páginas)",
            value=st.session_state.web_tools_enabled,
            help="Le da al modelo dos herramientas: buscar en DuckDuckGo y leer el contenido de una URL pública. Solo sitios públicos, sin login. Esto SÍ es global, aplica a cualquier backend.",
        )

        if st.button("💾 Guardar settings", use_container_width=True):
            db.save_backend_settings(
                bc,
                st.session_state[f"system_prompt_{bc}"],
                st.session_state[f"temperature_{bc}"],
                st.session_state[f"max_tokens_{bc}"],
                st.session_state[f"reasoning_level_{bc}"],
            )
            db.save_global_settings(
                st.session_state.web_tools_enabled,
                st.session_state.backend_choice,
            )
            st.success("✅ Guardado")

if st.session_state.current_conv:
    messages_db = db.get_messages(st.session_state.current_conv)

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
                    db.save_attachment(st.session_state.current_conv, f.name, pdf_text)
            elif name_lower.endswith(".txt"):
                txt_text = _extract_txt(file_bytes)
                attachment_notes.append(f"[Archivo de texto adjunto: {f.name}]")
                if txt_text.strip():
                    extra_text_parts.append(f"--- Contenido de {f.name} ---\n{txt_text}")
                    db.save_attachment(st.session_state.current_conv, f.name, txt_text)
            elif name_lower.endswith(".docx"):
                docx_text = _extract_docx(file_bytes)
                attachment_notes.append(f"[Documento Word adjunto: {f.name}]")
                if docx_text.strip():
                    extra_text_parts.append(f"--- Contenido de {f.name} ---\n{docx_text}")
                    db.save_attachment(st.session_state.current_conv, f.name, docx_text)

        prompt_text = (prompt or "").strip()
        display_text = "\n".join([prompt_text] + attachment_notes) if attachment_notes else prompt_text

        with chat_container:
            with st.chat_message("user"):
                st.write(display_text)
                for f in uploaded_files or []:
                    if f.type in ("image/jpeg", "image/png"):
                        st.image(f.getvalue(), width=200)
        db.save_message(st.session_state.current_conv, "user", display_text)

        reasoning_instruction = REASONING_INSTRUCTIONS[st.session_state[f"reasoning_level_{st.session_state.backend_choice}"]]

        # Adjuntos de texto disponibles en ESTA conversación (incluye el que
        # se acaba de subir en este mismo turno, si lo hay, porque ya se
        # guardó en la base un poco más arriba). Solo si hay al menos uno
        # habilitamos la tool de "leer_archivo_adjunto" — así no se le suma
        # overhead de tools a conversaciones que nunca adjuntaron nada.
        attachment_filenames = db.get_attachment_filenames(st.session_state.current_conv)

        system_content = f"{reasoning_instruction} {st.session_state[f'system_prompt_{st.session_state.backend_choice}']}"
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
        for msg in messages_db:
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

            active_backend = BACKENDS[st.session_state.backend_choice]
            llm = get_llm_client(active_backend["base_url"])
            model_name = active_backend["model"]
            active_max_tokens = st.session_state[f"max_tokens_{st.session_state.backend_choice}"]
            active_temperature = st.session_state[f"temperature_{st.session_state.backend_choice}"]

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
            if st.session_state.web_tools_enabled:
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
                            contenido = db.get_attachment_content(st.session_state.current_conv, nombre)
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
                # en el GGUF de Qwen3.6 automáticamente, igual que antes.
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
                    "(`finish_reason: length`). Probá subir **'Máx. tokens de respuesta'** "
                    "en Settings, o bajar el **Nivel de Razonamiento** para este tipo de prompt."
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
                "mensajes_en_historial_enviados": len(messages_db) + 1,
            }
            print(f"[DEBUG chat] {debug_info}", flush=True)

            with st.expander("🔍 Info de diagnóstico (último mensaje)"):
                st.json(debug_info)

        db.save_message(st.session_state.current_conv, "assistant", response_text)

else:
    st.info("Seleccioná un chat de la barra lateral o creá uno nuevo para empezar.")
