# Chat deployment

OpenChamber is the default chat interface. OpenCode is the private execution
backend; SilverBullet remains the read-only wiki browser. Both chat containers
use the same verified OpenCode CLI version. OpenChamber's npm dependency tree is
integrity-locked, installed without lifecycle downloads, and tracked by Dependabot.

Two transitive dependencies (`simple-git` and `@simple-git/argv-parser`) have
temporarily ignored Dependabot updates pending an upstream-compatible security
fix. [Issue #168](https://github.com/mstroppel/karpathy-wiki/issues/168) records
the accepted risk and removal criteria. Their security alerts remain open;
this exception does not fix the vulnerabilities. Keep UI users trusted and
Git authoring workflows unsupported: read-only mounts are not a Git API sandbox.
Review the exception on the next chat-runtime update, or by 2026-11-06.

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

The UI wiki mount is read-only. Ingest run records and reports are also mounted
read-only at `/knowledge/incoming/ingest-journal/runs`, using the same absolute
paths as OpenCode. Local report links open through OpenChamber's authenticated
file viewer; they are outside the wiki workspace and use outside-workspace reads.
Private transaction preparations and answer/audio inboxes are not mounted.
Local editor, Git write operations, worktrees,
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
session agent switching, correlated native SSE events, authenticated exact-path
report reads (including missing-file errors), read-only mounts and
restart persistence. Real-provider generation, browser question/permission
dialogs and deployment-specific HTTPS/WebSocket behavior require operator
acceptance testing; they are not claimed by the transport test.

### Operator acceptance checklist

Use a disposable installation, synthetic documents and an explicitly approved
model/provider budget. Do not use private sources for these checks.

- [ ] Open the HTTPS chat URL: unsigned API requests and an incorrect password
  must fail; UI sign-in succeeds without exposing the backend password.
- [ ] Select the wiki project and model. Confirm all four wiki commands and
  wiki agents/skills are visible; switch agents and verify the next turn uses
  the selected agent.
- [ ] Run `/ingest-new` on a synthetic published source: verify streamed output,
  a wiki commit, and the page in SilverBullet. Click the final report link and
  verify the viewer shows that run's report, not a previously opened wiki page;
  repeat with a second run. Run `/analysis`, then
  `/analysis-save` in the same conversation: verify the saved page and HTML view.
- [ ] With `answers` enabled, run `/gap-review`: answer and defer questions
  across turns. No answer source may be published before explicit confirmation;
  after confirmation verify local redaction and normal ingest.
- [ ] Trigger a harmless permission request with a disposable test agent:
  deny it and verify no operation occurs; approve another and verify it runs
  once. Trigger a native question and verify the response resumes the same turn.
- [ ] During a streamed reply, disconnect/reconnect the browser and restart
  the backend. Verify session history survives and subsequent native events
  arrive. Check the proxy preserves cookies, does not buffer SSE, and permits
  WebSocket upgrades for any enabled supported feature.
- [ ] Restart the UI: selected project/settings and backend sessions survive.
  Backend failure must make the UI unhealthy; recovery must restore readiness.

Record the release, architecture, model, outcomes and any failures before using
the deployment for real wiki work. Stop on failed confirmation or permission
boundaries; do not work around them with concurrent writers or local editing.
