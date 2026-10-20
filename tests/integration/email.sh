#!/bin/sh
# Disposable real TLS IMAP, durable archive, targeted delete/restart and replay.
set -eu
# shellcheck disable=SC1007
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
cd "$root"
temporary_root=${TMPDIR:-$HOME/.cache}
mkdir -p "$temporary_root"
scratch=$(mktemp -d "$temporary_root/kw-email.XXXXXX")
container=""
cleanup() {
  if [ -n "$container" ]; then docker rm -f "$container" >/dev/null 2>&1; fi
  rm -rf "$scratch"
}
trap cleanup EXIT
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -keyout "$scratch/key.pem" \
  -out "$scratch/cert.pem" -subj '/CN=localhost' -addext 'subjectAltName=DNS:localhost' \
  >/dev/null 2>&1
if [ "${SKIP_IMAGE_BUILD:-0}" != 1 ]; then
  docker build --target email -t kw-ingest-email:integration ingest/
fi
container=$(docker create --network none --user "$(id -u):$(id -g)" \
  --entrypoint python kw-ingest-email:integration /probe.py)
# Copy fixtures instead of host bind mounts, also supporting a remote daemon.
docker cp -a "$scratch" "$container:/data"
docker cp tests/integration/email_probe.py "$container:/probe.py"
docker start --attach "$container"
status=$(docker inspect --format '{{.State.ExitCode}}' "$container")
[ "$status" = 0 ] || exit "$status"
docker cp -a "$container:/data/sources" "$scratch/sources"
docker cp -a "$container:/data/wiki" "$scratch/wiki"
node --input-type=module - "$scratch" <<'JS'
import assert from 'node:assert/strict'
import path from 'node:path'
import { scanIngestStatus } from './config/tools/wiki_ingest_status_core.mjs'
const root = process.argv[2]
const result = await scanIngestStatus({
  sourceRoot: path.join(root, 'sources'),
  wikiSourceRoot: path.join(root, 'wiki/sources'),
})
assert.equal(result.summary.new, 4)
assert.equal(result.summary.invalid, 0)
console.log('email source-to-wiki scanner: OK')
JS
