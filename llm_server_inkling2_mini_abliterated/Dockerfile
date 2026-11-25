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

# Inkling 2 Mini usa la arquitectura `inkling2_loop`, que llegó a llama.cpp
# recién en b12600 (PR #32694, 2026-11-20). Este pin (master del 2026-11-24)
# incluye además los arreglos posteriores que afectan la calidad de salida:
#   - #32704 re-share de los loop slots después del scale pass
#   - #32799 ignore_merges en el vocab de inkling2
#   - #32833 parser/grammar del chat template de inkling
#   - #32940 clip con ggml_l2_norm
#   - #32855 mix() weighted sum
# NO incluye el fix del issue #32710 (salida basura/NaN en CUDA cuando los
# expertos de las capas en loop 6-17 quedan en host). Ver entrypoint.sh.
ARG LLAMA_CPP_COMMIT=0ce7f213b0749fa4dee4e87d8ae7bf63e621c1ad

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

# CLI `hf` para bajar el mmproj de ggml-org (repo público, sin token) en el
# primer arranque. El GGUF principal (repo gated de orcarouter) se baja a
# mano en el host: el contenedor nunca lo descarga.
RUN pip3 install --upgrade pip \
    && pip3 install \
        --no-cache-dir \
        --break-system-packages \
        "huggingface-hub==0.36.0"

COPY entrypoint.sh /opt/entrypoint.sh
RUN chmod +x /opt/entrypoint.sh

ENTRYPOINT ["/opt/entrypoint.sh"]
