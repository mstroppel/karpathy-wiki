#!/bin/sh
set -eu

repository="mstroppel/karpathy-wiki"
install_dir=${INSTALL_DIR:-karpathy-wiki}
version=${KARPATHY_WIKI_VERSION:-}

if ! command -v curl >/dev/null 2>&1; then
  printf 'curl is required\n' >&2
  exit 1
fi

if [ -z "$install_dir" ]; then
  printf 'INSTALL_DIR must not be empty\n' >&2
  exit 1
fi

if [ -e "$install_dir/compose.yaml" ] || [ -e "$install_dir/.env" ]; then
  printf '%s already contains compose.yaml or .env\n' "$install_dir" >&2
  exit 1
fi

if [ -z "$version" ]; then
  release_url=$(curl -fsSL -o /dev/null -w '%{url_effective}' \
    "https://github.com/$repository/releases/latest")
  tag=${release_url##*/}
  version=${tag#v}
else
  version=${version#v}
  tag="v$version"
fi

case "$version" in
  ''|*[!0-9A-Za-z.-]*|.*|*..*|*.)
    printf 'Could not determine a valid release version: %s\n' "$version" >&2
    exit 1
    ;;
esac

temporary_dir=$(mktemp -d)
trap 'rm -rf "$temporary_dir"' EXIT HUP INT TERM

base_url="https://raw.githubusercontent.com/$repository/$tag"
curl -fsSL "$base_url/compose.yaml" -o "$temporary_dir/compose.yaml"
curl -fsSL "$base_url/.env.example" -o "$temporary_dir/.env"
sed "s/^KARPATHY_WIKI_VERSION=.*/KARPATHY_WIKI_VERSION=$version/" \
  "$temporary_dir/.env" >"$temporary_dir/.env.pinned"
mv "$temporary_dir/.env.pinned" "$temporary_dir/.env"

mkdir -p "$install_dir"
chmod 0644 "$temporary_dir/compose.yaml"
chmod 0600 "$temporary_dir/.env"
mv "$temporary_dir/compose.yaml" "$install_dir/compose.yaml"
mv "$temporary_dir/.env" "$install_dir/.env"

printf 'Installed Karpathy Wiki %s in %s\n' "$version" "$install_dir"
printf 'Next: edit %s/.env, then run docker compose up -d in that directory.\n' \
  "$install_dir"
