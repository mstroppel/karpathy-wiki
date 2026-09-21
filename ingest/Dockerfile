# The ingest stack is split into installable distributions: the core package
# (karpathy-wiki-ingest) plus one wheel per built-in plugin. Each final stage
# installs the core and exactly one plugin, so no image ships another plugin's
# code. The rclone build stage is pinned by tag and digest so a registry
# retag cannot swap the copied binary. The default keeps standalone builds
# working; docker-bake.hcl passes the same value (validated by
# tests/test_dependencies.py).
ARG RCLONE_VERSION=1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5

FROM rclone/rclone:${RCLONE_VERSION} AS rclone

FROM python:3.14-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56 AS build

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY core/ /src/core/
COPY webdav/ /src/webdav/
COPY paperless/ /src/paperless/
# --no-deps: the plugin wheels declare an exact dependency on the core wheel,
# which is built here as well; the index must not be consulted.
RUN python -m pip wheel --no-deps --wheel-dir /wheels /src/core /src/webdav /src/paperless

FROM python:3.14-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56 AS core

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
COPY --from=build /wheels /wheels
# --no-index: the runtime image installs only wheels built from this
# repository; nothing is resolved from a package index at build time.
RUN python -m pip install --no-index --find-links=/wheels karpathy-wiki-ingest \
    && rm -rf /wheels
# The core image is the base for third-party plugins; it ships no plugins.
ENTRYPOINT ["python", "-m", "karpathy_wiki_ingest"]

FROM core AS webdav
COPY --from=build /wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels karpathy-wiki-ingest-webdav \
    && rm -rf /wheels

FROM core AS paperless
COPY --from=build /wheels /wheels
RUN python -m pip install --no-index --find-links=/wheels karpathy-wiki-ingest-paperless \
    && rm -rf /wheels
