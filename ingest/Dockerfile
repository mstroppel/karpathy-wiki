# The rclone build stage is pinned by tag and digest so a registry retag
# cannot swap the copied binary. The default keeps standalone builds working;
# docker-bake.hcl passes the same value (validated by tests/test_dependencies.py).
ARG RCLONE_VERSION=1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5

FROM rclone/rclone:${RCLONE_VERSION} AS rclone

FROM python:3.14-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
COPY karpathy_wiki_ingest /app/karpathy_wiki_ingest
COPY tests /app/tests

ENTRYPOINT ["python", "-m", "karpathy_wiki_ingest"]
