FROM python:3.12-slim

WORKDIR /app

# Layer-cached dependency install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Create non-root user
RUN groupadd --system appuser && useradd --system --gid appuser appuser

# Copy application code
COPY main.py .
COPY src/ src/
COPY config/ config/
COPY signals/ signals/

# Runtime data + logs (volumes mounted from host in production)
RUN mkdir -p data logs && chown -R appuser:appuser data logs

# Drop to non-root
USER appuser

# OAuth callback server
EXPOSE 8080

# Health check hits the aiohttp /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

ENTRYPOINT ["python", "main.py"]
