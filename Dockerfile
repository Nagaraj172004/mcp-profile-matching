FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    ANONYMIZED_TELEMETRY=False \
    HF_HOME=/models \
    MCP_TRANSPORT=http \
    MCP_HOST=0.0.0.0

# Networks that inspect HTTPS: put the inspecting root certificate (*.crt) in
# docker/certs/ and it is trusted for the build and at runtime.
COPY docker/certs/ /usr/local/share/ca-certificates/extra/
RUN update-ca-certificates
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt \
    SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt

WORKDIR /app
COPY requirements.txt .
RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
 && pip install -r requirements.txt

COPY . .
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')" \
 && useradd --create-home app \
 && mkdir -p /app/reports /app/logs /app/data /app/chroma_db \
 && chown -R app /app /models
USER app

EXPOSE 8101 8102 8103
CMD ["python", "filesystem_mcp_server.py", "--transport", "http", "--host", "0.0.0.0", "--port", "8101"]
