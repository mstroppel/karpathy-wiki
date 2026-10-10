#!/bin/sh
# GitHub-hosted runners share Docker Hub's anonymous pull quota. Configure
# daemon pulls (Compose and docker build) separately from container BuildKit.
set -eu

config=/etc/docker/daemon.json
if sudo test -f "$config"; then
  existing=$(sudo cat "$config")
else
  existing='{}'
fi
printf '%s\n' "$existing" | jq '
  .["registry-mirrors"] = (["https://mirror.gcr.io"] +
    ((.["registry-mirrors"] // []) | map(select(. != "https://mirror.gcr.io"))))
' | sudo tee "$config" >/dev/null
sudo systemctl restart docker
docker info
