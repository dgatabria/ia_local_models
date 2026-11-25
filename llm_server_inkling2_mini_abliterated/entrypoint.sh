#!/bin/bash
set -euo pipefail

# El backend Xet dio descargas inestables con GGUF grandes en este entorno.
export HF_HUB_DISABLE_XET=1

# Inkling 2 Mini abliterated (orcarouter), MoE 26.4B / 3.4B activos (5.4B
# efectivos: las capas 6-17 corren 3 pasadas con pesos compartidos).
# Variante Mix-16G (15.72GB): no entra entera en los 16GB de VRAM, así que
# los expertos de las capas NO loopeadas van a RAM (ver CPU_EXPERTS_REGEX).
#
# El repo de orcarouter es gated: el GGUF se baja a mano en el host y se
# monta vía ~/ai_models. Este contenedor NO lo descarga nunca.
MODEL_PATH="${INKLING_MODEL_PATH:-/models/inkling-2-mini-abliterated/Inkling-2-Mini-abliterated-Mix-16G.gguf}"

# El repo de orcarouter no trae mmproj. El proyector de visión no cambia con
# la abliteración, así que usamos el de ggml-org (público, ~0.35GB).
MMPROJ_REPO="ggml-org/Inkling-2-Mini-GGUF"
MMPROJ_FILE="mmproj-Inkling-2-Mini-Q8_0.gguf"
MMPROJ_DIR="${INKLING_MMPROJ_DIR:-/models/inkling-2-mini-abliterated}"
MMPROJ_PATH="${MMPROJ_DIR}/${MMPROJ_FILE}"

if [ ! -f "${MODEL_PATH}" ]; then
    echo "[X] No encuentro el GGUF en ${MODEL_PATH}." >&2
    echo "    Bajalo a mano (repo gated) y/o ajustá INKLING_MODEL_PATH (ruta dentro del contenedor, ~/ai_models = /models)." >&2
    exit 2
fi
echo ">> Modelo: ${MODEL_PATH}"

VISION="${INKLING_VISION:-cpu}"
MMPROJ_ARGS=()
case "${VISION}" in
    off|0|no|false)
        MMPROJ_ARGS=(--no-mmproj)
        ;;
    cpu|gpu)
        mkdir -p "${MMPROJ_DIR}"
        if [ ! -f "${MMPROJ_PATH}" ]; then
            echo ">> Descargando ${MMPROJ_FILE} desde ${MMPROJ_REPO} (~0.35GB)..."
            hf download "${MMPROJ_REPO}" "${MMPROJ_FILE}" --local-dir "${MMPROJ_DIR}"
        else
            echo ">> mmproj ya cacheado en ${MMPROJ_PATH}, omito descarga."
        fi
        MMPROJ_ARGS=(--mmproj "${MMPROJ_PATH}")
        # Encoder en CPU por defecto (mismo criterio que VISION=cpu del coder):
        # cada MiB de VRAM va a la KV cache.
        [ "${VISION}" = "cpu" ] && MMPROJ_ARGS+=(--no-mmproj-offload)
        ;;
    *)
        echo "[X] INKLING_VISION=${VISION} no es off, cpu ni gpu." >&2
        exit 2
        ;;
esac

# Expertos MoE que van a RAM. SOLO capas no loopeadas (0-5 y 18-23): el issue
# llama.cpp#32710 (abierto en el commit pineado) da salida basura/NaN en CUDA
# cuando los expertos de las capas en loop (6-17) quedan en host.
#   default  0-5 + 18-23 -> 6703 MiB a RAM (medido en el GGUF), ~8.1 GiB quedan en GPU
#   (contexto y ubatch medidos: ver comentario en docker-compose.yml)
#   alternativa más rápida (3422 MiB a RAM, menos contexto): blk\.(1[89]|2[0-3])\.ffn_.*_exps\.=CPU
CPU_EXPERTS_REGEX="${INKLING_CPU_EXPERTS_REGEX:-blk\.([0-5]|1[89]|2[0-3])\.ffn_.*_exps\.=CPU}"

CTX="${LLAMA_CTX_SIZE_INKLING:-237568}"
FLASH_ATTN="${INKLING_FLASH_ATTN:-on}"
N_PREDICT="${INKLING_N_PREDICT:-32768}"
UBATCH="${INKLING_UBATCH:-64}"

echo ">> Iniciando llama-server (Inkling 2 Mini abliterated, contexto ${CTX}, visión ${VISION}, fa ${FLASH_ATTN})..."
echo ">> Expertos en CPU: ${CPU_EXPERTS_REGEX}"

# - -ngl 99 + --fit off: TODO lo que no matchee el regex va a GPU. Si no
#   entra, preferimos un OOM claro antes que el auto-fit mande capas
#   loopeadas enteras a CPU (cae en el bug #32710 y genera basura).
# - KV: 8 aplicaciones de atención global (4 capas, 2 de ellas en loop x3)
#   x 8 KV heads x 128 dim -> ~17 KiB/token en q8_0 (~32 KiB en f16). Las 40
#   aplicaciones SWA tienen ventana fija de 1024 tokens (no crecen con CTX).
#   La KV cuantizada en V requiere flash-attn.
# - -np 1: un único slot, todo el contexto para una conversación.
# - Sampling recomendado por Thinking Machines / Unsloth: temp 1.0, top_p 1.0,
#   top_k 0, min_p 0. Sin penalties de repetición (rompen el uso agéntico).
# - --n-predict acotado: la ficha oficial advierte bucles de razonamiento y
#   recomienda limitar tokens de salida.
# shellcheck disable=SC2086  # INKLING_EXTRA_ARGS se separa en palabras a propósito
exec llama-server \
    --model "${MODEL_PATH}" \
    "${MMPROJ_ARGS[@]}" \
    --alias inkling-2-mini-abliterated \
    --host 0.0.0.0 \
    --port 8080 \
    -np 1 \
    --ctx-size "${CTX}" \
    -ngl 99 \
    --fit off \
    -ot "${CPU_EXPERTS_REGEX}" \
    --flash-attn "${FLASH_ATTN}" \
    -ctk q8_0 \
    -ctv q8_0 \
    --jinja \
    --cache-reuse 256 \
    --cache-ram 4096 \
    --ubatch-size "${UBATCH}" \
    --n-predict "${N_PREDICT}" \
    --metrics \
    --temp 1.0 \
    --top-p 1.0 \
    --top-k 0 \
    --min-p 0.0 \
    --presence-penalty 0.0 \
    --repeat-penalty 1.0 \
    --dry-multiplier 0.0 \
    ${INKLING_EXTRA_ARGS:-}
