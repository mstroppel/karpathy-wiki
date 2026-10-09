#!/bin/sh
# Negative runtime probe: successful completion does NOT approve auto-ingest.
# No host data, credentials, exposed ports, models, or external network access.
set -eu

image=${BACKEND_IMAGE:-kw-opencode:integration}
case "${1:-}" in
    ''|--require-admissible) ;;
    *) printf 'usage: %s [--require-admissible]\n' "$0" >&2; exit 2 ;;
esac
# shellcheck disable=SC1007
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
container="kw-auto-ingest-probe-$(date +%s)-$$"
cleanup() {
    status=$?
    trap - EXIT
    docker rm -f "$container" >/dev/null 2>&1 || true
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

OPENCODE_PASSWORD=$(od -An -N24 -tx1 </dev/urandom | tr -d ' \n')
export OPENCODE_PASSWORD
docker create --name "$container" --network none --user 1000:1000 \
    --cap-drop ALL --security-opt no-new-privileges:true \
    -e OPENCODE_PASSWORD -e OPENCODE_CONFIG=/etc/opencode/opencode.json \
    -e OPENCODE_DISABLE_PROJECT_CONFIG=1 -e OPENCODE_DISABLE_EXTERNAL_SKILLS=1 \
    -e OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1 \
    --entrypoint /bin/sh -w /tmp "$image" -c '
        mkdir -p /tmp/guard-probe/wiki /home/opencode/.config/opencode/plugins
        cp /tmp/auto_ingest_guard.js /home/opencode/.config/opencode/plugins/auto_ingest_guard.js
        exec karpathy-wiki-entrypoint serve --hostname 127.0.0.1 --port 4096
    ' >/dev/null
# All directories and fixtures belong to this disposable container only.
docker cp "$root/tests/integration/auto_ingest_guard.js" "$container:/tmp/auto_ingest_guard.js"
docker cp "$root/tests/integration/auto_ingest_runtime.py" "$container:/tmp/auto_ingest_runtime.py"
docker start "$container" >/dev/null
docker exec "$container" opencode --version
if ! docker exec "$container" python3 /tmp/auto_ingest_runtime.py; then
    docker logs "$container"
    exit 1
fi
printf 'auto-ingest-runtime: probe completed; release gate BLOCKED\n'
if [ "${1:-}" = --require-admissible ]; then
    exit 1
fi
