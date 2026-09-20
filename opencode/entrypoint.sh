#!/bin/sh
set -eu

mkdir -p "$HOME/.config/opencode/plugins"
for plugin in /etc/opencode/plugins/*; do
  ln -sf "$plugin" "$HOME/.config/opencode/plugins/${plugin##*/}"
done

exec opencode "$@"
