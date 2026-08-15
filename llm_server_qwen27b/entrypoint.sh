#!/bin/bash
set -e

export HF_HUB_DISABLE_XET=1

MODEL_DIR="/models/qwen3.6-27b"
MODEL_FILE="Qwen3.6-27B-UD-Q3_K_XL.gguf"
MODEL_PATH="${MODEL_DIR}/${MODEL_FILE}"

mkdir -p "${MODEL_DIR}"

if [ ! -f "${MODEL_PATH}" ]; then
    echo ">> Descargando ${MODEL_FILE} (14.5GB confirmado, puede tardar varios minutos)..."
    hf download unsloth/Qwen3.6-27B-GGUF \
        "${MODEL_FILE}" \
        --local-dir "${MODEL_DIR}"
else
    echo ">> Modelo ya cacheado en ${MODEL_PATH}, omito descarga."
fi

echo ">> Iniciando llama-server (Qwen3.6-27B, denso, híbrido Gated DeltaNet)..."

# SIN VISIÓN A PROPÓSITO: los 14.5GB de pesos + KV cache ya casi llenan
# los 16GB de VRAM — sumarle el mmproj (~931MB) causaba un OOM real al
# arrancar (cudaMalloc failed: out of memory). Como esta instancia se
# evalúa por calidad de código, no por leer imágenes, no hace falta.
#
# --ctx-size 32768 de entrada (no 262144 como en las otras instancias):
# la propia recomendación oficial para uso local es 32k-128k para
# conservar VRAM/RAM con este modelo. Subilo gradualmente si el margen
# real (medido con VmRSS, igual que ya hicimos antes) lo permite — pero
# con la VRAM tan ajustada (14.5GB de pesos solos, sin mmproj), no
# esperaría poder subirlo mucho.
#
# -ngl 99: TODAS las capas a GPU (a diferencia del setup con --cpu-moe,
# acá no hay nada que dejar en RAM — o entra en VRAM o hay que bajar a
# offload parcial, mucho más lento en un denso porque cada token toca
# TODAS las capas, no solo unos pocos expertos).
#
# CAVEAT DE VRAM: si esta instancia corre en simultáneo con
# llm_server (original) y/o llm_server_uncensored, es MUY probable que no
# entren juntas en tus 16GB — a diferencia de la variante uncensored
# (que compartía la arquitectura MoE liviana en VRAM), esta se come la
# VRAM ella sola. Probablemente necesites `docker compose stop
# llm_server llm_server_uncensored` mientras usás esta.
#
# CAVEATS CONOCIDOS (agosto 2026): tu driver corre CUDA 13.2 — la propia
# doc de Unsloth marca esa versión puntual como sospechosa de generar
# salida en gibberish para este modelo. Si ves texto roto (no relacionado
# a un OOM), es la primera variable a revisar. --chat-template-kwargs
# preserve_thinking también está roto en este GGUF (issue #22255 de
# llama.cpp, funciona bien en el 35B-A3B) — no lo activamos.
exec llama-server \
    --model "${MODEL_PATH}" \
    --host 0.0.0.0 \
    --port 8080 \
    -ngl 99 \
    -np 1 \
    --ctx-size "${LLAMA_CTX_SIZE_27B:-32768}" \
    --flash-attn on \
    -ctk q8_0 \
    -ctv q8_0 \
    --jinja \
    --cache-reuse 256 \
    --cache-ram 4096 \
    --ubatch-size 1024 \
    --temp 0.6 \
    --top-p 0.95 \
    --top-k 20 \
    --min-p 0.0 \
    --presence-penalty 1.5 \
    --repeat-penalty 1.1 \
    --repeat-last-n 256
