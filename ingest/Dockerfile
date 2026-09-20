# The rclone version is centralized in docker-bake.hcl (RCLONE_VERSION) and
# passed as a build argument; the default keeps standalone builds working.
ARG RCLONE_VERSION=1.75.1

FROM rclone/rclone:${RCLONE_VERSION} AS rclone

FROM python:3.14-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
COPY karpathy_wiki_ingest /app/karpathy_wiki_ingest
COPY tests /app/tests

ENTRYPOINT ["python", "-m", "karpathy_wiki_ingest"]
