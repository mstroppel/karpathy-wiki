FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251

# The OpenCode CLI is downloaded from the npm registry and verified against
# these pinned sha512 digests (hex, one per supported target architecture).
# Digests must be updated together with OPENCODE_VERSION; the scheduled
# OpenCode update workflow opens a pull request that bumps all three.
ARG OPENCODE_VERSION=2.0.16
ARG OPENCODE_SHA512_LINUX_X64=0559af55d740edce158525e04e21ad641f6953ec53c8582c779e8a92f42860cc9dab2f317f33c6bbd051c105421bf453c4685103075afcc60459aadd76c70852
ARG OPENCODE_SHA512_LINUX_ARM64=05386913179cf70108842e14f62458fe359e70cf1b54e56e630d984d8619fea367dabfd19630c6d9f967803982645335702ea68aeda4a8e80f9159d57aa69aa5
ARG TARGETARCH
ARG APP_UID=1000
ARG APP_GID=100

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git ripgrep tzdata \
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
COPY config/plugins /etc/opencode/plugins
COPY config/tools /etc/opencode/tools
COPY opencode/entrypoint.sh /usr/local/bin/karpathy-wiki-entrypoint
RUN chmod 0755 /etc/opencode/init.sh /usr/local/bin/karpathy-wiki-entrypoint

ENV HOME=/home/opencode
USER app
ENTRYPOINT ["karpathy-wiki-entrypoint"]
