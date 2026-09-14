FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY karpathy_wiki_ingest /app/karpathy_wiki_ingest
COPY tests /app/tests

ENTRYPOINT ["python", "-m", "karpathy_wiki_ingest"]
