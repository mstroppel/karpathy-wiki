#!/bin/sh
# No production mounts, external model traffic or credentials.
set -eu
# shellcheck disable=SC1007
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
container="kw-batch-probe-$(date +%s)-$$"
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
    --tmpfs /knowledge:uid=1000,gid=1000,mode=0700 \
    --cap-drop ALL --security-opt no-new-privileges:true \
    -e OPENCODE_PASSWORD -e FAKE_API_KEY=synthetic \
    -e OPENCODE_CONFIG=/etc/opencode/opencode.json \
    -e OPENCODE_DISABLE_PROJECT_CONFIG=1 -e OPENCODE_DISABLE_EXTERNAL_SKILLS=1 \
    -e OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1 \
    --entrypoint python3 "${BACKEND_IMAGE:-kw-opencode:integration}" \
    /tmp/batch_ingest_runtime.py >/dev/null
docker cp "$root/tests/integration/batch_ingest_runtime.py" "$container:/tmp/batch_ingest_runtime.py"
docker start -a "$container"
[ "$(docker inspect -f '{{.State.ExitCode}}' "$container")" -eq 0 ]
