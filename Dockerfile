FROM python:3.12-slim

WORKDIR /app

# System deps:
#   tesseract-ocr (+ eng language data) — used by src/parser/image_ocr.py to
#   extract trade fields from image-bot chart cards (Pingu Charts etc.).
#   libjpeg / zlib are Pillow runtime deps (not strictly required on slim
#   bookworm but safe to be explicit; preserves Pillow's image-format support).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       tesseract-ocr \
       tesseract-ocr-eng \
       libjpeg62-turbo \
       zlib1g \
    && rm -rf /var/lib/apt/lists/*

# Layer-cached dependency install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .
COPY src/ src/
COPY config/ config/
COPY signals/ signals/

# Runtime data + logs (volumes mounted from host in production)
RUN mkdir -p data logs

# Stay as root: Railway mounts persistent volumes as root-owned and
# a non-root container user can't write to them. Container isolation
# already provides the security boundary for this workload.

# OAuth callback server
EXPOSE 8080

# Health check hits the aiohttp /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

ENTRYPOINT ["python", "main.py"]
