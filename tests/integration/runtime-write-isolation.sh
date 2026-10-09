#!/bin/sh
# Pinned-runtime proof using disposable volumes only; no host binds or models.
set -eu
# shellcheck disable=SC1007
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
image=${BACKEND_IMAGE:-kw-opencode:integration}
prefix="kw-write-isolation-$(date +%s)-$$"
backend="$prefix-backend"
rejected="$prefix-rejected"
data="$prefix-data"
cleanup() {
    status=$?
    trap - EXIT
    docker rm -f "$backend" "$rejected" >/dev/null 2>&1 || true
    docker volume rm "$data" >/dev/null 2>&1 || true
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
OPENCODE_PASSWORD=$(od -An -N24 -tx1 </dev/urandom | tr -d ' \n')
export OPENCODE_PASSWORD
docker volume create "$data" >/dev/null
docker run --rm --network none --user 0:0 --entrypoint /bin/sh \
    --mount "type=volume,src=$data,dst=/knowledge" \
    -e WIKI_NAME='Synthetic Wiki' -e WIKI_PUBLIC_URL=https://wiki.invalid \
    -e GIT_AUTHOR_NAME='Synthetic Test' -e GIT_AUTHOR_EMAIL=test@localhost \
    -e PUID=1000 -e PGID=1000 "$image" /etc/opencode/init.sh >/dev/null
docker run --rm --network none --user 1000:1000 --entrypoint /bin/sh \
    --mount "type=volume,src=$data,dst=/knowledge" "$image" -c '
        for directory in /knowledge/wiki /knowledge/sources /knowledge/incoming/ingest-journal /knowledge/incoming/answers; do
            printf "synthetic unchanged bytes\n" > "$directory/isolation-sentinel"
        done
    '

create_backend() {
    mode=$1
    name=$2
    shift 2
    suffix=
    if [ "$mode" = true ]; then
        suffix=,readonly
        set -- --read-only "$@"
    fi
    docker create --name "$name" --network none --user 1000:1000 \
        --cap-drop ALL --security-opt no-new-privileges:true \
        -e OPENCODE_PASSWORD -e WIKI_RUNTIME_READ_ONLY="$mode" \
        -e OPENCODE_CONFIG=/etc/opencode/opencode.json \
        -e OPENCODE_DISABLE_PROJECT_CONFIG=1 -e OPENCODE_DISABLE_EXTERNAL_SKILLS=1 \
        -e OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1 \
        --tmpfs /tmp:exec --tmpfs /home/opencode/.cache:uid=1000,gid=1000,mode=0700 \
        --mount "type=volume,src=$data,dst=/knowledge/wiki,volume-subpath=wiki$suffix" \
        --mount "type=volume,src=$data,dst=/knowledge/sources,volume-subpath=sources,readonly" \
        --mount "type=volume,src=$data,dst=/knowledge/incoming/ingest-journal,volume-subpath=incoming/ingest-journal$suffix" \
        --mount "type=volume,src=$data,dst=/knowledge/incoming/answers,volume-subpath=incoming/answers$suffix" \
        --mount "type=volume,src=$data,dst=/home/opencode/.config/opencode,volume-subpath=opencode/config" \
        --mount "type=volume,src=$data,dst=/home/opencode/.local/share/opencode,volume-subpath=opencode/data" \
        --mount "type=volume,src=$data,dst=/home/opencode/.local/state,volume-subpath=opencode/state" \
        "$@" -w /knowledge/wiki "$image" serve --hostname 127.0.0.1 --port 4096 --log-level error --print-logs >/dev/null
}

wait_ready() {
    attempts=0
    until docker exec "$backend" sh -c 'curl -fsS -u "opencode:$OPENCODE_PASSWORD" http://127.0.0.1:4096/api/info >/dev/null' >/dev/null 2>&1; do
        attempts=$((attempts + 1))
        [ "$attempts" -lt 60 ] || { printf 'backend startup timed out\n' >&2; docker logs "$backend"; exit 1; }
        sleep 1
    done
}

# Deploy-time mount protection, not API hooks, changes the outcome.
for mode in false true; do
    WIKI_RUNTIME_READ_ONLY="$mode" docker compose --env-file "$root/.env.example" -f "$root/compose.yaml" config --format json | python3 -c '
import json, sys
service = json.load(sys.stdin)["services"]["opencode"]
enabled = sys.argv[1] == "true"
assert service["environment"]["WIKI_RUNTIME_READ_ONLY"] == sys.argv[1]
assert service.get("read_only", False) == enabled
volumes = {v["target"]: v for v in service["volumes"]}
for path in ("/knowledge/wiki", "/knowledge/incoming/ingest-journal", "/knowledge/incoming/answers"):
    assert volumes[path].get("read_only", False) == enabled
assert volumes["/knowledge/sources"]["read_only"]
assert service["cap_drop"] == ["ALL"]
assert "no-new-privileges:true" in service["security_opt"]
assert "/tmp:exec" in service["tmpfs"]
' "$mode"
    create_backend "$mode" "$backend"
    docker cp "$root/tests/integration/runtime_write_probe.js" "$backend:/home/opencode/.config/opencode/runtime_write_probe.js"
    # Install the probe into the mounted global plugin directory before startup.
    docker run --rm --network none --user 1000:1000 --entrypoint /bin/sh \
        --mount "type=volume,src=$data,dst=/knowledge" "$image" -c '
            mkdir -p /knowledge/opencode/config/plugins
            ln -sf ../runtime_write_probe.js /knowledge/opencode/config/plugins/runtime_write_probe.js
        '
    docker start "$backend" >/dev/null
    wait_ready
    # /tmp is ephemeral; copy the public probe after each start/restart.
    docker exec -i "$backend" sh -c 'cat > /tmp/runtime_write_isolation.py' < "$root/tests/integration/runtime_write_isolation.py"
    docker exec "$backend" opencode --version
    if [ "$mode" = false ]; then
        docker exec "$backend" python3 /tmp/runtime_write_isolation.py manual
    else
        if ! docker exec "$backend" python3 /tmp/runtime_write_isolation.py isolated; then
            docker logs "$backend"
            exit 1
        fi
        docker restart "$backend" >/dev/null
        wait_ready
        docker exec -i "$backend" sh -c 'cat > /tmp/runtime_write_isolation.py' < "$root/tests/integration/runtime_write_isolation.py"
        docker exec "$backend" python3 /tmp/runtime_write_isolation.py isolated
    fi
    docker rm -f "$backend" >/dev/null
done

# A writable alias, including an alternate mount of the backing volume, must
# fail before the server starts, not merely make one API request fail.
create_backend true "$rejected" --mount "type=volume,src=$data,dst=/tmp/writable-alias"
docker start "$rejected" >/dev/null
status=$(docker wait "$rejected")
[ "$status" = 1 ] || { printf 'unsafe topology was accepted\n' >&2; exit 1; }
docker logs "$rejected" 2>&1 | grep -q 'runtime-write-isolation: unsafe topology; refusing startup'
docker rm -f "$rejected" >/dev/null
create_backend false "$rejected" -e WIKI_RUNTIME_READ_ONLY=true
docker start "$rejected" >/dev/null
status=$(docker wait "$rejected")
[ "$status" = 1 ] || { printf 'read-only mode accepted writable mounts\n' >&2; exit 1; }
docker logs "$rejected" 2>&1 | grep -q 'runtime-write-isolation: unsafe topology; refusing startup'
printf 'runtime-write-isolation: OK; restart protected, writable backing alias and mismatched mode refused\n'
