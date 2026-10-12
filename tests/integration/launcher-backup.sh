#!/bin/sh
# Real Docker lifecycle test, only disposable synthetic data and images.
set -eu
cd "$(dirname -- "$0")/../.."
python3 tests/integration/launcher_backup_probe.py
