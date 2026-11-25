#!/bin/sh
# Entrypoint para levantar Qwen3.8-Flash-Next-GSQ-RCO-Coder con Strata.
# Basado en el entrypoint oficial de Strata v0.2.9 (/opt/strata/entrypoint.sh),
# preconfigurado para usar los shards locales en /models/Qwen3.8-Flash-Next-GSQ-RCO-Coder.
#
# Variables de entorno configurables (con defaults para este contenedor):
#   FAMILY                familia del catálogo de Strata (default: coder)
#   MODEL                 cuantización del modelo (default: IQ1_M)
#   MODEL_PATH            ruta dentro del contenedor con los shards .gguf (default: /models/Qwen3.8-Flash-Next-GSQ-RCO-Coder)
#   CONTEXT               tamaño de contexto por slot: 32k, 64k, 128k, 256k o tokens (default: 262144 / 256k; también acepta STRATA_CTX_SIZE)
#   PARALLEL              slots / requests concurrentes 1..8 (default: 1 -> 1 único slot de 262144 tokens)
#   VISION                off | on | gpu | cpu (default: cpu; requiere mmproj-Qwen3.8-Flash-Next-BF16.gguf en MODEL_PATH)
#   KV                    formato de KV cache: int8 | q4_0 | k8v4 | fp16 (default: int8)
#   TUNE                  quick | thorough | skip (default: skip para no demorar el primer arranque ~20 min)
#   PORT                  puerto interno del servidor HTTP OpenAI-compatible (default: 8080)
#   NO_API_KEY            1 para deshabilitar autenticación por API key en red local/Docker (default: 1)
#   API_KEY / API_KEY_FILE si se define, exige esa API key e ignora NO_API_KEY
#   STRATA_ALLOWED_HOSTS  hosts permitidos en cabecera Host cuando corre sin API key (default: *)
#   PRESET                nombre del preset persistido en /data (default: docker)
#   STRATA_SET            flags avanzados KEY=VALUE separados por espacio para `strata launch --set`
set -u

APP=/opt/strata
STRATA="$APP/terminal/bin/strata"
PYBIN="$APP/terminal/python/bin/python3"; [ -x "$PYBIN" ] || PYBIN=$(command -v python3 || echo python3)

# Defaults específicos para Qwen3.8-Flash-Next-GSQ-RCO-Coder
export FAMILY="${FAMILY:-coder}"
export MODEL="${MODEL:-IQ1_M}"
export MODEL_PATH="${MODEL_PATH:-/models/Qwen3.8-Flash-Next-GSQ-RCO-Coder}"
export CONTEXT="${CONTEXT:-${STRATA_CTX_SIZE:-262144}}"
export PARALLEL="${PARALLEL:-${STRATA_PARALLEL_CODER:-1}}"
export KV="${KV:-int8}"
export VISION="${VISION:-cpu}"
export TUNE="${TUNE:-skip}"
export NO_API_KEY="${NO_API_KEY:-1}"
export STRATA_ALLOWED_HOSTS="${STRATA_ALLOWED_HOSTS:-*}"

STATE_DIR="${XDG_STATE_HOME:-/data/state}/strata"
KEY_FILE="${STRATA_KEY_FILE:-/data/state/api-key}"
PHASE_FILE=/run/strata-phase
PRESET="${PRESET:-docker}"
PORT="${PORT:-8080}"

say()  { printf '%s\n' "$*"; }
warn() { printf '[!] %s\n' "$*" >&2; }
die()  { printf '[X] %s\n' "$*" >&2; exit "${2:-1}"; }
phase() { printf '%s\n' "$1" > "$PHASE_FILE" 2>/dev/null || true; }

legacy_models_guard() {
  [ -n "$1" ] && return 0
  stuck=$(find "$2" -maxdepth 3 -name '*.gguf' 2>/dev/null | head -1)
  [ -n "$stuck" ] || return 0
  msg="There are model files in ${stuck%/*} (the older image's place). This image keeps its models in /data/Strata-data/models and does not read them there, so it would download the model again (tens of GB). To use them: mount that folder read-only and set MODEL_PATH to the folder that holds the shards, or put them in /data/Strata-data/models/<model folder>/. To download anyway: set ALLOW_DOWNLOAD=1. Nothing was downloaded."
  if [ "${ALLOW_DOWNLOAD:-0}" = "1" ]; then warn "$msg Going on, because ALLOW_DOWNLOAD=1."; else die "$msg" 2; fi
}

# Permite ejecutar subcomandos arbitrarios: `docker run ... strata ...` o `docker run ... sh`
if [ "$#" -gt 0 ] && [ "$1" != "run" ] && [ "$1" != "serve" ]; then
  case "$1" in strata) shift; exec "$STRATA" "$@" ;; *) exec "$@" ;; esac
fi

