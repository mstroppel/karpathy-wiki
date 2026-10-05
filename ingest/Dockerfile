# The ingest stack is split into installable distributions: the core package
# (karpathy-wiki-ingest) plus one wheel per built-in plugin. Each final stage
# installs the core and exactly one plugin, so no image ships another plugin's
# code. The rclone build stage is pinned by tag and digest so a registry
# retag cannot swap the copied binary. The default keeps standalone builds
# working; docker-bake.hcl passes the same value (validated by
# tests/test_dependencies.py).
ARG RCLONE_VERSION=1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5

FROM rclone/rclone:${RCLONE_VERSION} AS rclone

FROM python:3.14-slim-bookworm@sha256:c8137f4c460908c8763f281c8f22c431eb5c538514ba9553fc3a89c06b7cfb88 AS build

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

COPY core/ /src/core/
COPY webdav/ /src/webdav/
COPY audio/ /src/audio/
COPY speech/ /src/speech/
COPY paperless/ /src/paperless/
# The PEP 517 build backend is preinstalled from a hash-pinned wheel so the
# wheel builds below can run with --no-build-isolation and never execute
# index-resolved tooling. This install is the only index access in the build.
COPY build-requirements.txt /src/build-requirements.txt
RUN python -m pip install --require-hashes --no-deps -r /src/build-requirements.txt
# Each distribution builds into its own wheel directory so every final stage
# can copy exactly the wheels it needs. --no-deps is safe because the plugin
# wheels declare an exact dependency on the core wheel, which is built here.
RUN mkdir -p /wheels/core /wheels/webdav /wheels/audio /wheels/speech /wheels/paperless \
    && python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels/core /src/core \
    && python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels/webdav /src/webdav \
    && python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels/audio /src/audio \
    && python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels/speech /src/speech \
    && python -m pip wheel --no-deps --no-build-isolation --wheel-dir /wheels/paperless /src/paperless

FROM python:3.14-slim-bookworm@sha256:c8137f4c460908c8763f281c8f22c431eb5c538514ba9553fc3a89c06b7cfb88 AS core

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone
# --no-index: the runtime image installs only wheels built from this
# repository; nothing is resolved from a package index at runtime-install time.
COPY --from=build /wheels/core /wheels
RUN python -m pip install --no-index --find-links=/wheels karpathy-wiki-ingest \
    && rm -rf /wheels
# The core image is the base for third-party plugins; it ships no plugins.
ENTRYPOINT ["python", "-m", "karpathy_wiki_ingest"]

FROM core AS webdav
COPY --from=build /wheels/webdav /wheels
RUN python -m pip install --no-deps --no-index --find-links=/wheels karpathy-wiki-ingest-webdav \
    && rm -rf /wheels

# The audio plugin image ships the audio provider only; transcription runs
# in the speech image so no provider image needs the model stack.
FROM core AS audio
COPY --from=build /wheels/speech /wheels
COPY --from=build /wheels/audio /wheels
RUN python -m pip install --no-deps --no-index --find-links=/wheels \
        karpathy-wiki-ingest-audio karpathy-wiki-speech \
    && rm -rf /wheels

# The speech image runs faster-whisper locally; PyAV carries the bundled
# FFmpeg libraries so decoding needs no system ffmpeg. This is the only
# stage that installs third-party runtime wheels from the index, pinned by
# hash in speech/requirements.txt so dependabot keeps them current.
FROM python:3.14-slim-bookworm@sha256:c8137f4c460908c8763f281c8f22c431eb5c538514ba9553fc3a89c06b7cfb88 AS speech
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY --from=build /wheels/speech /wheels
COPY speech/requirements.txt /src/speech-requirements.txt
# A new runtime layer gets a new identity, including base-image/dependency changes.
RUN python -m pip install --no-index --no-deps --find-links=/wheels karpathy-wiki-speech \
    && python -m pip install --require-hashes --only-binary=:all: \
        -r /src/speech-requirements.txt \
    && python -c 'import pathlib, uuid; pathlib.Path("/app/speech-runtime-id").write_text(uuid.uuid4().hex)' \
    && rm -rf /wheels /src
ENTRYPOINT ["python", "-m", "karpathy_wiki_speech"]

FROM core AS paperless
COPY --from=build /wheels/paperless /wheels
RUN python -m pip install --no-deps --no-index --find-links=/wheels karpathy-wiki-ingest-paperless \
    && rm -rf /wheels
