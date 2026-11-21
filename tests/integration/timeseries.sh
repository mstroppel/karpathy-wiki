#!/bin/sh
# Disposable Compose instance; all data and model traffic are synthetic.
set -eu

# shellcheck disable=SC1007
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
export TIMESERIES_TEST_IMAGE="${BACKEND_IMAGE:-kw-opencode:integration}"
export TIMESERIES_FIXTURE="$root/tests/integration/timeseries_runtime.py"
docker image inspect "$TIMESERIES_TEST_IMAGE" >/dev/null

scratch=$(mktemp -d)
project="kw-timeseries-$(date +%s)-$$"
cd "$scratch"
compose() {
  docker compose --project-directory "$scratch" --env-file "$scratch/.env" \
    -f "$root/compose.yaml" -f "$root/tests/integration/timeseries-compose.yaml" "$@"
}
cleanup() {
  status=$?
  trap - EXIT
  if [ "$status" -ne 0 ]; then
    compose logs --no-color fixture timeseries-query opencode >&2 || true
  fi
  compose down --volumes --remove-orphans >/dev/null 2>&1 || true
  docker network rm "$project-proxy" >/dev/null 2>&1 || true
  rm -r -- "$scratch"
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

mkdir -p data/opencode/config
printf '%s\n' '["sensor.synthetic_owlet", "sensor.synthetic_room_temperature"]' \
  > entities.json
printf 'synthetic-recorder\n' > token
printf '%s\n' '{"people":[{"values":["Example Person"],"replacement":"[PERSON]"}]}' \
  > redactions.json
chmod 0600 token redactions.json
cat > data/opencode/config/opencode.json <<'EOF'
{"providers":{"fake":{"env":["FAKE_API_KEY"],"package":"@opencode/ai/providers/openai-compatible","settings":{"baseURL":"http://fixture:4081/v1"},"models":{"synthetic":{"limit":{"context":128000,"output":4096}}}}}}
EOF
cat > .env <<EOF
COMPOSE_PROJECT_NAME=$project
DATA_ROOT=$scratch/data
WEBPROXY_NETWORK=$project-proxy
OPENCODE_PASSWORD=synthetic-backend
OPENCHAMBER_UI_PASSWORD=synthetic-frontend
COMPOSE_PROFILES=timeseries
HOME_ASSISTANT_URL=http://fixture:4081
HOME_ASSISTANT_TOKEN_FILE=$scratch/token
REDACTIONS_FILE=$scratch/redactions.json
PUID=$(id -u)
PGID=$(id -g)
AUDIO_MAX_BYTES=
AUDIO_MAX_DURATION_SECONDS=
HUGGINGFACE_TOKEN=
EOF

check_entity_mount() {
  compose config --format json | python3 -c '
import json, sys

services = json.load(sys.stdin)["services"]
mount, = [m for m in services["timeseries-import"]["volumes"]
          if m["target"] == "/etc/wiki-timeseries/entities.json"]
assert mount["type"] == "bind" and mount["source"] == sys.argv[1], mount
assert mount["read_only"] and not mount["bind"]["create_host_path"], mount
for name in ("timeseries-query", "opencode"):
    assert not any(m["target"].startswith("/etc/wiki-timeseries")
                   for m in services[name]["volumes"]), name
' "$1"
}
check_entity_mount "$scratch/entities.json"
HOME_ASSISTANT_ENTITIES_FILE=./custom.json check_entity_mount "$scratch/custom.json"

docker network create --internal "$project-proxy" >/dev/null
compose up --pull never -d fixture
attempts=0
until compose exec -T fixture python3 -c \
  'from urllib.request import urlopen; urlopen("http://127.0.0.1:4081/ready", timeout=1)' \
  >/dev/null 2>&1; do
  attempts=$((attempts + 1))
  [ "$attempts" -lt 30 ] || exit 1
  sleep 1
done
compose run --pull never --rm timeseries-import once
test ! -e data/timeseries/config
compose up --pull never -d timeseries-query opencode
attempts=0
until compose exec -T timeseries-query test -S /timeseries-socket/query.sock >/dev/null 2>&1; do
  attempts=$((attempts + 1))
  [ "$attempts" -lt 30 ] || exit 1
  sleep 1
done

compose exec -T timeseries-query sh -ec '
  test "$(ls /sys/class/net)" = lo
  test ! -e /run/secrets/home_assistant_token
  test ! -e /run/secrets/redactions
  test ! -w /archive/recorder.sqlite3
'
compose exec -T opencode sh -ec '
  test ! -e /archive/recorder.sqlite3
  test ! -e /run/secrets/home_assistant_token
  test ! -e /run/secrets/redactions
  test ! -w /timeseries-socket
'
compose exec -T opencode python3 /fixture.py chat
compose exec -T opencode sh -ec 'test -z "$(git status --porcelain)"'
printf 'timeseries: synthetic chat and follow-up matched expected coverage\n'
