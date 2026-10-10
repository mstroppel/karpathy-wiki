#!/bin/sh
set -eu
umask 077
: "${OPENCODE_PASSWORD:?OPENCODE_PASSWORD is required}"
# Shares the lifetime lock with the operator-only publisher. Neither can claim
# the same backing state as a new boot while the other is still alive.
exec python3 /opt/karpathy-wiki/publisher/service.py --manual
