FROM python:3.12-slim

# git + curl for shell-enabled roles (implementer/reviewer/deploy/qa workspaces)
RUN apt-get update \
    && apt-get install -y --no-install-recommends git curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sentinel ./sentinel
COPY docs ./docs
COPY config ./config
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && mkdir -p /data

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    DOCS_DIR=/app/docs \
    SENTINEL_CONFIG=/app/config/pipeline.yml

EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
CMD ["serve"]
