#!/bin/sh
set -eu

mkdir -p "$HOME/.config/opencode/tools"
for tool in /etc/opencode/tools/*; do
  ln -sf "$tool" "$HOME/.config/opencode/tools/${tool##*/}"
done

exec opencode "$@"
