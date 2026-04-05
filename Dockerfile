FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /app/data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

# Entrypoint fixes SSH key permissions at runtime (Docker volumes strip them)
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

EXPOSE 8765

ENTRYPOINT ["./entrypoint.sh"]
