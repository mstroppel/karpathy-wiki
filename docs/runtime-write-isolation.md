# Runtime write isolation

`WIKI_RUNTIME_READ_ONLY=true` opts into a **reader-only OpenCode runtime**.
It is a kernel-enforced boundary for [automatic-ingest work (#157)](https://github.com/mstroppel/karpathy-wiki/issues/157),
not writer serialization, a publisher or a completed auto-ingest feature.

## Enable

Set this in the installation's `.env`, then recreate OpenCode:

```env
WIKI_RUNTIME_READ_ONLY=true
```

```sh
./karpathy-wiki.sh up -d --force-recreate opencode
```

Compose mounts wiki, sources, the **entire** private ingest-journal and confirmed
answer inbox read-only. The container root is also read-only. Runtime credentials,
sessions, settings and state retain their separate writable mounts. Temporary
storage and the runtime cache are ephemeral. `/tmp` allows execution because the
pinned Bun runtime extracts its PTY native library there; this does not grant
write access to protected mounts or extra capabilities.

The shipped entrypoint checks the actual kernel mount table and filesystem flags
before launching OpenCode. It refuses root execution, nonzero capability sets,
missing `no-new-privileges`, a writable root/protected mount, writable overlapping
submounts, writable aliases of protected backing paths or an exposed default
Docker socket. Unsafe topology produces only a content-free startup error.
No model instructions, permission hook, API path filter or backend password is
the enforcement mechanism: all processes start without protected write access.

Default `false` preserves manual authoring. Return to that mode by setting
`false` and recreating the container. A restart alone does not apply changed
Compose mounts. This changes no stored data layout and performs no migration.

## Supported behavior and boundary

In reader mode, model questions, file reads and private session state remain
available, but `/ingest-new`, `/analysis-save`, journal/report writes and saving
confirmed `/gap-review` answers cannot work. Do not enable automated ingest here
yet: an independently isolated trusted publisher must be implemented before
authoring can resume without granting the model backend write authority.
The existing publisher core reports a generic lock failure when its kernel lock
write is denied; that is not evidence of another active writer.

Authenticated API/configuration access is **not** sandboxed in general. Runtime
secrets and sessions remain within the backend's existing authority. The guarantee
is no mutation of the protected backing trees by this unprivileged runtime,
including direct APIs, plugins, spawned shells and PTYs. It is not protection
against kernel/container-runtime exploits or an administrator who changes the
deployment, grants a remote writer credential, exposes a privileged socket or
pre-seeds writable hardlink aliases outside the protected trees. Keep host data
roots disjoint and do not expose Docker/host-writer authority to OpenCode.
Initialization, source providers and future publishers are separate trusted
writers outside this runtime boundary; do not mistake reader mode for their
coordination or for immutable source publication over time.

## Pinned-image proof

```sh
docker build --target opencode -t kw-opencode:integration -f opencode/Dockerfile .
sh tests/integration/runtime-write-isolation.sh
```

The runner uses disposable named volumes, no host data, no exposed ports, no
external network and no model calls. It checks both rendered Compose modes,
confirms a direct API write succeeds in manual mode, and then runs the isolated
mode with the same stock OpenCode 2.0.25 API. It verifies:

- Direct writes from wiki and unrelated locations cannot overwrite/create files.
- Actual location/session shells and a PTY execute overwrite/create/unlink/rename,
  chmod, mkdir and symlink attempts: the kernel returns `EROFS`.
- Symlink and `/proc/self/root` paths, hardlink escape and remount cannot bypass it.
- The actual journal core cannot start a run; publication's lock syscall returns
  `EROFS` and its core refuses admission. A synthetic custom command invokes these
  cores without asking a model; this is not model-facing tool-dispatch validation.
- Runtime/configuration writes outside the protected trees remain possible but
  do not restore protected write access. Entire protected tree contents, entries,
  permissions and ownership are unchanged, including Git and journal evidence.
- The same checks pass after restart, and startup rejects a writable backing
  alias or a read-only flag paired with writable mounts.

CI runs this positive boundary proof. The earlier negative probe in #188 concerns
the manual writable topology; its plugin-only guard limitation is not fixed
upstream. Passing this reader-boundary test does **not** prove publisher admission,
writer handover, idempotent dispatch, boot activation, quota handling or ingestion
semantics. Those remain gates for the full controller.
