FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587 AS opencode

# The OpenCode CLI is downloaded from the npm registry and verified against
# these pinned sha512 digests (hex, one per supported target architecture).
# Digests must be updated together with OPENCODE_VERSION; the scheduled
# OpenCode update workflow opens a pull request that bumps all three.
ARG OPENCODE_VERSION=2.0.30
ARG OPENCODE_SHA512_LINUX_X64=a01c3c76d83d05688a921c8f3b103a18dfa8efcce641f8e0562ec7cfd75ed69259255c8604c2e59189351303de4bebd6c8e0926adcb6171080b181f56cc752da
ARG OPENCODE_SHA512_LINUX_ARM64=110cd5038d235a46d831019da82df2ec8ec33949f0e736213cfc026b6bc56ff30afc1b8190766a3dcb030010fcd8409a355189a38de4cfcb340a4a1d6fc100e3
ARG TARGETARCH
ARG APP_UID=1000
ARG APP_GID=100

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git ripgrep tzdata python3 python3-markdown python3-bleach \
    && rm -rf /var/lib/apt/lists/*

RUN arch="${TARGETARCH:-$(dpkg --print-architecture)}" \
    && case "${arch}" in \
        amd64) target=linux-x64; digest="${OPENCODE_SHA512_LINUX_X64}" ;; \
        arm64) target=linux-arm64; digest="${OPENCODE_SHA512_LINUX_ARM64}" ;; \
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
FROM node:26-bookworm-slim@sha256:86f07bc9c5dce4578cf37e5a418b7bfc7f817cda25cde66e2b66e95ed86c4567 AS openchamber
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /opt/chat
COPY openchamber/package.json openchamber/package-lock.json ./
# Integrity-locked dependencies only; lifecycle scripts may fetch unverified
# binaries. node-pty ships its Linux x64/arm64 prebuilds in the locked tarball.
RUN npm ci --omit=dev --ignore-scripts \
    && node -e "require('node-pty')" \
    && npm cache clean --force
COPY --from=opencode /usr/local/bin/opencode /usr/local/bin/opencode
COPY openchamber/bootstrap.mjs openchamber/healthcheck.mjs /etc/openchamber/
COPY openchamber/manual-ingest.mjs /etc/openchamber/
COPY openchamber/patch-manual-ingest.mjs /etc/openchamber/
RUN node /etc/openchamber/patch-manual-ingest.mjs /opt/chat/node_modules/@openchamber/web/server/index.js
COPY openchamber/entrypoint.sh /usr/local/bin/karpathy-wiki-chat
RUN chmod 0755 /usr/local/bin/karpathy-wiki-chat \
    && install -d -o 1000 -g 1000 /home/openchamber/.config/openchamber /knowledge/wiki
ENV HOME=/home/openchamber NODE_ENV=production OPENCHAMBER_DATA_DIR=/home/openchamber/.config/openchamber
USER 1000:1000
WORKDIR /knowledge/wiki
ENTRYPOINT ["karpathy-wiki-chat"]

# Trusted writer only: no OpenCode, chat, npm packages, model credentials or API.
# The deterministic analysis renderer ships here so the trusted channel derives
# print views itself instead of trusting runtime-produced artifacts.
FROM node:26-bookworm-slim@sha256:86f07bc9c5dce4578cf37e5a418b7bfc7f817cda25cde66e2b66e95ed86c4567 AS publisher
RUN apt-get update \
    && apt-get install -y --no-install-recommends git python3 python3-markdown python3-bleach \
    && rm -rf /var/lib/apt/lists/*
COPY config/tools /opt/karpathy-wiki/config/tools
COPY publisher /opt/karpathy-wiki/publisher
COPY opencode/render_analysis.py /usr/local/bin/render-analysis
COPY config/skills/wiki-analysis-save/print-template.html /opt/karpathy-wiki/print-template.html
ENV KARPATHY_WIKI_PRINT_TEMPLATE=/opt/karpathy-wiki/print-template.html
RUN chmod 0755 /opt/karpathy-wiki/publisher/entrypoint.sh /usr/local/bin/render-analysis
USER 1000:1000
WORKDIR /knowledge/wiki
ENTRYPOINT ["/opt/karpathy-wiki/publisher/entrypoint.sh"]
