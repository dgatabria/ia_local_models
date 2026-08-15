FROM ghcr.io/openhands/agent-server:1.26.0-python

# Todas las dependencias que el agente necesita para trabajar con un
# proyecto Godot + C#:
#   - .NET SDK 8.0: compilar el código C# del juego
#   - Godot 4.7 mono (headless): correr el editor/exportar sin GUI,
#     lo que permite `dotnet build`, correr escenas en modo headless,
#     y ejecutar tests (GdUnit4 u otros frameworks que usen godot --headless)
#   - unzip, libssl, libfontconfig: dependencias de runtime de Godot en Linux

USER root

# ── .NET SDK 8.0 ──────────────────────────────────────────────────────────────
# Instalamos vía el script oficial de Microsoft (más portable que el PPA,
# que a veces no matchea la versión base de la imagen del agent-server).
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget \
        ca-certificates \
        libicu-dev \
        libssl-dev \
        unzip \
        libfontconfig1 \
        libglib2.0-0 \
        xvfb \
    && rm -rf /var/lib/apt/lists/*

RUN wget -qO /tmp/dotnet-install.sh https://dot.net/v1/dotnet-install.sh \
    && chmod +x /tmp/dotnet-install.sh \
    && /tmp/dotnet-install.sh --channel 8.0 --install-dir /usr/local/dotnet \
    && rm /tmp/dotnet-install.sh

ENV DOTNET_ROOT=/usr/local/dotnet
ENV PATH="${DOTNET_ROOT}:${DOTNET_ROOT}/tools:${PATH}"
ENV DOTNET_CLI_TELEMETRY_OPTOUT=1
ENV DOTNET_NOLOGO=1

# ── Godot 4.7 mono (headless) ─────────────────────────────────────────────────
# Usamos el build "mono" (con soporte C#/.NET) para x86_64.
# El flag --headless permite correr Godot sin display, que es lo que
# necesitamos en un contenedor sin GUI para compilar y correr escenas/tests.
ENV GODOT_VERSION=4.7
ENV GODOT_RELEASE=stable
ENV GODOT_FILENAME=Godot_v${GODOT_VERSION}-${GODOT_RELEASE}_mono_linux_x86_64

RUN wget -qO /tmp/godot.zip \
        "https://github.com/godotengine/godot/releases/download/${GODOT_VERSION}-${GODOT_RELEASE}/${GODOT_FILENAME}.zip" \
    && unzip /tmp/godot.zip -d /usr/local/godot \
    && ls -la /usr/local/godot/ \
    && GODOT_BIN=$(find /usr/local/godot -maxdepth 2 -type f -name "Godot_v*linux*" | grep -v ".console" | head -1) \
    && echo "Godot binary found: $GODOT_BIN" \
    && chmod +x "$GODOT_BIN" \
    && ln -sf "$GODOT_BIN" /usr/local/bin/godot \
    && rm /tmp/godot.zip

# Godot mono necesita que los GodotSharp assemblies sean accesibles.
# La instalación del build mono ya los incluye en el mismo directorio.
ENV GODOT_DATA_DIR=/usr/local/godot

# ── Verificación en build-time ─────────────────────────────────────────────────
RUN dotnet --version && godot --version --headless

# Volvemos al usuario por defecto del agent-server (root en la imagen base)
# El agente corre como root dentro del sandbox, igual que antes.
