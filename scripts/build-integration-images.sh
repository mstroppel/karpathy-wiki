#!/bin/sh
# Load only the images needed by an isolated integration job.
set -eu
cd "$(dirname -- "$0")/.."

for target do
  case "$target" in
    opencode|openchamber|ingest|ingest-webdav|ingest-email|ingest-paperless) ;;
    *) printf 'Unsupported integration image: %s\n' "$target" >&2; exit 2 ;;
  esac
done
[ "$#" -gt 0 ] || exit 2

# Apply integration tags to the same definitions/cache scopes as publication.
docker buildx bake --file docker-bake.hcl --load \
  --set 'opencode.tags=kw-opencode:integration' \
  --set 'openchamber.tags=kw-openchamber:integration' \
  --set 'ingest.tags=kw-ingest:integration' \
  --set 'ingest-webdav.tags=kw-ingest-webdav:integration' \
  --set 'ingest-email.tags=kw-ingest-email:integration' \
  --set 'ingest-paperless.tags=kw-ingest-paperless:integration' "$@"
