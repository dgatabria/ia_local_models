#!/bin/bash
set -euo pipefail

# El backend Xet dio descargas inestables con GGUF grandes en este entorno.
export HF_HUB_DISABLE_XET=1

MODEL_DIR="/models/RavenX-CyberAgent-35B-v5.1-Q4_K_M"
MODEL_FILE="RavenX-CyberAgent-35B-v5.1-Q4_K_M.gguf"
MODEL_PATH="${MODEL_DIR}/${MODEL_FILE}"

mkdir -p "${MODEL_DIR}"

if [ ! -f "${MODEL_PATH}" ]; then
    echo ">> Descargando ${MODEL_FILE} (~20.7GB, puede tardar varios minutos)..."
    hf download deadbydawn101/RavenX-CyberAgent-Qwen3.6-35B-A3B-Opus-4.7-OpenMythos-Pentester-BugHunter-RATH-GGUF \
        "${MODEL_FILE}" \
        --local-dir "${MODEL_DIR}"
else
    echo ">> Modelo ya cacheado en ${MODEL_PATH}, omito descarga."
fi

echo ">> Iniciando llama-server (RavenX-CyberAgent-35B-v5.1-Q4_K_M MoE, contexto ${LLAMA_CTX_SIZE_RAVENX:-262144})..."

# Configuración MoE (basada en Qwen3.6-35B-A3B) para la RTX 4090 Laptop de 16GB:
# - --cpu-moe: offloadea los tensores de los expertos MoE a RAM del sistema,
#   dejando en GPU las capas de atención y el experto compartido (-ngl 99);
# - 262.144 tokens (256K): contexto nativo máximo del modelo;
# - KV Q8 + Flash Attention para mantener bajo el consumo de VRAM del KV cache;
# - un único slot (-np 1) y reutilización de prefijos (--cache-reuse 256).
exec llama-server \
    --model "${MODEL_PATH}" \
    --host 0.0.0.0 \
    --port 8080 \
    --cpu-moe \
    -ngl 99 \
    -np 1 \
    --ctx-size "${LLAMA_CTX_SIZE_RAVENX:-262144}" \
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
