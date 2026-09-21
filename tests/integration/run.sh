#!/bin/sh
# Disposable Compose integration test for startup, restart, health, and
# failure behavior of the composed stack.
#
# Runs against locally built images in an isolated project with a temporary
# data root; every artifact is removed afterwards. Requires Docker with
# Compose v2. Intentionally no external backend: the WebDAV and Paperless
# ingests fail fast against unreachable test endpoints, which exercises the
# daemon retry, health, and failure behavior.
set -eu

# shellcheck disable=SC1007 # CDPATH is intentionally cleared for this command only
repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
scratch_dir=$(mktemp -d "${TMPDIR:-/tmp}/karpathy-wiki-integration.XXXXXX")
project_name="kw-it-$(date +%s)-$$"
network_name="${project_name}-net"

cd "$repository_root"

teardown() {
  status=$?
  set +e
  docker compose --env-file "${env_file:-}" -f compose.yaml \
    -f tests/integration/compose.integration.yaml \
    down -v --remove-orphans --timeout 30 >/dev/null 2>&1
  docker network rm "$network_name" >/dev/null 2>&1
  rm -rf "$scratch_dir"
  if [ "$status" -eq 0 ]; then
    printf 'compose-integration: OK\n'
  else
    printf 'compose-integration: FAILED\n' >&2
  fi
  return $status
}
trap teardown EXIT

fail() {
  printf 'compose-integration: %s\n' "$1" >&2
  exit 1
}

compose() {
  docker compose --env-file "$env_file" -f compose.yaml \
    -f tests/integration/compose.integration.yaml "$@"
}

# ---------------------------------------------------------------- fixtures
mkdir -p "$scratch_dir/data" "$scratch_dir/secrets"
cp tests/integration/fixtures/redactions.json "$scratch_dir/secrets/redactions.json"
printf 'integration-token\n' >"$scratch_dir/secrets/paperless-token"

password=$(od -An -N24 -tx1 </dev/urandom | tr -d ' \n')
env_file="$scratch_dir/env"
cat >"$env_file" <<EOF
COMPOSE_PROJECT_NAME=$project_name
STACK_ID=$project_name
WIKI_NAME=Integration Wiki
WIKI_PUBLIC_URL=https://wiki.invalid
OPENCODE_PASSWORD=$password
DATA_ROOT=$scratch_dir/data
WEBPROXY_NETWORK=$network_name
KARPATHY_WIKI_VERSION=integration
COMPOSE_PROFILES=webdav,paperless
PAPERLESS_TOKEN_FILE=$scratch_dir/secrets/paperless-token
REDACTIONS_FILE=$scratch_dir/secrets/redactions.json
PAPERLESS_SOURCE_TAG_ID=5
# Unreachable, fast-failing local endpoints for the failure test: the WebDAV
# and Paperless URLs point at the refused TCP discard port instead of an
# external hostname whose DNS behavior would make the assertions slow or
# nondeterministic.
WEBDAV_URL=https://127.0.0.1:9/
PAPERLESS_PUBLIC_URL=https://127.0.0.1:9
EOF

docker network create "$network_name" >/dev/null || fail "cannot create test network"

# ------------------------------------------------------------------ build
docker build -q -t kw-opencode:integration -f opencode/Dockerfile . \
  || fail "cannot build the opencode image"
docker build -q -t kw-ingest:integration ingest/ \
  || fail "cannot build the ingest image"

# ---------------------------------------------------------------- startup
printf 'compose-integration: starting the stack\n'
compose up -d --wait --wait-timeout 240 opencode silverbullet \
  || fail "opencode/silverbullet did not start"

init_exit=$(docker inspect -f '{{.State.ExitCode}}' "$project_name-init-1" 2>/dev/null || true)
[ "$init_exit" = "0" ] || fail "init did not complete successfully (exit: $init_exit)"

compose exec -T opencode curl -sS -o /dev/null http://127.0.0.1:4096/ \
  || fail "opencode does not answer HTTP on port 4096"

i=0
until compose exec -T opencode curl -sS -o /dev/null http://silverbullet:3000/ 2>/dev/null; do
  i=$((i + 1))
  [ "$i" -lt 30 ] || fail "silverbullet does not answer HTTP on port 3000"
  sleep 2
done
printf 'compose-integration: startup OK\n'

# ------------------------------------------------------------- webdav health
printf 'compose-integration: checking ingest daemon lifecycle and health\n'
compose up -d webdav-ingest || fail "cannot start webdav-ingest"

i=0
until docker inspect -f '{{.State.Running}}' "$project_name-webdav-ingest-1" 2>/dev/null | grep -q true; do
  i=$((i + 1))
  [ "$i" -lt 30 ] || fail "webdav-ingest is not running"
  sleep 2
done

i=0
until compose exec -T webdav-ingest test -f /tmp/health.json 2>/dev/null; do
  i=$((i + 1))
  [ "$i" -lt 60 ] || fail "webdav-ingest never wrote its health record"
  sleep 2
done
# The upstream WebDAV backend is unreachable in the test, so the failed
# synchronization must be visible in the health record.
if compose exec -T webdav-ingest python -m karpathy_wiki_ingest.health; then
  fail "healthcheck passed despite a failed synchronization"
fi

# ----------------------------------------------------------------- restart
printf 'compose-integration: restarting opencode\n'
compose restart opencode || fail "cannot restart opencode"
i=0
until compose exec -T opencode curl -sS -o /dev/null http://127.0.0.1:4096/ 2>/dev/null; do
  i=$((i + 1))
  [ "$i" -lt 30 ] || fail "opencode did not come back after restart"
  sleep 2
done
printf 'compose-integration: restart OK\n'

# ----------------------------------------------------------------- failure
printf 'compose-integration: checking one-shot failure exits\n'
compose run --rm webdav-ingest webdav --once >/dev/null 2>&1 &&
  fail "webdav --once unexpectedly succeeded without an upstream"
compose run --rm paperless-ingest paperless --once >/dev/null 2>&1 &&
  fail "paperless --once unexpectedly succeeded without an upstream"
printf 'compose-integration: failure exits OK\n'
