#!/bin/sh
# Shared lint gate. CI runs this script; keep local checks identical by
# running it from a checkout. Python tooling versions are pinned in
# requirements-dev.txt; JavaScript tooling is pinned by package-lock.json.
set -eu

cd "$(dirname -- "$0")/.."

fail_missing() {
  printf 'lint: %s is missing; install it first (see README.md, Development)\n' "$1" >&2
  exit 1
}

command -v python3 >/dev/null 2>&1 || fail_missing python3
command -v node >/dev/null 2>&1 || fail_missing node
command -v shellcheck >/dev/null 2>&1 || fail_missing shellcheck
python3 -m ruff --version >/dev/null 2>&1 || fail_missing "ruff (requirements-dev.txt)"
python3 -m mypy --version >/dev/null 2>&1 || fail_missing "mypy (requirements-dev.txt)"

# Python: formatting, linting, and types (configuration in pyproject.toml).
python3 -m ruff format --check .
python3 -m ruff check .
python3 -m mypy

# JavaScript: locked dependency metadata plus linting and formatting.
npm ci --silent
npm run lint
npm run format:check

# Shell scripts: syntax and ShellCheck (configuration in .shellcheckrc).
# The gate covers every shell script in the checkout, including this script
# and the Compose integration runner.
find . \
  -path ./.git -prune -o \
  -path ./node_modules -prune -o \
  -path ./data -prune -o \
  -path ./secrets -prune -o \
  -path ./temp -prune -o \
  -type f -name '*.sh' -print | sort | while IFS= read -r script; do
  sh -n "$script"
  shellcheck "$script"
done

# Shipped JavaScript and the full Compose configuration.
node --check config/plugins/wiki-ingest-status.js
node --check config/tools/wiki_ingest_status_core.mjs
node --check config/ingest-adapters/shared.mjs
for adapter in config/ingest-adapters/*/status.mjs; do
  node --check "$adapter"
done
docker compose --env-file .env.example config --quiet

printf 'lint: OK\n'
