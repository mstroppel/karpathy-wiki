# Automatic ingest runtime gate

Automatic ingest is **not available or enabled**. [Issue #157](https://github.com/mstroppel/karpathy-wiki/issues/157)
holds its confirmed requirements; [PR #162](https://github.com/mstroppel/karpathy-wiki/pull/162)
is temporary planning, not an implementation or release approval.

## Reproduce the first blocker

Build the pinned backend, then run the model-free negative probe:

```sh
docker build --target opencode -t kw-opencode:integration -f opencode/Dockerfile .
sh tests/integration/auto-ingest-runtime.sh
```

The runner creates only one disposable, network-isolated container. It mounts no
host data, publishes no ports, uses generated disposable authentication and
makes no model requests. Cleanup removes only that container. The fixture plugin
is copied into its private global plugin directory; it is not shipped in the image.

After explicit location activation, the probe waits for a marker written **after**
all deny hooks were registered. A shell control request must execute its denial
hook without creating its target file. Unauthenticated requests must return 401.
Authenticated direct filesystem writes are then tested inside and outside the
requested location, checking actual file bytes rather than only HTTP responses.

The marker proves permission/tool hook registration completed, not that their
denials were exercised. Only the shell hook has an executed denial control.
The JSON `hook_evidence` makes this distinction explicit; this probe does not
validate permission/tool denial behavior during model-driven execution.

On OpenCode 2.0.25 and 2.0.26 these direct writes bypass the permission, tool and shell
guards. The JSON result therefore says `auto_ingest_admissible: false` and the
human-readable result says `release gate BLOCKED`. Normal exit zero means only
that this **negative probe reproduced the known blocker**, not that the runtime
passed a safety test. To enforce the blocked gate as a nonzero process result:

```sh
sh tests/integration/auto-ingest-runtime.sh --require-admissible
```

This currently exits 1. A different response after a runtime upgrade requires
reassessment; a failed request or absent plugin is not accepted as proof of safety.

## Consequence

Do not enable automatic ingest using only session permissions, tool hooks or
the existing publication lock. The latter protects publication, not arbitrary
authenticated backend mutations. The read-only UI mount does not limit backend
filesystem APIs. This is an authenticated coordination gap, not evidence of
unauthenticated access.

Before proceeding, enforce and test a non-bypassable mutation boundary covering
direct filesystem writes, session/location shells, PTYs, transaction/journal
tools, configuration changes and already-running operations. A proxy alone is
not sufficient if another supported path can reach the writable backend directly.
External host writers remain outside the runtime guarantee.

Writer ownership/fencing, idempotent command admission, unattended boot readiness,
provider-limit classification and real-model execution remain separate gates.
The probe does not claim to test them. No migration, data-layout change, new
privilege or automatic writer is introduced.
