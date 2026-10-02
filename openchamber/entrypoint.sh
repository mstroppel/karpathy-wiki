#!/bin/sh
set -eu

: "${OPENCODE_PASSWORD:?OPENCODE_PASSWORD is required}"
: "${OPENCHAMBER_UI_PASSWORD:?OPENCHAMBER_UI_PASSWORD is required}"
: "${OPENCHAMBER_DATA_DIR:?OPENCHAMBER_DATA_DIR is required}"

node /etc/openchamber/bootstrap.mjs
# Direct foreground server: no CLI daemon, in-app restart manager, or SSH setup.
exec node /opt/chat/node_modules/@openchamber/web/server/index.js \
  --host 0.0.0.0 --port 3000 "$@"
