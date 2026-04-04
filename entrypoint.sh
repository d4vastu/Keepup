#!/bin/sh
# Fix SSH key permissions — Docker volume mounts lose them
if [ -d /app/keys ]; then
    chmod 700 /app/keys 2>/dev/null || true
    chmod 600 /app/keys/* 2>/dev/null || true
fi

# Check for SSL cert and key — enable TLS if both are present
DATA_DIR="${DATA_PATH:-/app/data}"
SSL_CERT="$DATA_DIR/ssl/cert.pem"
SSL_KEY="$DATA_DIR/ssl/key.pem"

if [ -f "$SSL_CERT" ] && [ -f "$SSL_KEY" ]; then
    echo "SSL certificates found — starting with HTTPS"
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 \
        --ssl-certfile "$SSL_CERT" \
        --ssl-keyfile "$SSL_KEY"
else
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000
fi
