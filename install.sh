#!/bin/sh
set -eu

repository="mstroppel/karpathy-wiki"
install_dir=${INSTALL_DIR:-.}
version=${KARPATHY_WIKI_VERSION:-}

if ! command -v curl >/dev/null 2>&1; then
  printf 'curl is required\n' >&2
  exit 1
fi

if [ ! -d "$install_dir" ]; then
  printf 'installation directory does not exist: %s\n' "$install_dir" >&2
  exit 1
fi

install_dir=$(CDPATH= cd -- "$install_dir" && pwd)
wiki_id=$(basename "$install_dir")
case "$wiki_id" in
  ''|*[!a-z0-9-]*|[-]*)
    printf 'installation directory name must start with a lowercase letter or digit and contain only lowercase letters, digits, or hyphens: %s\n' \
      "$wiki_id" >&2
    exit 1
    ;;
esac

for entry in "$install_dir"/* "$install_dir"/.[!.]* "$install_dir"/..?*; do
  if [ -e "$entry" ] || [ -L "$entry" ]; then
    printf '%s must be empty before installation\n' "$install_dir" >&2
    exit 1
  fi
done

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
main_url="https://raw.githubusercontent.com/$repository/main"
# Releases before the launcher was introduced do not contain karpathy-wiki.sh;
# fall back to main, which stays compatible with any pinned version.
# Older releases do not contain the launcher; suppress that expected miss while
# keeping errors from the fallback download visible.
curl -fsSL "$base_url/karpathy-wiki.sh" -o "$temporary_dir/karpathy-wiki.sh" 2>/dev/null ||
  curl -fsSL "$main_url/karpathy-wiki.sh" -o "$temporary_dir/karpathy-wiki.sh"
curl -fsSL "$base_url/.env.example" -o "$temporary_dir/.env"
sed \
  -e "s/^COMPOSE_PROJECT_NAME=.*/COMPOSE_PROJECT_NAME=$wiki_id/" \
  -e "s/^STACK_ID=.*/STACK_ID=$wiki_id/" \
  -e "s/^KARPATHY_WIKI_VERSION=.*/KARPATHY_WIKI_VERSION=$version/" \
  "$temporary_dir/.env" >"$temporary_dir/.env.pinned"
mv "$temporary_dir/.env.pinned" "$temporary_dir/.env"

chmod 0755 "$temporary_dir/karpathy-wiki.sh"
chmod 0600 "$temporary_dir/.env"
mv "$temporary_dir/karpathy-wiki.sh" "$install_dir/karpathy-wiki.sh"
mv "$temporary_dir/.env" "$install_dir/.env"

printf 'Installed Karpathy Wiki %s in %s\n' "$version" "$install_dir"
printf 'Configured COMPOSE_PROJECT_NAME and STACK_ID as %s\n' "$wiki_id"
printf 'Next: edit %s/.env, then run %s/karpathy-wiki.sh up -d.\n' \
  "$install_dir" "$install_dir"
printf 'The launcher downloads and caches the Compose file for the pinned\n'
printf 'version on demand; update with %s/karpathy-wiki.sh update.\n' \
  "$install_dir"
