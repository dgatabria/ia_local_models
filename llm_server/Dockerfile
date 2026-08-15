FROM nvidia/cuda:12.4.1-devel-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    git \
    cmake \
    ninja-build \
    build-essential \
    curl \
    libcurl4-openssl-dev \
    pciutils \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# Mismo fix que en el servicio de chat: el stub de libcuda (de la imagen
# devel) no tiene el sufijo .1 que busca el linker para resolver
# dependencias transitivas. En runtime, nvidia-container-toolkit monta
# la libcuda.so.1 real del host con prioridad sobre esta ruta.
RUN ln -sf /usr/local/cuda/lib64/stubs/libcuda.so /usr/lib/x86_64-linux-gnu/libcuda.so.1 && ldconfig

WORKDIR /opt

# Compilamos el binario standalone de llama.cpp (no el binding python).
# Necesitamos llama-server directamente porque expone flags nuevos como
# --cpu-moe (offload de expertos MoE a RAM) que llama-cpp-python todavía
# no expone. BUILD_SHARED_LIBS=OFF simplifica el despliegue (binario
# estático, sin .so sueltos que mover).
RUN git clone --depth 1 https://github.com/ggml-org/llama.cpp.git && \
    cmake llama.cpp -B llama.cpp/build \
        -DBUILD_SHARED_LIBS=OFF \
        -DGGML_CUDA=ON \
        -DLLAMA_CURL=ON \
        -DLLAMA_BUILD_EXAMPLES=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DCMAKE_BUILD_TYPE=Release && \
    cmake --build llama.cpp/build --config Release -j --target llama-server llama-gguf-split && \
    cp llama.cpp/build/bin/llama-server /usr/local/bin/ && \
    cp llama.cpp/build/bin/llama-gguf-split /usr/local/bin/ && \
    rm -rf llama.cpp

# El modelo se descarga al iniciar el contenedor (vía entrypoint) y queda
# cacheado en el volumen montado en /models, igual que en el servicio de
# chat. Esto evita re-descargar ~16GB cada vez que se reconstruye la imagen.
#
# Versión pineada a propósito: huggingface-hub renombró su CLI de
# huggingface-cli a hf y, en v1.0+, removió el comando viejo por completo.
# Pineamos una versión post-rename conocida-buena en vez de "latest" para
# no volver a romper el entrypoint con un cambio de CLI futuro.
RUN pip3 install --upgrade pip && \
    pip3 install --no-cache-dir --break-system-packages "huggingface-hub==0.36.0"

COPY entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh
ENTRYPOINT ["/opt/entrypoint.sh"]
