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

# El stub de libcuda incluido en la imagen de desarrollo no trae el nombre
# libcuda.so.1 que necesita el linker. En runtime, nvidia-container-toolkit
# monta la biblioteca real del host por delante de este stub.
RUN ln -sf /usr/local/cuda/lib64/stubs/libcuda.so \
        /usr/lib/x86_64-linux-gnu/libcuda.so.1 \
    && ldconfig

WORKDIR /opt

# Revisión conocida de llama.cpp al preparar este experimento.
# El pin evita que futuros rebuilds cambien silenciosamente kernels,
# templates o flags.
ARG LLAMA_CPP_COMMIT=ad1de39e0708e3ced9c71bb3c82d93a2c046a73f

RUN git clone --filter=blob:none --no-checkout \
        https://github.com/ggml-org/llama.cpp.git \
    && git -C llama.cpp fetch --depth 1 origin "${LLAMA_CPP_COMMIT}" \
    && git -C llama.cpp checkout --detach FETCH_HEAD \
    && cmake llama.cpp -B llama.cpp/build \
        -DBUILD_SHARED_LIBS=OFF \
        -DGGML_CUDA=ON \
        -DLLAMA_CURL=ON \
        -DLLAMA_BUILD_EXAMPLES=OFF \
        -DLLAMA_BUILD_TESTS=OFF \
        -DCMAKE_BUILD_TYPE=Release \
    && cmake --build llama.cpp/build \
        --config Release \
        -j \
        --target llama-server \
    && cp llama.cpp/build/bin/llama-server /usr/local/bin/ \
    && rm -rf llama.cpp

# Instala la CLI `hf`, utilizada por el entrypoint para descargar el GGUF
# al volumen persistente durante el primer arranque.
RUN pip3 install --upgrade pip \
    && pip3 install \
        --no-cache-dir \
        --break-system-packages \
        "huggingface-hub==0.36.0"

COPY entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh

ENTRYPOINT ["/opt/entrypoint.sh"]
