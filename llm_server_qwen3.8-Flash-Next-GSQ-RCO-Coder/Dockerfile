# Imagen oficial de Strata v0.2.9 con el motor firmado para CUDA 13
# (compatible con GPUs NVIDIA RTX 20–50 y driver 580+; el host corre
# RTX 4090 Laptop GPU con driver 595.99.02 / CUDA 13.2).
FROM ghcr.io/niko1221/strata:0.2.9-cuda13

# Configuración por defecto para Qwen3.8-Flash-Next-GSQ-RCO-Coder:
# - FAMILY=coder, MODEL=IQ1_M (coincide con los 2 shards .gguf en /models/Qwen3.8-Flash-Next-GSQ-RCO-Coder)
# - CONTEXT=262144, PARALLEL=1 y KV=int8 (1 único slot de 262.144 tokens;
#   libera ~7,31 GiB de VRAM de los slots de batch para que entren MTP y el caché de expertos en la RTX 4090 de 16GB)
# - VISION=cpu (el Coder conserva visión; el encoder mmproj-Qwen3.8-Flash-Next-BF16.gguf
#   va junto a los shards en MODEL_PATH y corre en CPU para no quitarle VRAM al modelo)
# - TUNE=skip (evita el autotuning inicial de ~20 min; poner TUNE=quick para habilitarlo)
# - NO_API_KEY=1 y STRATA_ALLOWED_HOSTS=* (permite que app, openhands y qwen_code
#   se conecten directo por red Docker o host.docker.internal sin rechazos 401/403)
ENV FAMILY=coder \
    MODEL=IQ1_M \
    MODEL_PATH=/models/Qwen3.8-Flash-Next-GSQ-RCO-Coder \
    CONTEXT=262144 \
    PARALLEL=1 \
    KV=int8 \
    VISION=cpu \
    TUNE=skip \
    NO_API_KEY=1 \
    STRATA_ALLOWED_HOSTS=* \
    PORT=8080

COPY entrypoint.sh /opt/strata/entrypoint.sh
RUN chmod 0755 /opt/strata/entrypoint.sh \
    && ln -sf /opt/strata/entrypoint.sh /opt/entrypoint.sh

EXPOSE 8080

STOPSIGNAL SIGTERM
ENTRYPOINT ["/usr/bin/tini", "--", "/opt/strata/entrypoint.sh"]
CMD ["run"]
