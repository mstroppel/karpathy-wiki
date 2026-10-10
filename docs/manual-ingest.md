# Trusted manual ingest from chat

This opt-in route implements the manual `/ingest-new` prerequisite of #157,
not automatic ingest. Default writable manual operation remains unchanged.
Other authoring remains unsupported in reader mode. Only an immediate browser
command is routed; queued commands, natural-language requests, direct backend
commands and agent tools do not grant admission.

## Enable

1. Back up the stopped stack, including wiki `.git`, the entire ingest-journal
   and `publisher/`. Do not run another writable backend or host writer.
2. Set `WIKI_RUNTIME_READ_ONLY=true`. Replace `publisher` in `COMPOSE_PROFILES`
   with `manual-ingest`, retaining source-provider profiles. Both writer services
   share the same symlink-safe lifetime lock and cannot run together.
3. Generate a separate token with
   `python3 -c 'import secrets; print(secrets.token_hex(32))'`. Set it as
   `WIKI_INGEST_CONTROL_TOKEN` in private `.env`; set
   `WIKI_INGEST_CONTROL_URL=http://manual-ingest:4080`. Never publish this port,
   mount controller state into OpenCode or forward the token to browsers/models.
4. Set `OPENCHAMBER_PUBLIC_URL` to the exact browser origin (scheme/host/port,
   no trailing slash/path). Recreate OpenCode, OpenChamber and `manual-ingest`.
   OpenCode independently refuses unsafe reader mounts.
5. Sign into chat, select the wiki project and an explicit available model, then
   invoke `/ingest-new` without arguments/attachments from an idle session.

The integrity-locked UI is patched at build time at its authenticated API slot,
before its backend proxy. A changed upstream slot fails the build. Admission
also checks the configured browser Origin against cross-origin cookie requests.
The controller token exists only in trusted UI/controller environments; backend
authentication is not publication authority. UI users and hosts remain trusted.
Reader isolation is not a general provider/configuration API sandbox.

## Execution and evidence

The controller checks Git/source state and snapshots all currently `new`/`outdated`
revisions; later arrivals require another invocation. Processing is sequential.
Each source gets a fresh linked child with deny-all tool permissions and the
parent's selected model. The controller uses pinned OpenCode's transient,
tool-free `/generate` endpoint. Worker JSON requests bounded reads, inspection
or private staging; it cannot choose roots, owners, runs, credentials, executable
operations or publication. No worker-facing writer endpoint, control inbox or
writable protected alias is installed in OpenCode.

Only the controller admits/publishes, using existing freshness/page/Git checks,
focused commits, receipts and verified complete private journal reports before
releasing ownership. Main chat receives verified records/report paths for compact
per-file summaries; full reports remain in the authenticated file viewer. Source
text and proposals are data, never executable instructions. Service logs contain
no document text, control tokens or raw provider errors. The controller holds a
private backend password, not model credentials; OpenCode uses existing accounts.

Private `publisher/manual.json` stores selection, active source, worker/run/draft
IDs, intent, verified records/reports and delivery flags. `manual-initialized.json`
prevents missing state from resetting stops. These files contain private data,
unlike content-free publisher `control.json`. Existing atomic storage primitives
use `0700` directories/`0600` files. Include them in stopped-stack backups.
No migrations, aliases, data moves, repairs or evidence pruning are added.

## Stops and explicit verification

Intent is durable before each runtime/writer call. Lost acknowledgements are not
retried. Restart during work persists `operator_action_required`, not redispatch;
there is no lease takeover. Each worker allows at most 256 generation requests
and each runtime call has a two-minute transport timeout. These are execution
safeguards, not context estimates or dollar caps. Timeout does not prove provider
execution or billing stopped.

A verified pristine draft failure receives a blocked record and private report;
drafts are retained without rollback. Uncertain writes/reporting/admission preserve
stops/ownership and explicitly flag missing reporting. Successful commits remain
intact; independent sources are not silently skipped. Check authenticated
`/api/wiki-ingest/status` on the chat origin, especially after a lost browser
response; do not blindly repeat the command.

Inspect and clarify failures first. Known interrupted publication uses the
[publisher's confirmed whole-container restart/recovery](trusted-publisher.md#coordination-and-interruption),
substituting `manual-ingest` for restart/exec. Then `/ingest-new resume` explicitly
requests verification, **not new ingestion**: no active/in-flight writer, queue or
publisher stop; pristine Git; valid sources; matching completed receipts, commits
and exact reports. Failed verification retains the stop. Run plain `/ingest-new`
afterwards only if another attempt is intended. Unknown preparation/staging still
requires maintenance; no general reset/unlock or live rollback exists. Never
delete state to resume. Other authoring, scheduling, retry/quarantine and quota
controls remain follow-up gates. Synthetic evidence is not real-provider quality
or subscription-exhaustion acceptance.

## Validation

```sh
node --test tests/test_manual_ingest.mjs
sh tests/integration/manual-ingest.sh
```

The disposable proof uses named volumes, an internal network, OpenCode 2.0.26
and a deterministic OpenAI-compatible fixture (no external model/cost): UI
auth/Origin refusal, separate admission, fresh worker proposals, verified commit,
journal/report and reader API/executed-shell denials.
