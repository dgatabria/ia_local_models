from openai import OpenAI
import database as db

# Temperatura/max_tokens fijos y bajos para estas llamadas: son tareas
# chicas y estructuradas (titular, extraer datos puntuales), no charla
# libre -- no tiene sentido usar la temperature/max_tokens configurados
# para el modelo como chat principal.
_UTILITY_TEMPERATURE = 0.2
_TITLE_MAX_TOKENS = 32
_MEMORY_MAX_TOKENS = 256


def _client_for(model):
    return OpenAI(base_url=model["base_url"], api_key="no-key")


def maybe_generate_title(prompt_text):
    """Devuelve un título corto (4-5 palabras) generado por el modelo
    marcado is_convenience, o None si no hay uno configurado o la llamada
    falla. El caller (chat_ui) debe tener un fallback para el caso None."""
    model = db.get_convenience_model()
    if model is None:
        return None
    try:
        client = _client_for(model)
        response = client.chat.completions.create(
            model=model["model_name"],
            messages=[{
                "role": "user",
                "content": (
                    "Generá un título de 4 o 5 palabras para esta conversación de chat, "
                    "en base al primer mensaje del usuario. Respondé SOLO el título, "
                    "sin comillas ni puntuación final.\n\n"
                    f"Mensaje: {prompt_text}"
                ),
            }],
            max_tokens=_TITLE_MAX_TOKENS,
            temperature=_UTILITY_TEMPERATURE,
        )
        title = (response.choices[0].message.content or "").strip().strip('"').strip("'")
        return title[:255] if title else None
    except Exception as e:
        print(f"[WARN] No se pudo generar título con el modelo de convenience: {e}", flush=True)
        return None


def maybe_extract_memories(user_id, prompt_text):
    """Le pide al modelo de convenience que extraiga datos duraderos
    (preferencias, actividades) del mensaje del usuario y los guarda como
    recuerdos nuevos (con dedup exacto contra los ya existentes). Devuelve
    la lista de recuerdos nuevos agregados. Best-effort: cualquier fallo
    acá se traga y nunca debe afectar la respuesta principal del chat."""
    model = db.get_convenience_model()
    if model is None:
        return []
    try:
        client = _client_for(model)
        response = client.chat.completions.create(
            model=model["model_name"],
            messages=[{
                "role": "user",
                "content": (
                    "Del siguiente mensaje de un usuario de un chatbot, extraé (si las hay) "
                    "preferencias personales o datos duraderos sobre sus actividades que valga "
                    "la pena recordar para futuras conversaciones (ej. su trabajo, gustos, "
                    "proyectos en los que trabaja, cosas que pidió explícitamente que se "
                    "recuerden). Si no hay nada nuevo o relevante, respondé exactamente NADA. "
                    "Si hay algo, respondé una lista corta, un dato por línea, sin viñetas ni "
                    "numeración, en tercera persona.\n\n"
                    f"Mensaje: {prompt_text}"
                ),
            }],
            max_tokens=_MEMORY_MAX_TOKENS,
            temperature=_UTILITY_TEMPERATURE,
        )
        content = (response.choices[0].message.content or "").strip()
        if not content or content.upper() == "NADA":
            return []

        new_memories = []
        for line in content.splitlines():
            line = line.strip(" -*\t")
            if not line or line.upper() == "NADA":
                continue
            if db.memory_exists(user_id, line):
                continue
            db.add_memory(user_id, line)
            new_memories.append(line)
        return new_memories
    except Exception as e:
        print(f"[WARN] No se pudieron extraer recuerdos con el modelo de convenience: {e}", flush=True)
        return []
