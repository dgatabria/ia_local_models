#!/bin/bash
set -euo pipefail

# El backend Xet dio descargas inestables con GGUF grandes en este entorno.
export HF_HUB_DISABLE_XET=1

MODEL_DIR="/models/qwen3.8-27b"
MODEL_FILE="Qwen3.8-27B-UD-Q3_K_XL.gguf"
MODEL_PATH="${MODEL_DIR}/${MODEL_FILE}"

mkdir -p "${MODEL_DIR}"

if [ ! -f "${MODEL_PATH}" ]; then
    echo ">> Descargando ${MODEL_FILE} (~13.4GB, puede tardar varios minutos)..."
    hf download unsloth/Qwen3.8-27B-GGUF \
        "${MODEL_FILE}" \
        --local-dir "${MODEL_DIR}"
else
    echo ">> Modelo ya cacheado en ${MODEL_PATH}, omito descarga."
fi

echo ">> Iniciando llama-server (Qwen3.8-27B, UD-Q3_K_XL, contexto ${LLAMA_CTX_SIZE_QWEN38_27B:-65536})..."

# Perfil inicial deliberadamente conservador para la RTX 4090 Laptop de 16GB:
# - solo texto: no se descarga ni carga mmproj (~931MB);
# - sin MTP: primero se mide calidad/estabilidad base;
# - todas las capas en GPU y un único slot;
# - KV Q8 + Flash Attention para ahorrar VRAM sin entrar todavía en el camino
#   experimental de KV Q4;
# - 65.536 tokens: primer escalón seguro antes de probar 81.920.
#
# Los parámetros de sampling corresponden al modo thinking recomendado por
# Qwen3.8. `reasoning_effort` y `preserve_thinking` se controlan por request.
exec llama-server \
    --model "${MODEL_PATH}" \
    --host 0.0.0.0 \
    --port 8080 \
    -ngl 99 \
    -np 1 \
    --ctx-size "${LLAMA_CTX_SIZE_QWEN38_27B:-65536}" \
    --flash-attn on \
    -ctk q8_0 \
    -ctv q8_0 \
    --jinja \
    --cache-reuse 256 \
    --cache-ram 4096 \
    --ubatch-size 1024 \
    --metrics \
    --temp 1.0 \
    --top-p 0.95 \
    --top-k 20 \
    --min-p 0.0 \
    --presence-penalty 0.0 \
    --repeat-penalty 1.0