# Si /data no está montado como volumen independiente pero existe el directorio del modelo en /models,
# redirigimos /data hacia ${MODEL_PATH}/strata_data para no perder el pack de expertos (~23.4 GB) ni el MTP (~6 GB).
if ! grep -q " /data " /proc/self/mountinfo 2>/dev/null && [ -d "${MODEL_PATH}" ] && [ -w "${MODEL_PATH}" ]; then
  PERSIST_DATA="${MODEL_PATH}/strata_data"
  mkdir -p "$PERSIST_DATA/state" "$PERSIST_DATA/home" "$PERSIST_DATA/install" "$PERSIST_DATA/Strata-data" "$PERSIST_DATA/config" 2>/dev/null || true
  mkdir -p /data 2>/dev/null || true
  for sub in state home install Strata-data config; do
    if [ ! -L "/data/$sub" ]; then
      rm -rf "/data/$sub" 2>/dev/null || true
      ln -s "$PERSIST_DATA/$sub" "/data/$sub"
    fi
  done
  say ">> /data vinculado a ${PERSIST_DATA} para persistir packs de expertos, MTP y configuración."
fi

mkdir -p /data/state /data/home /data/install /data/Strata-data /data/config /run 2>/dev/null || die "/data is not writable. Mount a volume there: -v strata-data:/data"

# Vincula el seed de llama.cpp (gguf-py) incluido en la imagen para que el primer arranque no descargue nada de GitHub
if [ -d /opt/strata/seed/third_party/llama.cpp ] && [ ! -e /data/install/third_party/llama.cpp ]; then
  mkdir -p /data/install/third_party && ln -s /opt/strata/seed/third_party/llama.cpp /data/install/third_party/llama.cpp
fi

if ! grep -q " /data " /proc/self/mountinfo 2>/dev/null && [ ! -L "/data/Strata-data" ]; then
  warn "/data is not a volume, so the prepared expert pack and your settings are lost when this container is removed. Add: -v strata-data:/data"
fi

# ---- Advertencias de entorno
cpuinfo="${STRATA_CPUINFO:-/proc/cpuinfo}"
if [ -r "$cpuinfo" ] && grep -q '^flags' "$cpuinfo" && ! grep -m1 '^flags' "$cpuinfo" | grep -qw avx2; then
  warn "This computer's CPU has no AVX2. The engine is built for CPUs with AVX2 and may stop with 'illegal instruction'."
fi
limit=$(cat /sys/fs/cgroup/memory.max 2>/dev/null || echo max)
case "$limit" in max|"") ;; *[!0-9]*) ;; *)
  gb=$((limit / 1073741824))
  warn "This container has a memory limit of ${gb} GB. Strata plans from the host's RAM (/proc/meminfo) and loads tens of GB, so a low limit can end in the container being killed. Raise or remove the limit."
esac
if [ -n "${API_KEY:-}" ] && [ "${#API_KEY}" -lt 16 ]; then
  warn "API_KEY is short (${#API_KEY} characters). Use 16 or more; 'openssl rand -hex 24' makes a good one."
fi

# ---- Limpieza de configuraciones de ejecución generadas por otra versión del motor
for c in /data/install/strata-*.json; do
  [ -f "$c" ] || continue
  gone=$("$PYBIN" -c '
import json, os, sys
def walk(v):
    if isinstance(v, str):
        yield v
    elif isinstance(v, dict):
        for x in v.values():
            yield from walk(x)
    elif isinstance(v, list):
        for x in v:
            yield from walk(x)
try:
    cfg = json.load(open(sys.argv[1], encoding="utf-8-sig"))
except (OSError, ValueError):
    sys.exit(0)
for s in walk(cfg):
    if s.startswith("/opt/strata/components/") and not os.path.exists(s):
        print(s)
        break
' "$c" 2>/dev/null)
  if [ -n "$gone" ]; then
    say "The saved run settings $(basename "$c") name $gone, which this image does not have: making them again. The model files are kept."
    rm -f "$c"
  fi
done

# ---- Verificación de motor y GPU
phase checking
engine_json=$("$STRATA" components engine --output json 2>/dev/null || true)
if ! printf '%s' "$engine_json" | grep -q '"usable": *true'; then
  echo "[X] The engine in this image does not match this computer's graphics card, or the card is not visible to the container." >&2
  "$STRATA" components engine 2>&1 | sed 's/^/    /' >&2 || true
  exit 3
fi

backend=$(printf '%s' "$engine_json" | sed -n 's/.*"backend": *"\([a-z0-9]*\)".*/\1/p' | head -1)
dev="${STRATA_DEV_DIR:-/dev}"
case "$backend" in
  cuda) ls "$dev"/nvidia[0-9]* >/dev/null 2>&1 || [ -e "$dev/dxg" ] || { echo "[X] NVIDIA GPU not visible inside the container. Start with --gpus all / deploy.resources.reservations.devices." >&2; exit 3; } ;;
  hip)  [ -e "$dev/kfd" ] || { echo "[X] AMD /dev/kfd not visible inside the container." >&2; exit 3; } ;;
  sycl) ls "$dev"/dri/renderD* >/dev/null 2>&1 || { echo "[X] Intel /dev/dri not visible inside the container." >&2; exit 3; } ;;
