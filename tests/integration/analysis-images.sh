#!/bin/sh
# Exercise the installed renderer/dependencies without network or writable host data.
set -eu
# shellcheck disable=SC1007
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
image=${BACKEND_IMAGE:-kw-opencode:integration}
tar -C "$root" -cf - tests/test_render_analysis.py \
  config/skills/wiki-analysis-save/print-template.html | \
docker run -i --rm --network none --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m \
  --cap-drop ALL --security-opt no-new-privileges:true \
  -e PYTHONDONTWRITEBYTECODE=1 -e ANALYSIS_RENDERER_SCRIPT=/usr/local/bin/render-analysis \
  --entrypoint sh "$image" -c '
    mkdir /tmp/repo
    tar -xf - -C /tmp/repo
    cd /tmp/repo
    python3 -m unittest discover -s tests -p test_render_analysis.py -v
  '
