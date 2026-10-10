#!/bin/sh
set -eu
if [ "${WIKI_RUNTIME_READ_ONLY:-false}" != true ] || [ "$(id -u)" = 0 ]; then
    printf 'publisher: isolated reader mode and non-root user required\n' >&2
    exit 1
fi
umask 077
# No listener, worker credentials or model runtime. Only trusted host exec can
# invoke the control CLI. Container restart kills all publication descendants.
exec python3 /opt/karpathy-wiki/publisher/service.py
