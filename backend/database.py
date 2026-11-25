import os
import secrets
import uuid
import bcrypt
import psycopg2
from psycopg2.extras import RealDictCursor


def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        user=os.getenv("DB_USER", "damian"),
        password=os.getenv("DB_PASSWORD", "root_password"),
        database=os.getenv("DB_NAME", "ai_chat")
    )


# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------

def hash_password(password_plain):
    return bcrypt.hashpw(password_plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password_plain, password_hash):
    return bcrypt.checkpw(password_plain.encode("utf-8"), password_hash.encode("utf-8"))


def generate_password(length=12):
    """Password temporal legible para mostrar en la UI al crear/resetear un
    usuario. token_urlsafe sobre-genera caracteres y se trunca al largo
    pedido (no afecta la entropía real usada)."""
    return secrets.token_urlsafe(length)[:length]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def _needs_schema_migration(cursor):
    """Detecta el esquema pre-multiusuario: 'conversations' existía pero sin
    columna user_id. CREATE TABLE IF NOT EXISTS no puede agregarle solo una
    columna NOT NULL sin default a una tabla ya existente, y el usuario
    autorizó perder los datos viejos en vez de escribir una migración ALTER
    completa. Devuelve False en una DB nueva (nada que migrar) o en una ya
    migrada (ya tiene user_id) -- en ambos casos no se dropea nada."""
    cursor.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.tables WHERE table_name = 'conversations'
        );
    """)
    if not cursor.fetchone()[0]:
        return False
    cursor.execute("""
        SELECT EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'conversations' AND column_name = 'user_id'
        );
    """)
    return not cursor.fetchone()[0]


def ensure_tables():
    """CREATE TABLE IF NOT EXISTS para todo el esquema: se puede llamar
    siempre al arrancar la app. La única excepción es la migración
    destructiva de una sola vez para 'conversations' (ver
    _needs_schema_migration) -- el resto es puramente aditivo/idempotente."""
    conn = get_db_connection()
    cursor = conn.cursor()

    if _needs_schema_migration(cursor):
        cursor.execute("DROP TABLE IF EXISTS conversations, messages, attachments, settings CASCADE;")

    cursor.execute("""
        CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

        CREATE TABLE IF NOT EXISTS users (
            id UUID PRIMARY KEY,
            username VARCHAR(100) UNIQUE NOT NULL,
            full_name VARCHAR(255) NOT NULL,
            password_hash TEXT NOT NULL,
            is_admin BOOLEAN NOT NULL DEFAULT false,
            locked BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        -- Reemplaza al dict BACKENDS hardcodeado. 'label' es el nombre
        -- amigable que ve el usuario en el selector (no es único a
        -- propósito: permite dar de alta el mismo model_name/base_url dos
        -- veces con distinto system_prompt, ej. "Qwen3.8 Programador" vs
        -- "Qwen3.8 Amigo/Consejero"). 'model_name' es el string real que se
        -- manda como `model=` a la API OpenAI-compatible.
        CREATE TABLE IF NOT EXISTS models (
            id UUID PRIMARY KEY,
            label VARCHAR(255) NOT NULL,
            model_name VARCHAR(255) NOT NULL,
            base_url TEXT NOT NULL,
            ctx_size INTEGER NOT NULL,
            system_prompt TEXT NOT NULL DEFAULT '',
            temperature REAL NOT NULL DEFAULT 0.7,
            max_tokens INTEGER NOT NULL DEFAULT 512,
            is_convenience BOOLEAN NOT NULL DEFAULT false,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS user_model_access (
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            model_id UUID NOT NULL REFERENCES models(id) ON DELETE CASCADE,
            PRIMARY KEY (user_id, model_id)
        );

        -- El id de esta tabla es el token opaco que se guarda en la cookie
        -- del browser (ver auth.py). expires_at fijo desde la creación (sin
        -- sliding window) para mantener la lógica simple.
        CREATE TABLE IF NOT EXISTS sessions (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ NOT NULL
        );

        CREATE TABLE IF NOT EXISTS user_settings (
            user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
            reasoning_level VARCHAR(20) NOT NULL DEFAULT 'medium',
            web_tools_enabled BOOLEAN NOT NULL DEFAULT true,
            active_model_id UUID REFERENCES models(id) ON DELETE SET NULL
        );

        CREATE TABLE IF NOT EXISTS memories (
            id SERIAL PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS conversations (
            id UUID PRIMARY KEY,
            user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            title VARCHAR(255) NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS messages (
            id SERIAL PRIMARY KEY,
            conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE,
            role VARCHAR(50) NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );

        CREATE TABLE IF NOT EXISTS attachments (
            id SERIAL PRIMARY KEY,
            conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)

    _seed_admin(cursor)
    _seed_default_models(cursor)

    conn.commit()
    cursor.close()
    conn.close()


def _seed_admin(cursor):
    """Login inicial admin:admin -- solo se crea si la tabla users está
    vacía, para no pisar credenciales ya cambiadas por el admin real."""
    cursor.execute("SELECT COUNT(*) FROM users;")
    if cursor.fetchone()[0] > 0:
        return
    cursor.execute(
        "INSERT INTO users (id, username, full_name, password_hash, is_admin, locked) "
        "VALUES (%s, %s, %s, %s, true, false);",
        (str(uuid.uuid4()), "admin", "Administrador", hash_password("admin")),
    )


def _seed_default_models(cursor):
    """Replica el dict BACKENDS que existía antes de este cambio, para que
    un deploy existente siga funcionando sin que el admin tenga que
    recrear los modelos a mano el día 1. Solo corre si 'models' está vacía."""
    cursor.execute("SELECT COUNT(*) FROM models;")
    if cursor.fetchone()[0] > 0:
        return
    defaults = [
        (
            "Qwen3.6 (estándar)", "qwen3.6-35b-a3b",
            os.environ.get("LLM_SERVER_URL", "http://llm_server:8080/v1"),
            262144, "Respondé siempre en el idioma del usuario.", 0.7, 512,
        ),
        (
            "Qwen3.6 Uncensored", "qwen3.6-35b-a3b-uncensored",
            os.environ.get("LLM_SERVER_URL_UNCENSORED", "http://llm_server_uncensored:8080/v1"),
            262144, "Respondé siempre en el idioma del usuario.", 0.7, 512,
        ),
    ]
    for label, model_name, base_url, ctx_size, system_prompt, temperature, max_tokens in defaults:
        cursor.execute(
            "INSERT INTO models (id, label, model_name, base_url, ctx_size, system_prompt, temperature, max_tokens) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s);",
            (str(uuid.uuid4()), label, model_name, base_url, ctx_size, system_prompt, temperature, max_tokens),
        )


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

def create_user(username, full_name, password_plain, locked=False):
    """Crea un usuario estándar (nunca admin -- no hay UI para promover a
    admin, solo existe el admin sembrado por _seed_admin)."""
    conn = get_db_connection()
    cursor = conn.cursor()
    user_id = str(uuid.uuid4())
    cursor.execute(
        "INSERT INTO users (id, username, full_name, password_hash, is_admin, locked) "
        "VALUES (%s, %s, %s, %s, false, %s);",
        (user_id, username, full_name, hash_password(password_plain), locked),
    )
    conn.commit()
    cursor.close()
    conn.close()
    return user_id


def list_users():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT id, username, full_name, is_admin, locked, created_at FROM users ORDER BY username ASC;")
    res = cursor.fetchall()
    for row in res:
        row["id"] = str(row["id"])
    cursor.close()
    conn.close()
    return res


def get_user_by_username(username):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM users WHERE username = %s;", (username,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if row:
        row["id"] = str(row["id"])
    return row


def get_user_by_id(user_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM users WHERE id = %s;", (user_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if row:
        row["id"] = str(row["id"])
    return row


def update_user(user_id, full_name, locked):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET full_name = %s, locked = %s WHERE id = %s;",
        (full_name, locked, user_id),
    )
    conn.commit()
    cursor.close()
    conn.close()


def set_user_password(user_id, password_plain):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE users SET password_hash = %s WHERE id = %s;",
        (hash_password(password_plain), user_id),
    )
    conn.commit()
    cursor.close()
    conn.close()


def delete_user(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE id = %s;", (user_id,))
    conn.commit()
    cursor.close()
    conn.close()


# ---------------------------------------------------------------------------
# Acceso a modelos por usuario
# ---------------------------------------------------------------------------

def get_user_model_ids(user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT model_id FROM user_model_access WHERE user_id = %s;", (user_id,))
    ids = {str(row[0]) for row in cursor.fetchall()}
    cursor.close()
    conn.close()
    return ids


def set_user_model_access(user_id, model_ids):
    """Reemplaza por completo el set de modelos habilitados para el
    usuario -- semántica simple de 'guardar los checkboxes tal como están
    ahora', igual que el resto de los forms de settings de esta app."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM user_model_access WHERE user_id = %s;", (user_id,))
    for model_id in model_ids:
        cursor.execute(
            "INSERT INTO user_model_access (user_id, model_id) VALUES (%s, %s);",
            (user_id, model_id),
        )
    conn.commit()
    cursor.close()
    conn.close()


def get_models_for_user(user_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("""
        SELECT m.* FROM models m
        JOIN user_model_access uma ON uma.model_id = m.id
        WHERE uma.user_id = %s
        ORDER BY m.label ASC;
    """, (user_id,))
    res = cursor.fetchall()
    for row in res:
        row["id"] = str(row["id"])
    cursor.close()
    conn.close()
    return res


# ---------------------------------------------------------------------------
# Modelos (CRUD admin)
# ---------------------------------------------------------------------------

def create_model(label, model_name, base_url, ctx_size, system_prompt, temperature, max_tokens, is_convenience):
    conn = get_db_connection()
    cursor = conn.cursor()
    model_id = str(uuid.uuid4())
    cursor.execute(
        "INSERT INTO models (id, label, model_name, base_url, ctx_size, system_prompt, temperature, max_tokens, is_convenience) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s);",
        (model_id, label, model_name, base_url, ctx_size, system_prompt, temperature, max_tokens, is_convenience),
    )
    conn.commit()
    cursor.close()
    conn.close()
    return model_id


def list_models():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM models ORDER BY label ASC;")
    res = cursor.fetchall()
    for row in res:
        row["id"] = str(row["id"])
    cursor.close()
    conn.close()
    return res


def get_model(model_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM models WHERE id = %s;", (model_id,))
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if row:
        row["id"] = str(row["id"])
    return row


def update_model(model_id, label, model_name, base_url, ctx_size, system_prompt, temperature, max_tokens, is_convenience):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE models SET label = %s, model_name = %s, base_url = %s, ctx_size = %s, "
        "system_prompt = %s, temperature = %s, max_tokens = %s, is_convenience = %s WHERE id = %s;",
        (label, model_name, base_url, ctx_size, system_prompt, temperature, max_tokens, is_convenience, model_id),
    )
    conn.commit()
    cursor.close()
    conn.close()


def delete_model(model_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM models WHERE id = %s;", (model_id,))
    conn.commit()
    cursor.close()
    conn.close()


def get_convenience_model():
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM models WHERE is_convenience = true ORDER BY created_at ASC LIMIT 1;")
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if row:
        row["id"] = str(row["id"])
    return row


# ---------------------------------------------------------------------------
# Sesiones (login persistido en cookie)
# ---------------------------------------------------------------------------

def create_session(user_id, ttl_days=30):
    conn = get_db_connection()
    cursor = conn.cursor()
    session_id = str(uuid.uuid4())
    cursor.execute(
        "INSERT INTO sessions (id, user_id, expires_at) VALUES (%s, %s, now() + (%s || ' days')::interval);",
        (session_id, user_id, ttl_days),
    )
    conn.commit()
    cursor.close()
    conn.close()
    return session_id


def get_session_user(session_id):
    """Devuelve el usuario dueño de la sesión si es válida (existe, no
    expiró, usuario no bloqueado), o None. None también si el session_id
    no es un UUID válido (cookie corrupta/vieja)."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    try:
        cursor.execute("""
            SELECT u.* FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.id = %s AND s.expires_at > now() AND u.locked = false;
        """, (session_id,))
        row = cursor.fetchone()
    except psycopg2.errors.InvalidTextRepresentation:
        conn.rollback()
        row = None
    cursor.close()
    conn.close()
    if row:
        row["id"] = str(row["id"])
    return row


def delete_session(session_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM sessions WHERE id = %s;", (session_id,))
        conn.commit()
    except psycopg2.errors.InvalidTextRepresentation:
        conn.rollback()
    cursor.close()
    conn.close()


# ---------------------------------------------------------------------------
# Settings por usuario (reemplaza los settings por-backend de antes)
# ---------------------------------------------------------------------------

def get_user_settings(user_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT * FROM user_settings WHERE user_id = %s;", (user_id,))
    row = cursor.fetchone()
    if not row:
        cursor.execute(
            "INSERT INTO user_settings (user_id) VALUES (%s) "
            "ON CONFLICT (user_id) DO NOTHING;",
            (user_id,),
        )
        conn.commit()
        cursor.execute("SELECT * FROM user_settings WHERE user_id = %s;", (user_id,))
        row = cursor.fetchone()
    cursor.close()
    conn.close()
    if row.get("active_model_id"):
        row["active_model_id"] = str(row["active_model_id"])
    return row


def save_user_settings(user_id, reasoning_level, web_tools_enabled):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO user_settings (user_id, reasoning_level, web_tools_enabled)
        VALUES (%s, %s, %s)
        ON CONFLICT (user_id) DO UPDATE SET
            reasoning_level = EXCLUDED.reasoning_level,
            web_tools_enabled = EXCLUDED.web_tools_enabled;
    """, (user_id, reasoning_level, web_tools_enabled))
    conn.commit()
    cursor.close()
    conn.close()


def set_active_model(user_id, model_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO user_settings (user_id, active_model_id)
        VALUES (%s, %s)
        ON CONFLICT (user_id) DO UPDATE SET active_model_id = EXCLUDED.active_model_id;
    """, (user_id, model_id))
    conn.commit()
    cursor.close()
    conn.close()


# ---------------------------------------------------------------------------
# Recuerdos
# ---------------------------------------------------------------------------

def list_memories(user_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute("SELECT id, content, created_at FROM memories WHERE user_id = %s ORDER BY created_at ASC;", (user_id,))
    res = cursor.fetchall()
    cursor.close()
    conn.close()
    return res


def add_memory(user_id, content):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO memories (user_id, content) VALUES (%s, %s);", (user_id, content))
    conn.commit()
    cursor.close()
    conn.close()


def memory_exists(user_id, content):
    """Dedup exacto para no acumular el mismo recuerdo mandado dos veces
    por la extracción automática del modelo de convenience."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM memories WHERE user_id = %s AND content = %s LIMIT 1;", (user_id, content))
    exists = cursor.fetchone() is not None
    cursor.close()
    conn.close()
    return exists


def delete_memory(memory_id, user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM memories WHERE id = %s AND user_id = %s;", (memory_id, user_id))
    conn.commit()
    cursor.close()
    conn.close()


# ---------------------------------------------------------------------------
# Conversaciones (scoped por usuario)
# ---------------------------------------------------------------------------

def get_conversations(user_id):
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute(
        "SELECT id, title, created_at FROM conversations WHERE user_id = %s ORDER BY created_at DESC;",
        (user_id,),
    )
    res = cursor.fetchall()
    for row in res:
        row["id"] = str(row["id"])
    cursor.close()
    conn.close()
    return res


def get_conversation(conv_id, user_id):
    """Devuelve la conversación solo si pertenece al usuario -- defensa en
    profundidad para no confiar únicamente en que la UI solo ofrezca ids
    del propio usuario."""
    conn = get_db_connection()
    cursor = conn.cursor(cursor_factory=RealDictCursor)
    cursor.execute(
        "SELECT id, title, created_at FROM conversations WHERE id = %s AND user_id = %s;",
        (conv_id, user_id),
    )
    row = cursor.fetchone()
    cursor.close()
    conn.close()
    if row:
        row["id"] = str(row["id"])
    return row


def create_conversation(user_id, title="Nuevo Chat"):
    conn = get_db_connection()
    cursor = conn.cursor()
    conv_id = str(uuid.uuid4())
    cursor.execute(
        "INSERT INTO conversations (id, user_id, title) VALUES (%s, %s, %s);",
        (conv_id, user_id, title),
    )
    conn.commit()
    cursor.close()
    conn.close()
    return conv_id


def delete_conversation(conv_id, user_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM conversations WHERE id = %s AND user_id = %s;", (conv_id, user_id))
    conn.commit()
    cursor.close()
    conn.close()


def update_conversation_title(conv_id, title):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE conversations SET title = %s WHERE id = %s;", (title, conv_id))
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


def count_messages(conv_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = %s;", (conv_id,))
    count = cursor.fetchone()[0]
    cursor.close()
    conn.close()
    return count


def save_message(conv_id, role, content):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO messages (conversation_id, role, content) VALUES (%s, %s, %s);", (conv_id, role, content))
    conn.commit()
    cursor.close()
    conn.close()


# ---------------------------------------------------------------------------
# Adjuntos
# ---------------------------------------------------------------------------

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
