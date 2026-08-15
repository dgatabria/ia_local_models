#!/bin/bash
set -e

# Mismo motivo que en el llm_server original: evitamos el backend "Xet"
# de huggingface-hub por su bug de colapso de congestión TCP en archivos
# grandes.
export HF_HUB_DISABLE_XET=1

MODEL_DIR="/models/qwen3.6-35b-a3b-uncensored"
MODEL_FILE="Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-Q4_K_M.gguf"
MODEL_PATH="${MODEL_DIR}/${MODEL_FILE}"

# mmproj propio de este modelo (no es el mismo archivo que el del modelo
# original, aunque cumple la misma función).
MMPROJ_FILE="mmproj-Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive-f16.gguf"
MMPROJ_PATH="${MODEL_DIR}/${MMPROJ_FILE}"

mkdir -p "${MODEL_DIR}"

if [ ! -f "${MODEL_PATH}" ]; then
    echo ">> Descargando ${MODEL_FILE} (~21GB, puede tardar varios minutos)..."
    hf download HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive \
        "${MODEL_FILE}" \
        --local-dir "${MODEL_DIR}"
else
    echo ">> Modelo ya cacheado en ${MODEL_PATH}, omito descarga."
fi

if [ ! -f "${MMPROJ_PATH}" ]; then
    echo ">> Descargando ${MMPROJ_FILE} (vision projector, ~899MB)..."
    hf download HauhauCS/Qwen3.6-35B-A3B-Uncensored-HauhauCS-Aggressive \
        "${MMPROJ_FILE}" \
        --local-dir "${MODEL_DIR}"
else
    echo ">> mmproj ya cacheado en ${MMPROJ_PATH}, omito descarga."
fi

echo ">> Iniciando llama-server (variante uncensored)..."

# NOTA DE VRAM: esta instancia corre en paralelo (proceso separado) al
# llm_server original, ambos compitiendo por la misma GPU de 16GB. A
# diferencia del multi-slot que evaluamos para OpenHands (un solo proceso
# repartiendo un --ctx-size entre varias conversaciones), esto son DOS
# procesos de llama-server independientes, cada uno con su propia
# asignación de VRAM. Por eso:
#   - CTX_SIZE default bajado a 65536 (vs. 262144 del original): de sobra
#     para charla casual, pero pesa mucho menos en VRAM (~3.5GB estimado
#     según la escala que ya midieron ustedes: 32768→3.2GB, 65536→3.6GB).
#   - --cache-ram bajado a 8192 (vs. 16384): este uso no necesita sostener
#     prefijos larguísimos en cache como un agente de coding.
# Si en algún momento querés contexto completo acá, subilo con la env var
# LLAMA_CTX_SIZE_UNCENSORED — pero entonces NO conviene tener las dos
# instancias cargadas en VRAM al mismo tiempo (mejor `docker compose stop
# llm_server` mientras usás esta, o viceversa).
#
# Mismos flags que el original por lo demás: --cpu-moe (misma arquitectura
# MoE, mismo truco de offload de expertos a RAM), -ngl 99, flash-attn +
# KV cache cuantizado, --jinja para el chat template, y el mismo
# --repeat-penalty que agregamos después del incidente de looping.
# Settings recomendados por los propios autores de Qwen para modo
# "thinking" general (según la ficha del modelo): temp=1.0, top_p=0.95,
# top_k=20, min_p=0, presence_penalty=1.5. Sumamos --repeat-penalty como
# red de seguridad extra (mismo criterio que en el llm_server original,
# tras el incidente de looping que tuvimos).
exec llama-server \
    --model "${MODEL_PATH}" \
    --mmproj "${MMPROJ_PATH}" \
    --host 0.0.0.0 \
    --port 8080 \
    --cpu-moe \
    -ngl 99 \
    --ctx-size "${LLAMA_CTX_SIZE_UNCENSORED:-65536}" \
    --flash-attn on \
    -ctk q8_0 \
    -ctv q8_0 \
    --jinja \
    --cache-reuse 256 \
    --cache-ram 8192 \
    --ubatch-size 1024 \
    --temp 1.0 \
    --top-p 0.95 \
    --top-k 20 \
    --min-p 0.0 \
    --presence-penalty 1.5 \
    --repeat-penalty 1.1 \
    --repeat-last-n 256
