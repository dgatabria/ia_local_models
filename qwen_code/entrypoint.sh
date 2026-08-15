#!/bin/bash
set -e

CONFIG_DIR="${HOME}/.qwen"
CONFIG_FILE="${CONFIG_DIR}/settings.json"

mkdir -p "${CONFIG_DIR}"

# Solo escribimos el settings.json si todavía no existe, para no pisar
# cambios que hagas a mano más adelante (por ejemplo, si agregás otro
# provider o ajustás sampling params). Si en algún momento querés que se
# regenere desde cero, borrá el volumen o el archivo y reiniciá el
# contenedor.
if [ ! -f "${CONFIG_FILE}" ]; then
    echo ">> Generando ${CONFIG_FILE} apuntando a ${LLM_SERVER_URL:-http://llm_server:8080/v1} ..."
    cat > "${CONFIG_FILE}" <<EOF
{
  "env": {
    "LOCAL_LLAMA_API_KEY": "local"
  },
  "modelProviders": {
    "openai": {
      "protocol": "openai",
      "models": [
        {
          "id": "${MODEL_ALIAS:-qwen3.6-local}",
          "name": "Qwen3.6 local (llm_server)",
          "envKey": "LOCAL_LLAMA_API_KEY",
          "baseUrl": "${LLM_SERVER_URL:-http://llm_server:8080/v1}",
          "generationConfig": {
            "timeout": 300000,
            "maxRetries": 1,
            "contextWindowSize": ${QWEN_CTX_SIZE:-262144}
          }
        }
      ]
    }
  },
  "model": {
    "name": "${MODEL_ALIAS:-qwen3.6-local}"
  }
}
EOF
else
    echo ">> ${CONFIG_FILE} ya existe, no lo piso."
fi

# Este contenedor no corre un server — Qwen Code es una TUI interactiva.
# Lo dejamos vivo para poder entrar con:
#   docker exec -it qwen_code qwen
# en vez de exponer un puerto como el resto de los servicios.
# Claude Code: forzamos permissions.defaultMode=bypassPermissions en
# ~/.claude/settings.json, para no depender de acordarnos de pasar
# --dangerously-skip-permissions cada vez. Usamos un merge (no
# sobrescritura completa) porque ~/.claude está montado desde tu host —
# no queremos pisar el resto de tu configuración real. Idempotente:
# corre en cada arranque del contenedor, no solo la primera vez, así
# queda garantizado el valor aunque el archivo del host cambie.
CLAUDE_SETTINGS="${HOME}/.claude/settings.json"
mkdir -p "$(dirname "${CLAUDE_SETTINGS}")"
python3 - "${CLAUDE_SETTINGS}" <<'PYEOF'
import json
import sys

path = sys.argv[1]
try:
    with open(path) as f:
        data = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    data = {}

data.setdefault("permissions", {})
data["permissions"]["defaultMode"] = "bypassPermissions"

with open(path, "w") as f:
    json.dump(data, f, indent=2)

print(f">> {path}: permissions.defaultMode = bypassPermissions")
PYEOF

echo ">> Contenedor listo. Entrá con: docker exec -it qwen_code qwen"
exec tail -f /dev/null
