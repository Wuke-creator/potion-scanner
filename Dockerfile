FROM python:3.12-slim

WORKDIR /app

# Layer-cached dependency install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .
COPY src/ src/
COPY signals/ signals/

# Health check using stdlib (no curl/wget needed)
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080')"

ENTRYPOINT ["python", "main.py"]
