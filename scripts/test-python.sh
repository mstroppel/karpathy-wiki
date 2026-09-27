#!/bin/sh
# Run both Python suites under one branch-coverage report.
set -eu

# shellcheck disable=SC1007 # Clear CDPATH only for locating the checkout.
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
export COVERAGE_FILE="$root/.coverage"
python3 -m coverage erase
sources=$root/ingest/core/src,$root/ingest/webdav/src,$root/ingest/paperless/src,$root/opencode
python3 -m coverage run --branch --source="$sources" \
  -m unittest discover -s tests -v
(cd ingest && python3 -m coverage run --append --branch --source="$sources" \
  -m unittest discover -s tests -v)
python3 -m coverage report
python3 -m coverage xml -o coverage.xml
