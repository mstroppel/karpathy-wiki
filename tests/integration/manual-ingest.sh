#!/bin/sh
# Disposable named volumes/network and deterministic synthetic provider only.
set -eu
# shellcheck disable=SC1007
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
prefix="kw-manual-$(date +%s)-$$"
data="$prefix-data"
network="$prefix-net"
seed="$prefix-seed"
reader="$prefix-reader"
writer="$prefix-writer"
ui="$prefix-ui"
fake="$prefix-fake"
backend_image=${BACKEND_IMAGE:-kw-opencode:integration}
publisher_image=${PUBLISHER_IMAGE:-kw-publisher:integration}
chat_image=${CHAT_IMAGE:-kw-openchamber:integration}
cleanup() {
    status=$?
    trap - EXIT
    docker rm -f "$seed" "$reader" "$writer" "$ui" "$fake" >/dev/null 2>&1 || true
    docker volume rm "$data" >/dev/null 2>&1 || true
    docker network rm "$network" >/dev/null 2>&1 || true
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
docker network create --internal "$network" >/dev/null
docker volume create "$data" >/dev/null
docker run --rm --network none --user 0:0 --entrypoint /bin/sh \
    --mount "type=volume,src=$data,dst=/knowledge" \
    -e WIKI_NAME='Synthetic Wiki' -e WIKI_PUBLIC_URL=https://wiki.invalid \
    -e GIT_AUTHOR_NAME='Synthetic Test' -e GIT_AUTHOR_EMAIL=test@localhost \
    -e PUID=1000 -e PGID=1000 "$backend_image" /etc/opencode/init.sh >/dev/null
docker run -d --name "$seed" --network none --user 1000:1000 --entrypoint sleep \
    --mount "type=volume,src=$data,dst=/knowledge" "$publisher_image" infinity >/dev/null
docker cp "$root/tests/helpers/publisher-fixture.mjs" "$seed:/tmp/publisher-fixture.mjs"
docker exec "$seed" node --input-type=module -e 'import { publisherFixture } from "/tmp/publisher-fixture.mjs"; await publisherFixture("/knowledge");'
docker exec "$seed" node --input-type=module -e 'import { writeFile } from "node:fs/promises"; await writeFile("/knowledge/opencode/config/opencode.json", JSON.stringify({ providers: { fake: { env: ["FAKE_API_KEY"], package: "@opencode/ai/providers/openai-compatible", settings: { baseURL: "http://fake-model:4081/v1" }, models: { synthetic: { limit: { context: 128000, output: 4096 } } } } } }));'
docker create --name "$fake" --network "$network" --network-alias fake-model \
    --read-only --user 1000:1000 --entrypoint sleep --tmpfs /tmp \
    "$publisher_image" infinity >/dev/null
docker start "$fake" >/dev/null
docker exec -i "$fake" sh -c 'cat > /tmp/manual_fake_model.mjs' < "$root/tests/integration/manual_fake_model.mjs"
docker exec -d "$fake" node /tmp/manual_fake_model.mjs
OPENCODE_PASSWORD=$(od -An -N24 -tx1 </dev/urandom | tr -d ' \n')
OPENCHAMBER_UI_PASSWORD=$(od -An -N24 -tx1 </dev/urandom | tr -d ' \n')
WIKI_INGEST_CONTROL_TOKEN=$(od -An -N32 -tx1 </dev/urandom | tr -d ' \n')
export OPENCODE_PASSWORD OPENCHAMBER_UI_PASSWORD WIKI_INGEST_CONTROL_TOKEN
docker run -d --name "$reader" --network "$network" --network-alias opencode \
    --read-only --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges:true \
    -e OPENCODE_PASSWORD -e WIKI_RUNTIME_READ_ONLY=true -e FAKE_API_KEY=synthetic \
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
    "$backend_image" serve --hostname 0.0.0.0 --port 4096 >/dev/null
docker run -d --name "$writer" --network "$network" --network-alias manual-ingest \
    --read-only --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges:true --init \
    -e OPENCODE_PASSWORD -e WIKI_RUNTIME_READ_ONLY=true -e WIKI_INGEST_CONTROL_TOKEN \
    -e GIT_CONFIG_COUNT=1 -e GIT_CONFIG_KEY_0=core.hooksPath -e GIT_CONFIG_VALUE_0=/dev/null \
    --tmpfs /tmp:uid=1000,gid=1000,mode=0700,noexec \
    --mount "type=volume,src=$data,dst=/knowledge/wiki,volume-subpath=wiki" \
    --mount "type=volume,src=$data,dst=/knowledge/sources,volume-subpath=sources,readonly" \
    --mount "type=volume,src=$data,dst=/knowledge/incoming/ingest-journal,volume-subpath=incoming/ingest-journal" \
    --mount "type=volume,src=$data,dst=/knowledge/publisher,volume-subpath=publisher" \
    --entrypoint /bin/sh "$publisher_image" /opt/karpathy-wiki/publisher/manual-entrypoint.sh >/dev/null
docker run -d --name "$ui" --network "$network" --read-only --user 1000:1000 \
    --cap-drop ALL --security-opt no-new-privileges:true --tmpfs /tmp \
    -e OPENCODE_PASSWORD -e OPENCHAMBER_UI_PASSWORD -e WIKI_INGEST_CONTROL_TOKEN \
    -e WIKI_INGEST_CONTROL_URL=http://manual-ingest:4080 -e WIKI_CHAT_ORIGIN=http://127.0.0.1:3000 \
    -e OPENCHAMBER_DATA_DIR=/home/openchamber/.config/openchamber \
    -e OPENCODE_HOST=http://opencode:4096 -e OPENCODE_SKIP_START=true -e OPENCHAMBER_OPENCODE_CWD=/knowledge/wiki \
    -e OPENCHAMBER_RELAY_HOST=off \
    --mount "type=volume,src=$data,dst=/knowledge/wiki,volume-subpath=wiki,readonly" \
    --mount "type=volume,src=$data,dst=/knowledge/incoming/ingest-journal/runs,volume-subpath=incoming/ingest-journal/runs,readonly" \
    --mount "type=volume,src=$data,dst=/home/openchamber/.config/openchamber,volume-subpath=openchamber" \
    "$chat_image" >/dev/null
attempts=0
until docker exec "$ui" node /etc/openchamber/healthcheck.mjs >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    [ "$attempts" -lt 90 ] || { printf 'manual-ingest startup timed out\n' >&2; exit 1; }
    sleep 1
done
docker exec "$reader" python3 -c 'import os; assert "WIKI_INGEST_CONTROL_TOKEN" not in os.environ'
if docker exec "$writer" python3 /opt/karpathy-wiki/publisher/service.py; then
    printf 'competing lifetime publisher accepted\n' >&2
    exit 1
else
    [ "$?" = 1 ] || exit 1
fi
docker exec -i "$ui" sh -c 'cat > /tmp/manual_ingest_chat.mjs' < "$root/tests/integration/manual_ingest_chat.mjs"
baseline=$(docker exec "$seed" git -C /knowledge/wiki rev-list --count HEAD)
docker exec "$ui" node /tmp/manual_ingest_chat.mjs
[ "$(docker exec "$seed" git -C /knowledge/wiki rev-list --count HEAD)" -eq "$((baseline + 1))" ]
docker exec "$seed" node --input-type=module -e 'import { readFile, writeFile } from "node:fs/promises"; import { createHash } from "node:crypto"; const text="Synthetic changed revision, line 1.\n"; await writeFile("/knowledge/sources/webdav/notes.md",text); const file="/knowledge/sources/webdav/manifest.json"; const manifest=JSON.parse(await readFile(file,"utf8")); const revision=createHash("sha256").update(text).digest("hex"); manifest.items[0].source_revision=revision; manifest.items[0].frontmatter.source_revision=revision; await writeFile(file,JSON.stringify(manifest));'
docker exec "$ui" node /tmp/manual_ingest_chat.mjs
[ "$(docker exec "$seed" git -C /knowledge/wiki rev-list --count HEAD)" -eq "$((baseline + 2))" ]
docker restart "$writer" >/dev/null
docker exec "$ui" node /tmp/manual_ingest_chat.mjs noop
[ "$(docker exec "$seed" git -C /knowledge/wiki rev-list --count HEAD)" -eq "$((baseline + 2))" ]
docker exec -i "$reader" sh -c 'cat > /tmp/runtime_write_isolation.py' < "$root/tests/integration/runtime_write_isolation.py"
docker exec -i "$reader" sh -c 'cat > /tmp/publisher_reader_probe.py' < "$root/tests/integration/publisher_reader_probe.py"
docker exec "$reader" python3 /tmp/publisher_reader_probe.py