esac

# ---- Configuración de API key
key="${API_KEY:-}"
if [ -z "$key" ] && [ -n "${API_KEY_FILE:-}" ]; then
  [ -r "$API_KEY_FILE" ] || die "API_KEY_FILE=$API_KEY_FILE cannot be read."
  key=$(tr -d '\r\n' < "$API_KEY_FILE")
fi

# ---- Construcción de flags para `strata launch`
set -- launch --no-tui --access lan --port "$PORT" --data-dir /data/Strata-data
have=""

preset_exists=""
if "$STRATA" launch --list-presets --output json 2>/dev/null | grep -q "\"name\": *\"$PRESET\""; then preset_exists=1; fi

model_path="${MODEL_PATH:-${GGUF_DIR:-}}"
if [ -n "$model_path" ]; then
  if [ ! -e "$model_path" ]; then
    die "MODEL_PATH=$model_path is not in the container. Mount your models folder (e.g. -v ~/ai_models:/models). Nothing was downloaded." 2
  elif [ -d "$model_path" ] && ! ls "$model_path"/*.gguf >/dev/null 2>&1; then
    sub=$(find "$model_path" -maxdepth 2 -name '*.gguf' 2>/dev/null | head -1)
    if [ -n "$sub" ]; then
      die "MODEL_PATH=$model_path has no .gguf files directly in it, but there are some in ${sub%/*}. Set MODEL_PATH=${sub%/*}." 2
    fi
    die "MODEL_PATH=$model_path holds no .gguf files. Mount the folder with the model's shards (Qwen3.8-Flash-Next-GSQ-RCO-IQ1_M-0000*-of-00002.gguf)." 2
  fi
fi
legacy_models_guard "$model_path" "${STRATA_LEGACY_MODELS:-/data/models}"
[ -n "${MODEL:-}" ]       && { set -- "$@" --model "$MODEL"; have=1; }
[ -n "${FAMILY:-}" ]      && { set -- "$@" --family "$FAMILY"; have=1; }
[ -n "$model_path" ]      && { set -- "$@" --model-path "$model_path"; have=1; }
[ -n "${CONTEXT:-}" ]     && { set -- "$@" --context "$CONTEXT"; have=1; }
[ -n "${PARALLEL:-}" ] && [ "${PARALLEL}" != "auto" ] && { set -- "$@" --parallel "$PARALLEL"; have=1; }
[ -n "${KV:-}" ]          && { set -- "$@" --kv "$KV"; have=1; }
if [ -n "${VISION:-}" ]; then
  case "$(printf '%s' "$VISION" | tr 'A-Z' 'a-z')" in
    off|no|false|0) v=off ;; on|yes|true|1) v=on ;; gpu) v=gpu ;; cpu) v=cpu ;;
    *) die "VISION=$VISION is not one of off, on, gpu, cpu." 2 ;;
  esac
  set -- "$@" --vision "$v"; have=1
fi
gpus="${GPUS:-${GPU:-}}"
case "$gpus" in ""|all|ALL) ;; *)
  set -- "$@" --gpu "$gpus"; have=1
  want=$(printf '%s' "$gpus" | tr -d ' ')
  for c in /data/install/strata-*.json; do
    [ -f "$c" ] || continue
    cur=$("$PYBIN" -c 'import json, sys; g = json.load(open(sys.argv[1])).get("gpu"); print(",".join(map(str, g)) if isinstance(g, list) else ("" if g is None else g))' "$c" 2>/dev/null)
    if [ "$cur" != "$want" ]; then
      say "The saved run settings use graphics card '${cur:-default}', GPU/GPUS asks for '$want': making them again (model files are kept)."
      rm -f "$c"
    fi
  done ;;
esac
roles=$(printf '%s' "${GPU_ROLES:-}" | tr -d ' ')
if [ -n "$roles" ]; then
  case "$gpus" in ""|all|ALL) ;; *) die "Set GPU_ROLES or GPU / GPUS, not both." 2 ;; esac
  set -- "$@" --gpu-roles "$roles"; have=1
fi
[ -n "${STRATA_SET:-}" ] && { for kv in $STRATA_SET; do set -- "$@" --set "$kv"; done; have=1; }

if [ -n "$key" ]; then
  set -- "$@" --api-key-env STRATA_LAUNCH_KEY
  have=1
elif [ "${NO_API_KEY:-1}" = "1" ] || [ "${NO_API_KEY:-}" = "true" ]; then
  set -- "$@" --no-api-key
  have=1
fi

# ---- Tuning opcional (por defecto TUNE=skip)
tune_mode=$(printf '%s' "${TUNE:-}" | tr 'A-Z' 'a-z')
if [ -n "$tune_mode" ] && [ "$tune_mode" != "skip" ]; then
  tuned=""
  if [ -n "$preset_exists" ] && "$STRATA" launch --list-presets 2>/dev/null | grep -E "^$PRESET[[:space:]]" | grep -qv "not tuned"; then tuned=1; fi
  if [ -n "$tuned" ] && [ "${RETUNE:-0}" != "1" ]; then
    say "This preset is already tuned; keeping the tuned settings (set RETUNE=1 to tune again)."
  else
    set -- "$@" --tune "$tune_mode"; have=1
    say "Tuning is on (TUNE=$tune_mode): the first start takes longer (up to about $([ "$tune_mode" = quick ] && echo 20 || echo 120) minutes), once."
  fi
elif [ "$tune_mode" = "skip" ]; then
  set -- "$@" --tune skip; have=1
fi
[ "${TUNE_ALLOW_SMALL:-0}" = "1" ] && set -- "$@" --tune-allow-small

if [ -n "$preset_exists" ]; then
  set -- "$@" --preset "$PRESET"
  [ -n "$have" ] && set -- "$@" --save-preset "$PRESET"
elif [ -n "$have" ]; then
  set -- "$@" --save-preset "$PRESET"
else
  set -- "$@" --save-preset "$PRESET"
fi

# ---- Arranque y manejo de señales (SIGTERM / SIGINT)
stopping=""
launch_pid=""
on_term() {
  stopping=1
  phase stopping
  say "Stop requested: shutting down Strata cleanly."
  if [ -n "$launch_pid" ] && kill -0 "$launch_pid" 2>/dev/null; then
    kill -INT "$launch_pid" 2>/dev/null
    i=0; while kill -0 "$launch_pid" 2>/dev/null && [ "$i" -lt 30 ]; do sleep 1; i=$((i+1)); done
    kill -TERM "$launch_pid" 2>/dev/null || true
  fi
  "$STRATA" launch --stop --preset "$PRESET" || true
  exit 0
}
trap on_term TERM INT

RESET_INT='import os, signal, sys; signal.signal(signal.SIGINT, signal.SIG_DFL); os.execv(sys.argv[1], sys.argv[1:])'

phase preparing
what="${FAMILY}/${MODEL}${model_path:+ desde $model_path} (${PARALLEL} slots x ${CONTEXT} contexto por slot)"
say ">> Iniciando Strata: $what [preset '$PRESET']..."
if [ -n "$key" ]; then
  STRATA_LAUNCH_KEY="$key" "$PYBIN" -c "$RESET_INT" "$STRATA" "$@" &
else
  "$PYBIN" -c "$RESET_INT" "$STRATA" "$@" &
fi
launch_pid=$!
wait "$launch_pid"
rc=$?
launch_pid=""
[ -n "$stopping" ] && exit 0
if [ "$rc" -ne 0 ]; then
  phase failed
  die "strata launch stopped with code $rc (the lines above say why)." "$rc"
fi

# Guarda la API key si está activa para que healthcheck.sh y `strata monitor` puedan leerla
if [ -z "$key" ] && { [ "${NO_API_KEY:-1}" = "1" ] || [ "${NO_API_KEY:-}" = "true" ]; }; then
  rm -f "$KEY_FILE"
  say ">> Autenticación por API key deshabilitada (--no-api-key) para compatibilidad con contenedores locales."
else
  ( umask 077; "$STRATA" launch --show-key --preset "$PRESET" > "$KEY_FILE" 2>/dev/null ) || true
  [ -s "$KEY_FILE" ] || rm -f "$KEY_FILE"
  if [ -z "$key" ]; then
    warn "No API_KEY was set and NO_API_KEY=0, so Strata generated one."
    warn "Show it with: docker exec <container> strata launch --show-key --preset $PRESET"
  fi
fi

phase serving
say ">> Strata sirviendo Qwen3.8-Flash-Next-GSQ-RCO-Coder en el puerto interno $PORT."

# ---- Mantiene el contenedor vivo mientras el servidor esté en estado "ready"
bad=0
while [ -z "$stopping" ]; do
  sleep 10 &
  wait $!
  [ -n "$stopping" ] && break
  if "$STRATA" launch --status --output json 2>/dev/null | grep -q '"state": *"ready"'; then bad=0; else bad=$((bad+1)); fi
  if [ "$bad" -ge 3 ]; then
    phase failed
    die "The Strata server is no longer running. Check /data/install logs and 'docker logs'." 1
  fi
done
exit 0
