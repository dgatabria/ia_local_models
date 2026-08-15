import os
import uuid
import psycopg2
from psycopg2.extras import RealDictCursor

def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER", "damian"),
        password=os.getenv("DB_PASSWORD", "root_password"),
        database=os.getenv("DB_NAME", "ai_chat")
    )

def get_conversations():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT id, title, created_at FROM conversations ORDER BY created_at DESC;")
    res = cursor.fetchall()
    # Convertir los UUID a string para que Streamlit no chille al usarlos como keys
    for row in res:
        row['id'] = str(row['id'])
    cursor.close()
    conn.close()
    return res

def create_conversation(title="Nuevo Chat"):
    conn = get_db_connection()
    cursor = conn.cursor()
    conv_id = str(uuid.uuid4())
    cursor.execute("INSERT INTO conversations (id, title) VALUES (%s, %s);", (conv_id, title))
    conn.commit()
    cursor.close()
    conn.close()
    return conv_id

def delete_conversation(conv_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM conversations WHERE id = %s;", (conv_id,))
    conn.commit()
    cursor.close()
    conn.close()

def get_messages(conv_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT role, content FROM messages WHERE conversation_id = %s ORDER BY created_at ASC;", (conv_id,))
    res = cursor.fetchall()
    cursor.close()
    conn.close()
    return res

BACKEND_SETTING_DEFAULTS = {
    "standard": {
        "system_prompt": "Respondé siempre en el idioma del usuario.",
        "temperature": "0.7",
        "max_tokens": "512",
        "reasoning_level": "medium",
    },
    "uncensored": {
        # temperature=1.0 según los settings recomendados por los propios
        # autores de Qwen para modo thinking general (ver ficha del modelo).
        "system_prompt": "Respondé siempre en el idioma del usuario.",
        "temperature": "1.0",
        "max_tokens": "512",
        "reasoning_level": "medium",
    },
}


def get_global_settings():
    """Settings que NO dependen de qué backend/modelo esté activo."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT key, value FROM settings WHERE key IN ('web_tools_enabled', 'backend_choice');")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    defaults = {"web_tools_enabled": "true", "backend_choice": "standard"}
    saved = {row["key"]: row["value"] for row in rows}
    return {**defaults, **saved}


def save_global_settings(web_tools_enabled, backend_choice):
    conn = get_db_connection()
    cursor = conn.cursor()
    items = [
        ("web_tools_enabled", str(web_tools_enabled).lower()),
        ("backend_choice", backend_choice),
    ]
    for key, value in items:
        cursor.execute("""
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
        """, (key, value))
    conn.commit()
    cursor.close()
    conn.close()


def get_backend_settings(backend_key):
    """Settings de comportamiento (system_prompt, temperature, max_tokens,
    reasoning_level) INDEPENDIENTES por backend — namespaced en la tabla
    con el prefijo '{backend_key}__', para que cambiar de modelo no pise
    la configuración del otro."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    prefix = f"{backend_key}__"
    cursor.execute("SELECT key, value FROM settings WHERE key LIKE %s;", (prefix + "%",))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()
    defaults = BACKEND_SETTING_DEFAULTS.get(backend_key, BACKEND_SETTING_DEFAULTS["standard"])
    saved = {row["key"][len(prefix):]: row["value"] for row in rows}
    return {**defaults, **saved}


def save_backend_settings(backend_key, system_prompt, temperature, max_tokens, reasoning_level):
    conn = get_db_connection()
    cursor = conn.cursor()
    prefix = f"{backend_key}__"
    items = [
        (prefix + "system_prompt", system_prompt),
        (prefix + "temperature", str(temperature)),
        (prefix + "max_tokens", str(max_tokens)),
        (prefix + "reasoning_level", reasoning_level),
    ]
    for key, value in items:
        cursor.execute("""
            INSERT INTO settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value;
        """, (key, value))
    conn.commit()
    cursor.close()
    conn.close()

def ensure_attachments_table():
    """CREATE TABLE IF NOT EXISTS: se puede llamar siempre al arrancar la
    app sin miedo a romper una base ya existente (a diferencia de tocar
    init.sql, que solo corre la primera vez que se crea el volumen de
    Postgres)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS attachments (
            id SERIAL PRIMARY KEY,
            conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
    conn.commit()
    cursor.close()
    conn.close()


def save_attachment(conv_id, filename, content):
    """Persiste el texto completo de un adjunto (PDF/TXT/DOCX ya
    extraído), para que el modelo lo pueda volver a pedir más adelante en
    la misma conversación sin que el usuario tenga que resubirlo."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO attachments (conversation_id, filename, content) VALUES (%s, %s, %s);",
        (conv_id, filename, content),
    )
    conn.commit()
    cursor.close()
    conn.close()


def get_attachment_filenames(conv_id):
    """Nombres únicos de los adjuntos disponibles en la conversación (sin
    el contenido), para listárselos al modelo en el system prompt."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute(
        "SELECT DISTINCT filename FROM attachments WHERE conversation_id = %s ORDER BY filename ASC;",
        (conv_id,),
    )
    res = [row["filename"] for row in cursor.fetchall()]
    cursor.close()
    conn.close()
    return res


def get_attachment_content(conv_id, filename):
    """Texto completo de un adjunto puntual. Si se subió más de una vez
    con el mismo nombre, devuelve la versión más reciente."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute(
        "SELECT content FROM attachments WHERE conversation_id = %s AND filename = %s "
        "ORDER BY created_at DESC LIMIT 1;",
        (conv_id, filename),
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    return row["content"] if row else None


def save_message(conv_id, role, content):
    """Guarda un mensaje en la conversación y actualiza el título si es el primer mensaje del usuario."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s);", (conv_id, role, content))
    
    # Actualizar el título si es el primer mensaje del usuario
    cursor.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = %s;", (conv_id,))
    if cursor.fetchone()[0] == 1 and role == "user":
        title = content[:30] + "..." if len(content) > 30 else content
        cursor.execute("UPDATE conversations SET title = %s WHERE id = %s;", (title, conv_id))
        
    conn.commit()
    cursor.close()
    conn.close()
