#!/bin/sh
# Disposable integration test of the shipped primary chat image.
# Images must already exist locally; no host data/configuration is mounted.
set -eu

OPENCHAMBER_SMOKE_IMAGE=${CHAT_IMAGE:-kw-openchamber:integration}
OPENCODE_SMOKE_IMAGE=${BACKEND_IMAGE:-kw-opencode:integration}

# shellcheck disable=SC1007
repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
prefix="kw-oc-smoke-$(date +%s)-$$"
backend="$prefix-opencode"
frontend="$prefix-openchamber"
data="$prefix-data"
home="$prefix-home"
network="$prefix-net"
OPENCODE_PASSWORD=$(od -An -N24 -tx1 </dev/urandom | tr -d ' \n')
OPENCHAMBER_UI_PASSWORD=$(od -An -N24 -tx1 </dev/urandom | tr -d ' \n')
export OPENCODE_PASSWORD OPENCHAMBER_UI_PASSWORD

cleanup() {
  status=$?
  trap - EXIT
  docker rm -f "$frontend" "$backend" >/dev/null 2>&1 || true
  docker volume rm "$home" "$data" >/dev/null 2>&1 || true
  docker network rm "$network" >/dev/null 2>&1 || true
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

docker image inspect "$OPENCHAMBER_SMOKE_IMAGE" "$OPENCODE_SMOKE_IMAGE" >/dev/null
docker network create --internal "$network" >/dev/null
docker volume create "$data" >/dev/null
docker volume create "$home" >/dev/null
docker run --rm --network none --user 0:0 --entrypoint /bin/sh \
  --mount "type=volume,src=$data,dst=/knowledge" \
  -e WIKI_NAME="Disposable Wiki" -e WIKI_PUBLIC_URL=https://wiki.invalid \
  -e GIT_AUTHOR_NAME="Smoke Test" -e GIT_AUTHOR_EMAIL=smoke@localhost \
  -e PUID=1000 -e PGID=1000 -e COMPOSE_PROFILES=answers \
  "$OPENCODE_SMOKE_IMAGE" /etc/opencode/init.sh >/dev/null
docker run --rm --network none --user 0:0 --entrypoint /bin/sh \
  --mount "type=volume,src=$home,dst=/settings" \
  "$OPENCODE_SMOKE_IMAGE" -c 'chown 1000:1000 /settings'

docker run -d --name "$backend" --network "$network" --network-alias opencode \
  --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges:true \
  -e OPENCODE_PASSWORD -e OPENCODE_CONFIG=/etc/opencode/opencode.json \
  -e OPENCODE_DISABLE_PROJECT_CONFIG=1 -e OPENCODE_DISABLE_EXTERNAL_SKILLS=1 \
  -e OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1 \
  --mount "type=volume,src=$data,dst=/knowledge/wiki,volume-subpath=wiki" \
  --mount "type=volume,src=$data,dst=/knowledge/sources,volume-subpath=sources,readonly" \
  --mount "type=volume,src=$data,dst=/knowledge/incoming/answers,volume-subpath=incoming/answers" \
  --mount "type=volume,src=$data,dst=/home/opencode/.local/share/opencode,volume-subpath=opencode/data" \
  -w /knowledge/wiki "$OPENCODE_SMOKE_IMAGE" \
  serve --hostname 0.0.0.0 --port 4096 >/dev/null

docker run -d --name "$frontend" --network "$network" \
  --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges:true \
  --read-only --tmpfs /tmp -e OPENCHAMBER_RELAY_HOST=off \
  -e OPENCODE_DISABLE_EXTERNAL_SKILLS=1 -e OPENCODE_DISABLE_CLAUDE_CODE_SKILLS=1 \
  -e OPENCODE_HOST=http://opencode:4096 -e OPENCODE_SKIP_START=true \
  -e OPENCHAMBER_OPENCODE_CWD=/knowledge/wiki \
  -e OPENCODE_PASSWORD -e OPENCHAMBER_UI_PASSWORD \
  --mount "type=volume,src=$data,dst=/knowledge/wiki,volume-subpath=wiki,readonly" \
  --mount "type=volume,src=$home,dst=/home/openchamber/.config/openchamber" \
  -w /knowledge/wiki "$OPENCHAMBER_SMOKE_IMAGE" >/dev/null

wait_ready() {
  attempts=0
  until docker exec "$frontend" node -e \
    'fetch("http://127.0.0.1:3000/health").then(r=>r.json()).then(h=>{if(!h.isOpenCodeReady)process.exit(1)}).catch(()=>process.exit(1))' >/dev/null 2>&1; do
    attempts=$((attempts + 1))
    [ "$attempts" -lt 60 ] || { printf 'openchamber-smoke: startup timed out\n' >&2; exit 1; }
    sleep 2
  done
}
wait_ready
docker exec -i "$frontend" sh -c 'cat > /tmp/chat.mjs' < "$repository_root/tests/integration/chat.mjs"
docker exec "$frontend" node /tmp/chat.mjs create

# These checks concern mounts/process access, not API authorization or model safety.
if docker exec "$frontend" sh -c 'touch /knowledge/wiki/.smoke-write' >/dev/null 2>&1; then
  printf 'openchamber-smoke: frontend can write the wiki\n' >&2
  exit 1
fi
if docker exec "$backend" sh -c 'touch /knowledge/sources/.smoke-write' >/dev/null 2>&1; then
  printf 'openchamber-smoke: backend can write sources\n' >&2
  exit 1
fi
docker exec "$frontend" sh -c 'test ! -e /knowledge/sources && test ! -e /knowledge/incoming && test ! -e /var/run/docker.sock && test ! -e /home/opencode'
docker exec "$backend" sh -c 'test -w /knowledge/wiki/index.md'
processes=$(docker top "$frontend" -eo pid,args)
if printf '%s\n' "$processes" | grep -E '(^|[ /])opencode serve' >/dev/null; then
  printf 'openchamber-smoke: frontend started another OpenCode server\n' >&2
  exit 1
fi

docker restart "$backend" "$frontend" >/dev/null
wait_ready
docker exec -i "$frontend" sh -c 'cat > /tmp/chat.mjs' < "$repository_root/tests/integration/chat.mjs"
docker exec "$frontend" node /tmp/chat.mjs restart
printf 'openchamber-smoke: OK; disposable containers and volumes will be removed\n'
