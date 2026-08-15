#!/bin/bash
set -e

# El backend de transferencia "Xet" (default en huggingface-hub moderno
# para repos grandes) tiene un bug conocido de colapso de congestión TCP
# en archivos grandes: el CDN entrega paquetes fuera de orden a gran
# escala, la ventana de congestión nunca se expande, y la descarga queda
# trabada para siempre sin tirar error. Forzamos el método HTTP clásico,
# que es más lento en teoría pero estable de punta a punta.
export HF_HUB_DISABLE_XET=1

MODEL_DIR="/models/qwen3.6-35b-a3b"
MODEL_FILE="Qwen3.6-35B-A3B-UD-Q4_K_M.gguf"
MODEL_PATH="${MODEL_DIR}/${MODEL_FILE}"

# mmproj: el "vision projector" que le da a llama-server la capacidad de
# procesar imágenes con este modelo. Sin este archivo, llama-server sirve
# el modelo igual, pero cualquier request con una imagen falla o la ignora.
MMPROJ_FILE="mmproj-BF16.gguf"
MMPROJ_PATH="${MODEL_DIR}/${MMPROJ_FILE}"

mkdir -p "${MODEL_DIR}"

if [ ! -f "${MODEL_PATH}" ]; then
    echo ">> Descargando ${MODEL_FILE} (~22.1GB, puede tardar varios minutos)..."
    hf download unsloth/Qwen3.6-35B-A3B-GGUF \
        "${MODEL_FILE}" \
        --local-dir "${MODEL_DIR}"
else
    echo ">> Modelo ya cacheado en ${MODEL_PATH}, omito descarga."
fi

if [ ! -f "${MMPROJ_PATH}" ]; then
    echo ">> Descargando ${MMPROJ_FILE} (vision projector)..."
    hf download unsloth/Qwen3.6-35B-A3B-GGUF \
        "${MMPROJ_FILE}" \
        --local-dir "${MODEL_DIR}"
else
    echo ">> mmproj ya cacheado en ${MMPROJ_PATH}, omito descarga."
fi

echo ">> Iniciando llama-server..."

# --cpu-moe: offloadea los tensores de los expertos MoE a RAM del sistema,
#   dejando en GPU la atención + el experto compartido. Es lo que permite
#   que un modelo de 35B (pesos totales) entre en una GPU de 16GB.
# -ngl 99: para las capas que SÍ van a GPU (atención), usar todas.
# --jinja: habilita el chat template embebido en el GGUF (necesario para
#   que el formato de tool-calling de Qwen3.6 funcione correctamente).
# -fa on: flash attention, reduce uso de VRAM del KV cache.
# -ctk/-ctv q8_0: cuantiza el KV cache también, ahorra VRAM extra.
#
# CTX_SIZE en 262144 (máximo del modelo, n_ctx_train=262144): mediciones
# reales confirmaron escala casi lineal y muy económica en VRAM gracias
# a --cpu-moe + KV cache cuantizado (-ctk/-ctv q8_0):
#   32768  tokens → 3.2GB VRAM
#   65536  tokens → 3.6GB VRAM  (+400MB)
#   131072 tokens → 4.5GB VRAM  (+900MB)
#   262144 tokens → ~6-7GB VRAM estimado, bien dentro de los 16GB
# Con 262144 de contexto el modelo puede leer codebases completos de
# varios miles de líneas sin fragmentar el análisis. Si aún así se
# queda corto, la siguiente estrategia es cambiar el approach del agente
# (análisis por fases/componentes) en vez de subir más el contexto.
#
# --cache-reuse 256: el flag más impactante para agent loops. Cuando el
#   agente manda requests sucesivos con el mismo system prompt + skills
#   (que son siempre idénticos turno a turno), el servidor reutiliza el
#   KV cache del prefijo en vez de reprocesarlo desde cero. Sin esto,
#   cada request de OpenHands paga el costo completo de ~4-5 minutos
#   de prefill para los 16k tokens de system prompt + skills, aunque
#   sean byte-a-byte idénticos al request anterior.
#
# --cache-ram 4096: tamaño del cache de prompts en RAM del sistema
#   (default: 8192 MiB desde oct 2025). Bajado desde los 16384 originales
#   al pasar a UD-Q4_K_M (22.1GB, vs 16.6GB de UD-Q3_K_M): medimos con
#   VmRSS/VmHWM que el pool real nunca creció más de ~2.5GB en uso normal,
#   así que 4GB deja margen de sobra sin arriesgar el total de RAM del
#   sistema (~30.5GB). Cuenta: 22.1GB (modelo) + 4GB (tope de cache) =
#   26.1GB, dejando ~4.4GB de colchón. Si en algún momento se ve cache
#   miss frecuente (prefijos largos de OpenHands eviccionándose entre
#   sí), es la primera variable para subir — pero solo si la RAM nueva
#   finalmente llega.
#
# --ubatch-size 1024: mejora el throughput de prefill en prompts largos
#   (decenas de miles de tokens) al correr las unidades de matmul con
#   micro-batches más grandes, más cerca de su pico de rendimiento.
#
# NOTA: Qwen3.6 viene con "thinking mode" activado por default (razona
# en un bloque <think> antes de responder). Para un agente de coding
# esto puede sumar latencia o, en el peor caso, interferir con el
# parseo de tool-calls. Si se nota lento o falla el tool-calling,
# agregar: --chat-template-kwargs '{"enable_thinking":false}'
#
# --repeat-penalty 1.1 + --repeat-last-n 256: penaliza tokens que ya
#   aparecieron en los últimos 256 tokens generados. Se agrega después
#   de ver un caso real de looping: una tarea de OpenHands generó
#   ~124k tokens sin converger a una respuesta final, hasta pegar
#   contra el techo del --ctx-size (n_tokens=262143, truncated=1 en
#   los logs). min_p 0.0 no da ninguna protección anti-repetición por
#   sí solo; este flag es la mitigación directa a ese síntoma. 1.1 es
#   un valor conservador (penalización leve) para no distorsionar la
#   calidad de respuestas normales; si se siguen viendo loops largos,
#   subir gradualmente (1.15, 1.2).
# --mmproj: habilita el soporte de visión (JPG/PNG) vía el vision projector
#   descargado arriba. AVISO: hay reportes de la comunidad de segfaults al
#   procesar imágenes con este modelo puntual en versiones de llama.cpp de
#   abril 2026 — probar con cautela, puede que ya esté resuelto o puede que
#   no. Si el server crashea al mandar una imagen, ese es el sospechoso #1
#   a revisar (buscar issues abiertos en llama.cpp sobre Qwen3.6 + mmproj).
exec llama-server \
    --model "${MODEL_PATH}" \
    --mmproj "${MMPROJ_PATH}" \
    --host 0.0.0.0 \
    --port 8080 \
    --cpu-moe \
    -ngl 99 \
    --ctx-size "${LLAMA_CTX_SIZE:-262144}" \
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
    --repeat-penalty 1.1 \
    --repeat-last-n 256
