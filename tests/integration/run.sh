#!/bin/sh
# Disposable Compose integration test for successful WebDAV ingestion,
# source-to-wiki status/publication, restart, health, and failure behavior.
#
# Runs against locally built images in an isolated project with a temporary
# data root; every artifact is removed afterwards. Requires Docker with
# Compose v2. A disposable rclone WebDAV upstream serves the success fixture;
# the failure phase switches to refused local endpoints. No model credentials
# are needed: publication is deterministic test code using the real manifest
# and status scanner, not an automated production publisher.
set -eu

# shellcheck disable=SC1007 # CDPATH is intentionally cleared for this command only
repository_root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
scratch_dir=$(mktemp -d "${TMPDIR:-/tmp}/karpathy-wiki-integration.XXXXXX")
project_name="kw-it-$(date +%s)-$$"
network_name="${project_name}-net"

cd "$repository_root"

restore_host_ownership() {
  # init writes generated wiki files and Git history as root after chowning
  # the data root. The host test runner needs to publish and commit pages.
  docker run --rm --user 0:0 --entrypoint chown \
    -v "$scratch_dir/data:/data" kw-opencode:integration \
    -R "$(id -u):$(id -g)" /data
}

teardown() {
  status=$?
  set +e
  docker compose --env-file "${env_file:-}" -f compose.yaml \
    -f tests/integration/compose.integration.yaml \
    down -v --remove-orphans --timeout 30 >/dev/null 2>&1
  docker network rm "$network_name" >/dev/null 2>&1
  restore_host_ownership >/dev/null 2>&1
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
mkdir -p "$scratch_dir/data" "$scratch_dir/secrets" "$scratch_dir/fixture/Wiki Sources"
cp tests/integration/fixtures/redactions.json "$scratch_dir/secrets/redactions.json"
printf 'integration-token\n' >"$scratch_dir/secrets/paperless-token"
printf '# Meeting\n\nMax Mustermann approved the first draft.\n' >"$scratch_dir/fixture/Wiki Sources/notes.md"

password=$(od -An -N24 -tx1 </dev/urandom | tr -d ' \n')
env_file="$scratch_dir/env"
cat >"$env_file" <<EOF
COMPOSE_PROJECT_NAME=$project_name
STACK_ID=$project_name
WIKI_NAME=Integration Wiki
WIKI_PUBLIC_URL=https://wiki.invalid
OPENCODE_PASSWORD=$password
DATA_ROOT=$scratch_dir/data
INTEGRATION_FIXTURE_ROOT=$scratch_dir/fixture
PUID=$(id -u)
PGID=$(id -g)
WEBPROXY_NETWORK=$network_name
KARPATHY_WIKI_VERSION=integration
COMPOSE_PROFILES=webdav
PAPERLESS_TOKEN_FILE=$scratch_dir/secrets/paperless-token
REDACTIONS_FILE=$scratch_dir/secrets/redactions.json
PAPERLESS_SOURCE_TAG_ID=5
WEBDAV_URL=http://webdav-fixture:8080/
WEBDAV_VENDOR=other
WEBDAV_USERNAME=fixture
PAPERLESS_PUBLIC_URL=https://127.0.0.1:9
EOF

docker network create "$network_name" >/dev/null || fail "cannot create test network"

# ------------------------------------------------------------------ build
docker build -q -t kw-opencode:integration -f opencode/Dockerfile . \
  || fail "cannot build the opencode image"
docker build -q -t kw-ingest-webdav:integration --target webdav ingest/ \
  || fail "cannot build the webdav ingest image"
docker build -q -t kw-ingest-paperless:integration --target paperless ingest/ \
  || fail "cannot build the paperless ingest image"
obscured_password=$(docker run --rm --entrypoint rclone kw-ingest-webdav:integration obscure integration-only) \
  || fail "cannot obscure the WebDAV test password"
printf 'WEBDAV_PASSWORD_OBSCURED=%s\n' "$obscured_password" >>"$env_file"

# ---------------------------------------------------------------- startup
printf 'compose-integration: starting the stack\n'
compose up -d --wait --wait-timeout 240 opencode silverbullet \
  || fail "opencode/silverbullet did not start"

init_exit=$(docker inspect -f '{{.State.ExitCode}}' "$project_name-init-1" 2>/dev/null || true)
[ "$init_exit" = "0" ] || fail "init did not complete successfully (exit: $init_exit)"
restore_host_ownership || fail "cannot give the host runner access to initialized wiki files"

i=0
until compose exec -T opencode curl -sS -o /dev/null http://127.0.0.1:4096/ 2>/dev/null; do
  i=$((i + 1))
  [ "$i" -lt 30 ] || fail "opencode does not answer HTTP on port 4096"
  sleep 2
done

i=0
until compose exec -T opencode curl -sS -o /dev/null http://silverbullet:3000/ 2>/dev/null; do
  i=$((i + 1))
  [ "$i" -lt 30 ] || fail "silverbullet does not answer HTTP on port 3000"
  sleep 2
done
silverbullet_space_writable=$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/space"}}{{.RW}}{{end}}{{end}}' "$project_name-silverbullet-1")
[ "$silverbullet_space_writable" = false ] || fail "silverbullet wiki mount is not read-only"
if compose exec -T silverbullet sh -c 'touch /space/.integration-write-check' >/dev/null 2>&1; then
  fail "silverbullet can modify the wiki space"
fi
if [ -e "$scratch_dir/data/wiki/.integration-write-check" ]; then
  fail "silverbullet created a file in the wiki space"
fi
printf 'compose-integration: startup OK\n'

# ----------------------------------------------------- successful publication
printf 'compose-integration: syncing a disposable WebDAV source\n'
compose up -d webdav-fixture || fail "cannot start WebDAV fixture"
i=0
until compose exec -T opencode curl -fsS -u fixture:integration-only \
  -X PROPFIND http://webdav-fixture:8080/ >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -lt 30 ] || fail "WebDAV fixture did not become ready"
  sleep 2
done
compose run --rm webdav-ingest webdav --once || fail "WebDAV initial sync failed"
node tests/integration/publication.mjs "$scratch_dir/data" new publish \
  || fail "new source was not published as a current wiki page"
git -C "$scratch_dir/data/wiki" add sources/webdav/notes.md/index.md
git -C "$scratch_dir/data/wiki" -c commit.gpgsign=false commit -m "docs(wiki): import integration source" \
  || fail "cannot commit the first wiki source page"
printf '# Meeting\n\nMax Mustermann approved the revised draft.\n' >"$scratch_dir/fixture/Wiki Sources/notes.md"
compose run --rm webdav-ingest webdav --once || fail "WebDAV update sync failed"
node tests/integration/publication.mjs "$scratch_dir/data" outdated publish \
  || fail "updated source was not republished"
git -C "$scratch_dir/data/wiki" add sources/webdav/notes.md/index.md
git -C "$scratch_dir/data/wiki" -c commit.gpgsign=false commit -m "docs(wiki): update integration source" \
  || fail "cannot commit the updated wiki source page"
node tests/integration/publication.mjs "$scratch_dir/data" current \
  || fail "wiki status was not current"
commit_count=$(git -C "$scratch_dir/data/wiki" rev-list --count HEAD)
[ "$commit_count" -eq 3 ] || fail "expected an initial and two source commits (got $commit_count)"
if grep -R -F 'Max Mustermann' "$scratch_dir/data/sources" "$scratch_dir/data/wiki/sources"; then
  fail "raw upstream name escaped into published data"
fi
printf 'compose-integration: publication OK\n'

# ------------------------------------------------------------- webdav health
printf 'compose-integration: checking ingest daemon lifecycle and health\n'
compose up -d webdav-ingest || fail "cannot start webdav-ingest"
i=0
until compose exec -T webdav-ingest test -f /tmp/health.json 2>/dev/null; do
  i=$((i + 1))
  [ "$i" -lt 60 ] || fail "healthy webdav-ingest never wrote its health record"
  sleep 2
done
compose exec -T webdav-ingest python -m karpathy_wiki_ingest.health \
  || fail "successful synchronization was not healthy"
# Refused local port exercises the failed sync without relying on DNS or an
# external service. Recreate the daemon with the new endpoint after success.
sed 's|^WEBDAV_URL=.*|WEBDAV_URL=https://127.0.0.1:9/|' "$env_file" >"$env_file.next"
mv "$env_file.next" "$env_file"
compose up -d --force-recreate webdav-ingest || fail "cannot recreate webdav-ingest"

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
node tests/integration/publication.mjs "$scratch_dir/data" current \
  || fail "failed upstream sync changed the published source or wiki status"

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
node tests/integration/publication.mjs "$scratch_dir/data" current \
  || fail "publication did not survive opencode restart"
[ "$(git -C "$scratch_dir/data/wiki" rev-list --count HEAD)" -eq 3 ] \
  || fail "wiki Git history changed after opencode restart"

# ----------------------------------------------------------------- failure
printf 'compose-integration: checking one-shot failure exits\n'
compose run --rm webdav-ingest webdav --once >/dev/null 2>&1 &&
  fail "webdav --once unexpectedly succeeded without an upstream"
mkdir -p "$scratch_dir/data/sources/paperless" "$scratch_dir/data/quarantine/paperless"
compose run --rm paperless-ingest paperless --once >/dev/null 2>&1 &&
  fail "paperless --once unexpectedly succeeded without an upstream"
printf 'compose-integration: failure exits OK\n'
