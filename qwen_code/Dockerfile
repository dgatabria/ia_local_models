FROM node:22-slim

# git: Qwen Code lo usa para operaciones de repo (status, diff, etc.)
# curl/unzip: para bajar .NET SDK y Godot más abajo.
# ssh: para que git use SSH como transporte (push/pull con tu clave
# montada en ~/.ssh). Nota: "ssh" en Debian trae también openssh-server,
# que no hace falta acá — si en algún momento se quiere una imagen más
# chica, alcanza con "openssh-client" solo.
# python3/pip: para los validadores de contenido de tu proyecto.
#
# screen: para persistir sesiones largas de Qwen Code sin depender de
# que la terminal/SSH se mantenga conectada. NOTA: se probó primero
# GNU screen y rompía el render de la TUI (Ink) incluso con
# ncurses-term instalado — se pasó a tmux, que tiene mejor soporte de
# alternate-screen/truecolor para este tipo de TUIs modernas.
# ncurses-term: entradas de terminfo para TERM=screen/tmux.
# Las libX11/libwayland/etc. de acá abajo son las dependencias runtime
# reales de Godot 4.x (según el listado oficial del paquete de Fedora,
# que declara explícitamente cada .so que usa el binario) — aunque
# corras con --headless, Godot igual intenta inicializar un display
# server (primero X11, después Wayland) antes de caer a modo headless
# de verdad, y necesita estas librerías cargadas aunque nunca conecten
# a una pantalla real. Sin esto es un juego de "whack-a-mole": arreglás
# una librería faltante, aparece la siguiente en el próximo intento.
#
# CAVEAT conocido: en Debian 13+ / Ubuntu recientes, "libasound2" se
# renombró a "libasound2t64" (transición de ABI de 64-bit time_t). Si el
# build falla ahí con "Package libasound2 has no installation candidate",
# cambiá esa línea a libasound2t64 — es la única de esta lista con
# historial de renames conocido.
#
# sudo: el usuario "node" no es root (a propósito, para que los archivos
# que edite en tus repos queden con tu UID, no con root). Pero durante el
# desarrollo vas descubriendo dependencias de sistema nuevas sobre la
# marcha (compiladores, librerías, lo que sea que tu proyecto necesite) —
# sudo sin contraseña le permite instalarlas sin tener que entrar como
# root a mano cada vez.
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    ca-certificates \
    sudo \
    curl \
    unzip \
    ssh \
    python3 \
    python3-pip \
    screen \
    tmux \
    ncurses-term \
    libicu72 \
    libx11-6 \
    libxext6 \
    libxrender1 \
    libxi6 \
    libxcursor1 \
    libxinerama1 \
    libxrandr2 \
    libxkbcommon0 \
    libwayland-client0 \
    libwayland-cursor0 \
    libwayland-egl1 \
    libfontconfig1 \
    libgl1 \
    libglu1-mesa \
    libasound2 \
    libpulse0 \
    libudev1 \
    libdbus-1-3 \
    && rm -rf /var/lib/apt/lists/* \
    && echo "node ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/node \
    && chmod 0440 /etc/sudoers.d/node

# --- Locale UTF-8 ---
# node:22-slim no genera ningún locale por default — sin esto, caracteres
# multi-byte (tildes, ñ, etc.) se renderizan mal o con ancho incorrecto
# en TUIs como Qwen Code. IMPORTANTE: LANG/LC_ALL tienen que estar
# seteados a nivel de imagen (ENV), no alcanza con que tu terminal del
# host los tenga bien configurados — `docker exec` no hereda el entorno
# de tu shell del host, arranca con el entorno propio del contenedor.
RUN apt-get update && apt-get install -y --no-install-recommends locales \
    && rm -rf /var/lib/apt/lists/* \
    && sed -i '/es_AR.UTF-8/s/^# //g' /etc/locale.gen \
    && locale-gen

ENV LANG=es_AR.UTF-8
ENV LANGUAGE=es_AR:es
ENV LC_ALL=es_AR.UTF-8

# Claude Code: variable no documentada oficialmente (confirmada por
# análisis del binario) que hace que detecte este contenedor como
# "sandbox reconocido" — la propia doc pública de permission-modes lo
# corrobora indirectamente: "The check is skipped automatically inside
# a recognized sandbox". Sin esto, algunos chequeos de seguridad
# (pensados para no correr con privilegios elevados en la máquina real
# del usuario) se disparan igual, aunque estemos en un contenedor
# aislado de un solo uso.
ENV IS_SANDBOX=1

# Qwen Code corta el stream si no ve actividad por 240s, aunque el
# server siga vivo generando/pensando (mismo tipo de problema que ya
# resolvimos con LLM_TIMEOUT en OpenHands, acá del lado del cliente en
# vez del agente). Con prompts largos + reasoning_level alto, el prefill
# y el "pensamiento" antes del primer token pueden tardar más que eso
# sin que nada esté roto. 0 = sin límite.
ENV QWEN_STREAM_IDLE_TIMEOUT_MS=0

# --- .NET 8 SDK ---
# NOTA sobre libicu72: es la versión de ICU (globalización/culturas) que
# trae Debian 12 "bookworm" (base de node:22-slim). Si al buildear esto en
# tu máquina falla con "Package libicu72 has no installation candidate" o
# similar, tu base image usa otra versión de Debian — corré
# `apt-cache search libicu` DENTRO de un contenedor node:22-slim para ver
# el nombre exacto disponible y avisame para ajustarlo. La alternativa
# rápida (pero con trade-off) es setear DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1
# para que .NET no necesite ICU en absoluto — a costa de comparaciones de
# string/fechas "culture-aware" (relevante si tus validadores procesan
# texto en español con reglas de localización reales).
ENV DOTNET_ROOT=/usr/local/dotnet-sdk
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1
ENV DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1
ENV PATH="${DOTNET_ROOT}:${PATH}"

RUN curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
    && chmod +x /tmp/dotnet-install.sh \
    && /tmp/dotnet-install.sh --channel 8.0 --install-dir "${DOTNET_ROOT}" --no-path \
    && rm /tmp/dotnet-install.sh
# Se instaló como root; el usuario "node" solo necesita poder LEER y
# EJECUTAR estos archivos, nunca escribirlos — de ahí a+rX (no +w).
RUN chmod -R a+rX "${DOTNET_ROOT}"

# --- Godot 4.4.1 Mono (Linux x86_64) ---
# Mismo release/flavor/plataforma que scripts/setup_environment.sh del
# repo. Si en algún momento cambian de versión de Godot, actualizar
# GODOT_VERSION acá (y el nombre exacto del binario buscado más abajo,
# que incluye la versión en el nombre de archivo).
ARG GODOT_VERSION=4.4.1
ENV GODOT_ROOT=/usr/local/godot
ENV GODOT_SILENCE_ROOT_WARNING=1

RUN mkdir -p "${GODOT_ROOT}" \
    && curl -fL --retry 3 --retry-delay 2 \
        "https://downloads.godotengine.org/?flavor=stable&platform=linux.64&slug=mono_linux_x86_64.zip&version=${GODOT_VERSION}" \
        -o /tmp/godot.zip \
    && unzip -q /tmp/godot.zip -d "${GODOT_ROOT}" \
    && rm /tmp/godot.zip \
    && GODOT_BIN="$(find "${GODOT_ROOT}" -type f -name "Godot_v${GODOT_VERSION}-stable_mono_linux.x86_64" -print -quit)" \
    && test -n "${GODOT_BIN}" \
    && chmod +x "${GODOT_BIN}" \
    && ln -s "${GODOT_BIN}" /usr/local/bin/godot \
    && ln -s "${GODOT_BIN}" /usr/local/bin/godot-mono

# --- Python: dependencias de los validadores de contenido ---
# --break-system-packages: Debian moderno bloquea pip system-wide por
# PEP 668; instalamos igual porque este contenedor es de un solo uso
# (no hay riesgo real de pisar paquetes del sistema que algo más
# necesite), tal como el propio setup_environment.sh ya contemplaba
# como fallback.
RUN pip3 install --no-cache-dir --break-system-packages PyYAML jsonschema

RUN npm install -g @qwen-code/qwen-code@latest

# --- Claude Code ---
# Instalación estándar (npm). Auth: en Linux guarda las credenciales en
# ~/.claude/.credentials.json (modo 0600) + ~/.claude.json (metadata de
# cuenta). Mapeamos ambos desde el host en docker-compose.yml para
# reusar la sesión ya logueada, sin volver a autenticar acá.
RUN npm install -g @anthropic-ai/claude-code

# --- Antigravity CLI (agy) ---
# El instalador oficial detecta el HOME y deja el binario en
# ~/.local/bin/agy. Como esto corre como root durante el build (HOME=
# /root), lo copiamos a /usr/local/bin para que quede en el PATH de
# cualquier usuario, incluido "node" en runtime.
#
# CAVEAT importante sobre auth: Antigravity usa el keyring del sistema
# (libsecret/D-Bus) para cachear la sesión — algo que un contenedor
# mínimo como este NO tiene corriendo. Su config vive en ~/.gemini/
# (heredado de que reemplazó a Gemini CLI); mapeamos esa carpeta desde
# el host como la mejor apuesta para evitar el login, pero si el host
# guarda el secreto real en su keyring (no en un archivo dentro de
# ~/.gemini/), puede que la PRIMERA vez adentro del contenedor sí haga
# falta loguearse una vez (flujo de URL + código, pensado justo para
# entornos headless/SSH) — de ahí en más debería persistir en el
# directorio mapeado, ya que el contenedor no tiene keyring al que caer.
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash \
    && cp /root/.local/bin/agy /usr/local/bin/agy \
    && chmod +x /usr/local/bin/agy

# Pre-creamos y le damos dueño al directorio ANTES de que el volumen
# nombrado se monte encima. Docker inicializa un volumen nombrado vacío
# copiando el contenido (y los permisos) que ya existían en esa ruta
# dentro de la imagen — sin este paso, el volumen queda con dueño root
# y el usuario "node" (uid 1000) no puede escribir ahí.
RUN mkdir -p /home/node/.qwen && chown -R node:node /home/node

WORKDIR /workspace

COPY entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh

# --- Symlinks a /usr/bin para todos los binarios instalados ---
# Lo que se instala vía apt (git, tmux, ssh, curl, python3...) ya vive
# en /usr/bin de por sí. Esto cubre el resto: lo que instalamos por
# fuera de apt (dotnet en /usr/local/dotnet-sdk, godot extraído de un
# zip, agy vía su instalador propio, y los paquetes globales de npm) —
# así todo es encontrable en la ubicación canónica /usr/bin sin
# depender del orden del PATH. Se resuelve dinámicamente con `command -v`
# en vez de hardcodear paths, para no romper si algún instalador cambia
# de ubicación en una versión futura; lo que no se encuentre se saltea
# con un aviso en vez de tirar abajo el build entero.
RUN for bin in node npm npx dotnet godot godot-mono qwen claude agy; do \
        real_path="$(command -v "$bin" || true)"; \
        if [ -n "$real_path" ]; then \
            ln -sf "$real_path" "/usr/bin/$bin"; \
            echo "OK: $bin -> $real_path"; \
        else \
            echo "AVISO: '$bin' no encontrado en el PATH al buildear, se omite el symlink"; \
        fi; \
    done

ENTRYPOINT ["/opt/entrypoint.sh"]
