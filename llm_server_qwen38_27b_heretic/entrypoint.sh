#!/bin/bash
set -euo pipefail

# El backend Xet dio descargas inestables con GGUF grandes en este entorno.
export HF_HUB_DISABLE_XET=1

# Qwen3.8-27B descensurado con Heretic (ARA), KL 0.0085 vs. base según el
# autor. Reemplaza a llm_server_uncensored (Qwen3.6-35B-A3B "Aggressive"),
# que alucinaba y se loopeaba en uso agéntico.
REPO="0bserverx/Qwen3.8-27B-Heretic-Abliterated-Uncensored-GGUF"
MODEL_DIR="/models/qwen3.8-27b-heretic"
# Q3_K_M multilingual (13.3GB): mismo tamaño que el UD-Q3_K_XL que ya corre
# en llm_server_qwen38_27b con ~80K de contexto. La variante "-vision" del
# repo pesa 16.2GB y no entra en 16GB de VRAM.
MODEL_FILE="RVN-Q3_K_M-multilingual.gguf"
MODEL_PATH="${MODEL_DIR}/${MODEL_FILE}"
MMPROJ_FILE="mmproj-Qwen3.8-27B-Q8_0.gguf"
MMPROJ_PATH="${MODEL_DIR}/${MMPROJ_FILE}"

mkdir -p "${MODEL_DIR}"

if [ ! -f "${MODEL_PATH}" ]; then
    echo ">> Descargando ${MODEL_FILE} (~13.3GB, puede tardar varios minutos)..."
    hf download "${REPO}" "${MODEL_FILE}" --local-dir "${MODEL_DIR}"
else
    echo ">> Modelo ya cacheado en ${MODEL_PATH}, omito descarga."
fi

if [ ! -f "${MMPROJ_PATH}" ]; then
    echo ">> Descargando ${MMPROJ_FILE} (vision projector, ~630MB)..."
    hf download "${REPO}" "${MMPROJ_FILE}" --local-dir "${MODEL_DIR}"
else
    echo ">> mmproj ya cacheado en ${MMPROJ_PATH}, omito descarga."
fi

# El encoder de visión corre en CPU por defecto: el modelo denso + 80K de
# KV ya ocupan casi toda la VRAM. Con MMPROJ_GPU=1 se carga en GPU
# (encodea imágenes más rápido, pero resta ~0.7GB de VRAM).
MMPROJ_OFFLOAD_FLAG="--no-mmproj-offload"
if [ "${MMPROJ_GPU:-0}" = "1" ]; then
    MMPROJ_OFFLOAD_FLAG=""
fi

CTX="${LLAMA_CTX_SIZE_QWEN38_HERETIC:-81920}"
echo ">> Iniciando llama-server (Qwen3.8-27B Heretic, Q3_K_M, contexto ${CTX})..."

# - Denso: sin --cpu-moe, un único slot (-np 1) para que los 80K de
#   contexto sean de una sola conversación.
# - VRAM (medido): con las 65 capas en GPU y 81920 de contexto pide
#   ~15.5GB de 15.5GB libres (-ngl 99 daba OOM). En vez de -ngl fijo, usamos
#   el auto-fit de llama.cpp con 512MiB de margen: quedan 62/65 capas en GPU
#   (3 en CPU, penalidad chica de velocidad) y el contexto intacto.
#   ubatch 512 (no 1024) achica el compute buffer para que entren más capas.
# - KV Q8 + flash-attn, igual que llm_server_qwen38_27b.
# - Sampling de Qwen para coding (temp 0.6, top_p 0.95, top_k 20) y SIN
#   penalties de repetición (presence/repeat/DRY): ya vimos con el
#   uncensored anterior que rompen el uso agéntico (rutas y comandos
#   repiten tokens a propósito).
# shellcheck disable=SC2086
exec llama-server \
    --model "${MODEL_PATH}" \
    --mmproj "${MMPROJ_PATH}" \
    ${MMPROJ_OFFLOAD_FLAG} \
    --alias qwen3.8-27b-heretic \
    --host 0.0.0.0 \
    --port 8080 \
    -np 1 \
    --ctx-size "${CTX}" \
    --flash-attn on \
    -ctk q8_0 \
    -ctv q8_0 \
    --jinja \
    --cache-reuse 256 \
    --cache-ram 4096 \
    --ubatch-size 512 \
    --fit-target 512 \
    --metrics \
    --temp 0.6 \
    --top-p 0.95 \
    --top-k 20 \
    --min-p 0.0 \
    --presence-penalty 0.0 \
    --repeat-penalty 1.0 \
    --dry-multiplier 0.0
