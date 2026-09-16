FROM rclone/rclone:1.75.1 AS rclone

FROM python:3.14-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
COPY karpathy_wiki_ingest /app/karpathy_wiki_ingest
COPY tests /app/tests

ENTRYPOINT ["python", "-m", "karpathy_wiki_ingest"]
