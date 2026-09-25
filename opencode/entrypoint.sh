#!/bin/sh
set -eu

mkdir -p "$HOME/.config/opencode/plugins"
if [ ! -e "$HOME/.config/opencode/AGENTS.md" ] && [ ! -L "$HOME/.config/opencode/AGENTS.md" ]; then
  ln -s /etc/opencode/routing.md "$HOME/.config/opencode/AGENTS.md"
fi
for plugin in /etc/opencode/plugins/*; do
  ln -sf "$plugin" "$HOME/.config/opencode/plugins/${plugin##*/}"
done

exec opencode "$@"
