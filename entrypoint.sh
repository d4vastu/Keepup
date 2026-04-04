#!/bin/sh
# Fix SSH key permissions — Docker volume mounts lose them
if [ -d /app/keys ]; then
    chmod 700 /app/keys 2>/dev/null || true
    chmod 600 /app/keys/* 2>/dev/null || true
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
