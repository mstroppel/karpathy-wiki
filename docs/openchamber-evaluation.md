# OpenChamber compatibility evaluation

Tracking issue: [#137](https://github.com/mstroppel/karpathy-wiki/issues/137).
Evaluation date: 2026-10-02. This is a disposable compatibility spike, not a
supported deployment profile or a replacement for the existing OpenCode UI.

## Result

OpenChamber Web 2.1.0 can attach to the repository's OpenCode 2.0.22 service
using `OPENCODE_HOST=http://opencode:4096`, `OPENCODE_SKIP_START=true`, and the
existing server password. No second OpenCode service is needed. Keep
SilverBullet as the read-only wiki reader.

The container transport smoke test passed on Linux amd64:

- Both the OpenChamber API and the OpenCode API reject unauthenticated requests.
- OpenChamber discovers `/ingest-new`, `/analysis`, `/analysis-save`, and
  `/gap-review`, their configured wiki agents, and wiki skills. Its skill
  settings panel also receives the skills from the external service.
- A session can be created at `/knowledge/wiki`, switched to `wiki-analysis`,
  and recovered with its title, location, and agent after restarting both
  containers.
- The proxied SSE event stream delivers the native connection marker and a
  `session.created` event containing the test's newly created session ID,
  before and after restart. Proxy heartbeat comments do not count as events.
- The OpenChamber process cannot write the wiki mount; OpenCode can write the
  wiki but cannot write the source mount. OpenChamber starts no second
  `opencode serve` process.

These checks do **not** execute the four commands, make model calls, verify
question/permission UI interactions, test WebSockets or a reverse proxy, or
prove source-to-wiki publication. They also do not validate ARM64.

## Deployment direction and authority

Use a separate opt-in OpenChamber service connected to the existing OpenCode
service. The smallest tested filesystem access is:

| Service | Access |
| --- | --- |
| OpenChamber | Wiki checkout at the same `/knowledge/wiki` path, read-only; its own writable home/settings |
| OpenCode | Existing writable wiki, read-only sanitized sources, private answer inbox, and its own persistent state |

Do not copy the upstream Compose example's shared OpenCode credential/state
mounts or SSH mounts. OpenChamber does not need raw incoming sources, speech
results/model caches, provider secrets, the Docker socket, or broad host mounts.
Its server needs the OpenCode API password and a **separate** UI password.

A read-only wiki mount only constrains local OpenChamber processes. An
authenticated OpenChamber user and its server still have the existing OpenCode
API's authority, including model execution and writable-wiki operations. This
is a trusted administration interface, not a reader or a new isolation boundary.
The spike does not solve #108 or #113. Do not claim that missing direct mounts
prevent API-mediated access to data or credentials.

OpenChamber's file, terminal, Git, and configuration features operate partly on
its own filesystem. A read-only wiki mount intentionally prevents local edits,
commits, and worktree creation there. Configuration editing and skill-file
previews are not validated by the successful skill-list check; they must not
be enabled by sharing writable OpenCode configuration as a shortcut.

The upstream agent-control tool is unavailable in external-server/skip-start
mode. Session browsing, agent selection, and command discovery are promising
for wiki use. Multi-run, worktree sessions, Session Goals, and scheduled writers
must not be used to create concurrent writers against the shared wiki checkout.
Review whether the deployment can disable or clearly guard those features.
Private Relay, public tunnels, and automatic updates are outside this spike;
relay hosting was explicitly disabled with `OPENCHAMBER_RELAY_HOST=off`.

## Repeat the transport smoke test

Requires Docker with volume-subpath support (Engine 26+) and locally built
images. The OpenChamber test image must expose `openchamber`, Node.js 22+, Git,
and a compatible OpenCode CLI on `PATH`. Even external-server mode currently
requires the local CLI to be present. Do not point this test at a live stack.

The exploratory image used Node's `22-bookworm-slim` base (resolved digest
`sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c`),
installed Git, Python, make, g++, and CA certificates, then installed
`@openchamber/web@2.1.0` and `@opencode/cli@2.0.22` with npm. This dependency
resolution is **not** a release lockfile; a production image still needs a
reviewed, locked dependency tree, verified artifacts, update coverage, and
architecture validation. The exploratory image is not published by this repo.

To build the same kind of exploratory image, save this Dockerfile outside the
checkout and build it as `kw-openchamber:137-spike`:

```dockerfile
FROM node:22-bookworm-slim@sha256:43ac6c60b8f89723f746e8a92ce91abd5017e627ce1ddfe4238355d3a30b772c
RUN apt-get update && apt-get install -y --no-install-recommends git python3 make g++ ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN npm install --global @openchamber/web@2.1.0 @opencode/cli@2.0.22
USER node
ENV HOME=/home/node NODE_ENV=production OPENCHAMBER_RELAY_HOST=off
```

Build the backend from this checkout, then run:

```sh
docker build -t kw-opencode:137-spike -f opencode/Dockerfile .
OPENCHAMBER_SMOKE_IMAGE=kw-openchamber:137-spike \
  OPENCODE_SMOKE_IMAGE=kw-opencode:137-spike \
  sh tests/integration/openchamber-smoke.sh
```

The runner creates unique named volumes and an internal network, generates
throwaway passwords, publishes no host ports, and removes its containers,
volumes, and network on exit. It mounts no host data and has no model/provider
credentials or runtime internet access. Built images remain available for reuse.
The test is opt-in, separate from the normal Compose integration suite.

## Remaining acceptance checks before a supported profile

- [ ] Build a release-managed, pinned OpenChamber image and validate its target
  architectures; do not enable in-app dependency updates in an immutable image.
- [ ] Add an opt-in service with the tested minimum mounts, private service
  connectivity, a required UI password, health checks, and no published host
  ports. Retain the original UI and SilverBullet.
- [ ] Test the browser at desktop/mobile sizes: project selection, existing
  sessions, commands, agents, skill use, streaming, interruption, and reconnect.
- [ ] Exercise all four wiki commands against synthetic sources with an
  explicitly authorized model configuration. For `/gap-review`, answer/defer
  across turns and verify that no draft is submitted before confirmation.
  For `/analysis-save`, verify complete context, provenance, and print links.
- [ ] Verify question and permission prompts, reject/accept behavior, and
  server failure/recovery without starting a replacement OpenCode service.
- [ ] Test an authenticated reverse proxy with WebSocket upgrades, unbuffered
  SSE, suitable timeouts, attachment limits, and forwarded headers over HTTPS.
- [ ] Decide how unsupported local editing/configuration features and unsafe
  concurrent/scheduled writer features are guarded and explained to users.
- [ ] Document setup, separate persistent settings backup, removal, and manual
  data cleanup. No migrations or legacy-path fallbacks before 1.0.

## Upstream references

Source inspection used release
[`v2.1.0`](https://github.com/openchamber/openchamber/tree/v2.1.0)
(commit `90726f994`), not the moving main branch:

- [External server configuration](https://github.com/openchamber/openchamber/blob/v2.1.0/packages/docs/content/docs/opencode-server.mdx)
- [Authentication implementation](https://github.com/openchamber/openchamber/blob/v2.1.0/packages/web/server/lib/opencode/auth-state-runtime.js)
- [External-mode agent-control limitation](https://github.com/openchamber/openchamber/blob/v2.1.0/packages/docs/content/docs/agent-control-tool.mdx)
- [Reverse proxy requirements](https://github.com/openchamber/openchamber/blob/v2.1.0/docs/REVERSE_PROXY.md)
- [OpenCode V2 API](https://opencode.ai/v2/docs/api)
