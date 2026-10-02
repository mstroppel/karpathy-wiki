# Chat deployment

OpenChamber is the default chat interface. OpenCode is the private execution
backend; SilverBullet remains the read-only wiki browser. Both chat containers
use the same verified OpenCode CLI version. OpenChamber's npm dependency tree is
integrity-locked, installed without lifecycle downloads, and tracked by Dependabot.

## Setup

- Set separate strong `OPENCHAMBER_UI_PASSWORD` and `OPENCODE_PASSWORD` values.
  The installer generates both. Never forward the backend password to browsers.
- Point the HTTPS chat reverse proxy at `${STACK_ID}-openchamber:3000` on
  `WEBPROXY_NETWORK`. Do not expose OpenCode or publish host ports.
- Preserve cookies and WebSocket upgrades, disable response buffering, and allow
  long-lived SSE connections. Serve at the hostname root, not a URL subpath.
- Sign in with the UI password, connect a provider in Settings, and select a
  model. Provider credentials and sessions belong to the backend, not the UI mount.
- Initialization selects `/knowledge/wiki` as the project without overwriting
  existing UI settings. `${DATA_ROOT}/openchamber` stores private UI settings.

Example Caddy configuration (Caddy must join the proxy network):

```caddyfile
chat.example.com {
    reverse_proxy karpathy-wiki-openchamber:3000 {
        flush_interval -1
    }
}
```

OpenChamber authenticates users itself; additional proxy access control is
recommended for untrusted networks. Do not replace upstream Authorization with
the OpenCode credential. OpenCode is attached only to the private stack network.

## Wiki workflows

Use `/ingest-new`, `/analysis`, `/analysis-save`, and `/gap-review` in chat;
the existing agents, skills, tools, permissions and question handling run in
OpenCode. The `answers` profile is still required to publish confirmed Q&A.
Use one active wiki-writing conversation at a time. Do not enable schedules or
concurrent writer sessions: this stack does not add writer serialization.

The UI wiki mount is read-only. Local editor, Git write operations, worktrees,
terminal-based editing, local provider configuration files, and file uploads
are not supported as wiki-authoring workflows. Use chat commands instead.
External-server mode does not install OpenChamber's agent-control tools into
the backend. OpenChamber is not an API sandbox: authenticated users retain the
backend's API authority, including provider and configuration operations.

No sources, raw audio, provider-secret files, OpenCode state, SSH keys or Docker
socket are mounted into the frontend. The frontend root filesystem is read-only,
with its own settings mount and temporary storage. Treat all UI users as trusted
operators; these mounts do not solve model credential or publication isolation.

## Operations

Compose waits for authenticated backend readiness before starting the UI. Both
services restart on failure; the UI health check includes backend readiness.
After a backend restart the UI reconnects; reload the page if a client stream
stays disconnected. Back up the whole data root, including `openchamber/` and
`opencode/`, with the stack stopped.

For an existing installation, manually set `OPENCHAMBER_UI_PASSWORD`, update the
chat proxy to the new alias/port, and run the stack's `init` service to create the
private UI directory before starting the new services. Remove the old public
OpenCode proxy route. No automatic data moves, aliases, or migration code exist.

`tests/integration/chat.sh` exercises the shipped images with disposable volumes
and no model calls: separate authentication, all wiki commands/agents/skills,
session agent switching, correlated native SSE events, read-only mounts and
restart persistence. Real-provider generation, browser question/permission
dialogs and deployment-specific HTTPS/WebSocket behavior require operator
acceptance testing; they are not claimed by the transport test.
