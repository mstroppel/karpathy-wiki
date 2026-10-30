FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587 AS opencode

# The OpenCode CLI is downloaded from the npm registry and verified against
# the pinned sha512 digest (hex) for the supported amd64 architecture.
# Update the digest together with OPENCODE_VERSION; the scheduled
# OpenCode update workflow opens a pull request that bumps both.
ARG OPENCODE_VERSION=2.2.2
ARG OPENCODE_SHA512_LINUX_X64=c1ad289751d468a3434ea8dfa89c2a9aade7e2d95e8c1cb6513e3cc15442ca94541b2f566c8a5778bf719479a58c0a1d403e8771947676a8896054e50cca8854
ARG TARGETARCH
ARG APP_UID=1000
ARG APP_GID=100

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git ripgrep tzdata python3 python3-markdown python3-bleach python3-pil python3-defusedxml python3-tinycss2 \
    && rm -rf /var/lib/apt/lists/*

RUN arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && case "${arch}" in \
        amd64) target=linux-x64; digest="${OPENCODE_SHA512_LINUX_X64}" ;; \
        *) printf 'unsupported architecture: %s\n' "${arch}" >&2; exit 1 ;; \
    esac \
    && curl --fail --location --retry 3 \
        "https://registry.npmjs.org/@opencode/cli-${target}/-/cli-${target}-${OPENCODE_VERSION}.tgz" \
        --output /tmp/opencode.tar.gz \
    && printf '%s  /tmp/opencode.tar.gz\n' "${digest}" | sha512sum --check - \
    && tar -xzf /tmp/opencode.tar.gz -C /tmp \
    && install -m 0755 /tmp/package/bin/opencode /usr/local/bin/opencode \
    && rm -rf /tmp/opencode.tar.gz /tmp/package \
    && opencode --version

RUN getent group "$APP_GID" >/dev/null || groupadd --gid "$APP_GID" app \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --create-home --home-dir /home/opencode app \
    && install -d -o "$APP_UID" -g "$APP_GID" \
        /home/opencode/.config/opencode \
        /home/opencode/.local/share/opencode \
        /home/opencode/.local/state

COPY config/init.sh /etc/opencode/init.sh
COPY config/opencode.json /etc/opencode/opencode.json
COPY config/routing.md /etc/opencode/routing.md
COPY config/skills /etc/opencode/skills
COPY opencode/render_analysis.py /usr/local/bin/render-analysis
COPY config/plugins /etc/opencode/plugins
COPY config/tools /etc/opencode/tools
COPY opencode/entrypoint.sh /usr/local/bin/karpathy-wiki-entrypoint
COPY opencode/runtime_write_isolation.py /usr/local/lib/karpathy-wiki/runtime_write_isolation.py
RUN chmod 0755 /etc/opencode/init.sh /usr/local/bin/karpathy-wiki-entrypoint /usr/local/bin/render-analysis

ENV HOME=/home/opencode
USER app
ENTRYPOINT ["karpathy-wiki-entrypoint"]

# Both images share the same checksum-verified CLI; external-server mode still
# needs it for version discovery. Never download a second independent CLI pin.
FROM node:26-bookworm-slim@sha256:28147155d47dc31abe0a46e0d12b59b2fb3d0a2dcc5dc65fce76a074180b16fe AS openchamber
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/chat
COPY openchamber/package.json openchamber/package-lock.json ./
# Integrity-locked dependencies only; lifecycle scripts may fetch unverified
# binaries. node-pty ships its Linux x64 prebuild in the locked tarball.
RUN npm ci --omit=dev --ignore-scripts \
    && node -e "require('node-pty')" \
    && npm cache clean --force
COPY --from=opencode /usr/local/bin/opencode /usr/local/bin/opencode
COPY openchamber/bootstrap.mjs openchamber/healthcheck.mjs /etc/openchamber/
COPY openchamber/entrypoint.sh /usr/local/bin/karpathy-wiki-chat
RUN chmod 0755 /usr/local/bin/karpathy-wiki-chat \
    && install -d -o 1000 -g 1000 /home/openchamber/.config/openchamber /knowledge/wiki
ENV HOME=/home/openchamber NODE_ENV=production OPENCHAMBER_DATA_DIR=/home/openchamber/.config/openchamber
USER 1000:1000
WORKDIR /knowledge/wiki
ENTRYPOINT ["karpathy-wiki-chat"]
