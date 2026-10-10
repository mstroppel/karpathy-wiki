#!/bin/sh
# Disposable named volumes only, real pinned runtime, no ports/network/models.
set -eu
# shellcheck disable=SC1007
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
image=${PUBLISHER_IMAGE:-kw-publisher:integration}
backend_image=${BACKEND_IMAGE:-kw-opencode:integration}
prefix="kw-publisher-$(date +%s)-$$"
data="$prefix-data"
writer="$prefix-writer"
reader="$prefix-reader"
seed="$prefix-seed"
rejected="$prefix-rejected"
cleanup() {
    status=$?
    trap - EXIT
    docker rm -f "$writer" "$reader" "$seed" "$rejected" >/dev/null 2>&1 || true
    docker volume rm "$data" >/dev/null 2>&1 || true
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
WIKI_RUNTIME_READ_ONLY=true COMPOSE_PROFILES=publisher docker compose --env-file "$root/.env.example" -f "$root/compose.yaml" config --format json | python3 -c '
import json, sys
services = json.load(sys.stdin)["services"]
publisher = services["publisher"]
assert publisher["network_mode"] == "none"
assert publisher["read_only"] and publisher["cap_drop"] == ["ALL"]
assert "no-new-privileges:true" in publisher["security_opt"]
mounts = {mount["target"]: mount for mount in publisher["volumes"]}
assert set(mounts) == {"/knowledge/wiki", "/knowledge/sources", "/knowledge/incoming/ingest-journal", "/knowledge/publisher"}
assert mounts["/knowledge/sources"]["read_only"]
assert not mounts["/knowledge/wiki"].get("read_only", False)
assert not any("PASSWORD" in name or "TOKEN" in name for name in publisher["environment"])
assert not any(mount["target"] == "/knowledge/publisher" for mount in services["opencode"]["volumes"])
'
docker volume create "$data" >/dev/null
docker run --rm --network none --user 0:0 --entrypoint /bin/sh \
    --mount "type=volume,src=$data,dst=/knowledge" \
    -e WIKI_NAME='Synthetic Wiki' -e WIKI_PUBLIC_URL=https://wiki.invalid \
    -e GIT_AUTHOR_NAME='Synthetic Test' -e GIT_AUTHOR_EMAIL=test@localhost \
    -e PUID=1000 -e PGID=1000 "$backend_image" /etc/opencode/init.sh >/dev/null
docker create --name "$seed" --network none --user 1000:1000 --entrypoint sleep \
    --mount "type=volume,src=$data,dst=/knowledge" "$image" infinity >/dev/null
docker cp "$root/tests/helpers/publisher-fixture.mjs" "$seed:/tmp/publisher-fixture.mjs"
docker start "$seed" >/dev/null
docker exec "$seed" node --input-type=module -e 'import { publisherFixture } from "/tmp/publisher-fixture.mjs"; await publisherFixture("/knowledge")'
docker rm -f "$seed" >/dev/null
docker create --name "$writer" --network none --read-only --user 1000:1000 \
    --cap-drop ALL --security-opt no-new-privileges:true --init \
    -e WIKI_RUNTIME_READ_ONLY=true \
    -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=core.hooksPath -e GIT_CONFIG_VALUE_0=/dev/null \
    --tmpfs /tmp:uid=1000,gid=1000,mode=0700,noexec \
    --mount "type=volume,src=$data,dst=/knowledge/wiki,volume-subpath=wiki" \
    --mount "type=volume,src=$data,dst=/knowledge/sources,volume-subpath=sources,readonly" \
    --mount "type=volume,src=$data,dst=/knowledge/incoming/ingest-journal,volume-subpath=incoming/ingest-journal" \
    --mount "type=volume,src=$data,dst=/knowledge/publisher,volume-subpath=publisher" \
    "$image" >/dev/null
OPENCODE_PASSWORD=$(od -An -N24 -tx1 </dev/urandom | tr -d ' \n')
export OPENCODE_PASSWORD
docker run -d --name "$reader" --network none --read-only --user 1000:1000 \
    --cap-drop ALL --security-opt no-new-privileges:true \
    -e OPENCODE_PASSWORD -e WIKI_RUNTIME_READ_ONLY=true \
    -e OPENCODE_CONFIG=/etc/opencode/opencode.json -e OPENCODE_DISABLE_PROJECT_CONFIG=1 \
    -e OPENCODE_DISABLE_EXTERNAL_SKILLS=1 -e OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1 \
    --tmpfs /tmp:exec --tmpfs /home/opencode/.cache:uid=1000,gid=1000,mode=0700 \
    --mount "type=volume,src=$data,dst=/knowledge/wiki,volume-subpath=wiki,readonly" \
    --mount "type=volume,src=$data,dst=/knowledge/sources,volume-subpath=sources,readonly" \
    --mount "type=volume,src=$data,dst=/knowledge/incoming/ingest-journal,volume-subpath=incoming/ingest-journal,readonly" \
    --mount "type=volume,src=$data,dst=/knowledge/incoming/answers,volume-subpath=incoming/answers,readonly" \
    --mount "type=volume,src=$data,dst=/home/opencode/.config/opencode,volume-subpath=opencode/config" \
    --mount "type=volume,src=$data,dst=/home/opencode/.local/share/opencode,volume-subpath=opencode/data" \
    --mount "type=volume,src=$data,dst=/home/opencode/.local/state,volume-subpath=opencode/state" \
    "$backend_image" serve --hostname 127.0.0.1 --port 4096 >/dev/null
docker start "$writer" >/dev/null
attempts=0
until docker exec "$writer" test -f /tmp/publisher-boot-id; do
    attempts=$((attempts + 1))
    [ "$attempts" -lt 30 ] || { printf 'publisher startup timed out\n' >&2; exit 1; }
    sleep 1
done
# A second container cannot supply a different boot ID as false restart proof.
if docker run --name "$rejected" --network none --read-only --user 1000:1000 \
    --cap-drop ALL --security-opt no-new-privileges:true -e WIKI_RUNTIME_READ_ONLY=true \
    --tmpfs /tmp:uid=1000,gid=1000,mode=0700,noexec \
    --mount "type=volume,src=$data,dst=/knowledge/publisher,volume-subpath=publisher" \
    "$image"; then
    printf 'competing live publisher accepted\n' >&2
    exit 1
else
    [ "$?" = 1 ] || exit 1
fi
docker rm -f "$rejected" >/dev/null
copy_writer_probe() {
    docker exec -i "$writer" sh -c 'cat > /tmp/publisher-fixture.mjs' < "$root/tests/helpers/publisher-fixture.mjs"
    docker exec -i "$writer" sh -c 'cat > /tmp/trusted_publisher_probe.mjs' < "$root/tests/integration/trusted_publisher_probe.mjs"
}
copy_writer_probe
docker exec -i "$reader" sh -c 'cat > /tmp/runtime_write_isolation.py' < "$root/tests/integration/runtime_write_isolation.py"
docker exec -i "$reader" sh -c 'cat > /tmp/publisher_reader_probe.py' < "$root/tests/integration/publisher_reader_probe.py"
docker exec "$reader" opencode --version
docker exec "$writer" node /tmp/trusted_publisher_probe.mjs start
docker exec "$reader" python3 /tmp/publisher_reader_probe.py
docker exec "$writer" node /tmp/trusted_publisher_probe.mjs crash >/dev/null 2>&1 &
crash_pid=$!
attempts=0
until docker exec "$writer" test -f /tmp/crash-boundary; do
    attempts=$((attempts + 1))
    [ "$attempts" -lt 60 ] || { printf 'publication crash boundary timed out\n' >&2; exit 1; }
    sleep 1
done
docker restart "$writer" >/dev/null
wait "$crash_pid" || true
attempts=0
until docker exec "$writer" test -f /tmp/publisher-boot-id; do
    attempts=$((attempts + 1))
    [ "$attempts" -lt 30 ] || exit 1
    sleep 1
done
copy_writer_probe
docker exec "$writer" node /tmp/trusted_publisher_probe.mjs recover
docker exec "$reader" python3 /tmp/publisher_reader_probe.py
printf 'trusted-publisher: OK; no runtime publisher channel, one verified commit after crash/restart\n'
