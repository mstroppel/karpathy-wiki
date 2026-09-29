FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251

# The OpenCode CLI is downloaded from the npm registry and verified against
# these pinned sha512 digests (hex, one per supported target architecture).
# Digests must be updated together with OPENCODE_VERSION; the scheduled
# OpenCode update workflow opens a pull request that bumps all three.
ARG OPENCODE_VERSION=2.0.19
ARG OPENCODE_SHA512_LINUX_X64=141feb44df8bc1b8a9c357881565d4491cc67a3935dc7877c8fde428af216deeb6acae649af0959f047145adf3c6ac00eb2ea22e73cc2ab7b3dae1efa6f8d6df
ARG OPENCODE_SHA512_LINUX_ARM64=8b41ce1e8bd6826f102ba333128b3a579ac38f774738e69f51d3f770d1f831ce1d8d42d63004307cda32829e905e69565dab55814cd0b7ae571e474217b5403c
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
