FROM python:3.11-slim

WORKDIR /app

# poppler-utils: dependencia de sistema de pdf2image (usa pdftoppm/pdftocairo
# por debajo) para rasterizar páginas de PDF a imágenes.
RUN apt-get update && apt-get install -y --no-install-recommends \
    poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# Ya no se compila nada con CUDA acá: el modelo vive en el servicio
# llm_server, y este contenedor solo necesita ser un cliente HTTP liviano
# (Streamlit + openai-python) que le habla por la API OpenAI-compatible.
COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
