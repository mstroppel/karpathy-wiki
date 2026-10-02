FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251

# The OpenCode CLI is downloaded from the npm registry and verified against
# these pinned sha512 digests (hex, one per supported target architecture).
# Digests must be updated together with OPENCODE_VERSION; the scheduled
# OpenCode update workflow opens a pull request that bumps all three.
ARG OPENCODE_VERSION=2.0.22
ARG OPENCODE_SHA512_LINUX_X64=0e5575aa01030e756aa5358c3eabfbb421c2717a1dcd904568c7318ec898758e84e601c7d90ebc25f6ac564b324359eeea6d7cf3b59028684eb1ea44fa82b263
ARG OPENCODE_SHA512_LINUX_ARM64=b7fcaebbd0ea77f338e1d147f796e986eee290ee85013310b9593ff754019c2333b0ff3356b5d4ef30310f5c152e9618503a88a271268bcd17113945ab1edc87
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
RUN chmod 0755 /etc/opencode/init.sh /usr/local/bin/karpathy-wiki-entrypoint /usr/local/bin/render-analysis

ENV HOME=/home/opencode
USER app
ENTRYPOINT ["karpathy-wiki-entrypoint"]
