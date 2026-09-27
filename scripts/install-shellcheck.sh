#!/bin/sh
# Install the same verified ShellCheck release used by CI into a user bin dir.
set -eu

version=0.11.0
digest=8c3be12b05d5c177a04c29e3c78ce89ac86f1595681cab149b65b97c4e227198
destination=${1:-"$HOME/.local/bin"}
archive=$(mktemp -d)
trap 'rm -rf "$archive"' EXIT

case "$(uname -m)" in
  x86_64) platform=linux.x86_64 ;;
  *) printf 'Unsupported ShellCheck platform: %s\n' "$(uname -m)" >&2; exit 1 ;;
esac

curl -fsSL --retry 3 \
  "https://github.com/koalaman/shellcheck/releases/download/v$version/shellcheck-v$version.$platform.tar.xz" \
  -o "$archive/shellcheck.tar.xz"
printf '%s  %s\n' "$digest" "$archive/shellcheck.tar.xz" | sha256sum -c -
tar -xJf "$archive/shellcheck.tar.xz" -C "$archive"
mkdir -p "$destination"
install -m 0755 "$archive/shellcheck-v$version/shellcheck" "$destination/shellcheck"
