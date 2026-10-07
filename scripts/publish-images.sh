#!/bin/sh
# Retry registry publication using the same builder cache and image version.
set -eu

cd "$(dirname -- "$0")/.."

case "${1:-}" in
  release|stable) target=$1 ;;
  *) printf 'Usage: sh scripts/publish-images.sh release|stable\n' >&2; exit 2 ;;
esac
: "${IMAGE_VERSION:?IMAGE_VERSION must be set}"

attempt=1
while :; do
  if docker buildx bake --file docker-bake.hcl --push \
    --set '*.platform=linux/amd64,linux/arm64' "$target"; then
    exit 0
  else
    status=$?
  fi
  if [ "$attempt" -ge 3 ]; then
    printf 'Image publication failed after %s attempts\n' "$attempt" >&2
    exit "$status"
  fi
  delay=$((attempt * 30))
  printf 'Image publication attempt %s failed; retrying in %ss\n' "$attempt" "$delay" >&2
  sleep "$delay"
  attempt=$((attempt + 1))
done
