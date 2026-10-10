# Trusted publisher: operator-only foundation

The opt-in `publisher` profile implements a prerequisite of [#157](https://github.com/mstroppel/karpathy-wiki/issues/157),
not automatic ingestion or a model-facing service. It reuses the source checks,
private transactions, focused commits, verified journal and reports in a separate writer.

## Authority and deployment

The publisher has **no network**, listener, OpenCode runtime, chat, model
credentials, Docker socket or shared control inbox. Its only interface is a
narrow JSON CLI invoked by a trusted host operator through container exec.
OpenCode retains read-only mounts and cannot invoke this channel, even knowing
job/preparation IDs. IDs are locators, not credentials. Never mount publisher state
into OpenCode or expose this CLI through a model-callable shell, plugin, API or tool.
The future trusted controller must own admission/publication; workers supply only proposal data.

Set `WIKI_RUNTIME_READ_ONLY=true`, add `publisher` to existing `COMPOSE_PROFILES`, then:

```sh
./karpathy-wiki.sh up -d --force-recreate opencode publisher
```

Startup requires reader mode and non-root execution. Compose gives the publisher
a read-only root, no capabilities, no-new-privileges, private temporary storage,
read-only sources, writable wiki/journal and private `publisher/` state. It has
no runtime configuration/credential mounts. The reader independently checks its
kernel boundary. Host/admin writers and source providers remain outside coordination;
do not run `init` concurrently.

**This operator-only profile does not route chat authoring.** An alternative
[manual-ingest profile](manual-ingest.md) routes immediate browser `/ingest-new`;
`/analysis-save`, answer saves and other authoring remain unsupported in reader
mode. No scheduler is enabled. Default writable manual mode stays unchanged.
Stop the publisher before returning OpenCode to writable mode.

## Private control protocol

Keep requests/results private on the host. One strict UTF-8 JSON object (maximum
64 KiB) is consumed from stdin. Use this fixed command, never source text as shell arguments:

```sh
./karpathy-wiki.sh exec -T publisher node /opt/karpathy-wiki/publisher/cli.mjs < request.json
```

Output is `{"result": ...}` or a content-free refusal (exit 1). Inspect `status`
on refusal. Results/identities/text are private output, not service logs. No arbitrary
files, shell commands, caller-selected roots/runs or worker-asserted ownership.

1. **Enqueue** an explicitly selected published revision:

   ```json
   {"operation":"enqueue","request_id":"req-source-1","kind":"manual","source":{"adapter":"webdav","source_key":"notes.md","source_revision":"<64 lowercase SHA-256 hex characters>"}}
   ```

   `kind` is `manual` or `automatic`, asserted **only by the trusted operator**;
   `automatic` currently tests coordination and makes no model request. Job IDs
   are publisher-generated. Request IDs start with `req-` plus 1–96 letters,
   digits, hyphens or underscores. Exact replay returns the original job; changed
   content with the same ID is refused.
2. **Activate**: `{"operation":"activate"}` selects manual jobs first, then
   enqueue time with a stable job-ID tie-breaker, checks source/pristine wiki and
   prepares a private draft. Unresolved prior journal runs block admission rather
   than being adopted. It returns active job/preparation/run IDs or `{"idle":true}`.
   Repeated activation returns the current owner, never a competing writer.
3. **Propose** via the trusted bridge. Only `proposal` is untrusted worker data,
   not the surrounding operator request:

   ```json
   {"operation":"propose","request_id":"req-read-1","job_id":"<returned job ID>","proposal":{"operation":"read_source","offset":1,"limit":30}}
   ```

   Operations reuse the [transaction protocol](ingest-reports.md): `read_source`
   (paginate fully), `inspect`, `stage`, `declare`, `state`. Proposal fields:
   `operation`, `page`, `draft`, `append`, `reference`, `replacement`, `reviewed`,
   `offset`, `limit`, `query`, `changed_pages`. Stage the source draft and
   review/update `overview.md`; code alone generates index/log. Staging leaves the
   live wiki untouched. Proposal `prepare`, `publish`, `resume`, `rollback`, ownership
   and executable operations are refused.
4. **Publish** only on trusted operator authority:

   ```json
   {"operation":"publish","request_id":"req-publish-1","job_id":"<returned job ID>","report":{"title":"Selected source","content":"Findings with evidence locations.","contradictions":"None identified.","extraction_limits":"Fully read; stated limitations."}}
   ```

   The core checks complete reads, freshness, changed-page contents, focused commit
   and verified durable success, and finishes the report before releasing ownership.
   Exact proposal/publication replay returns stored results; stale owners/altered
   replays are refused. Source text is never executed. Reports cannot assert commits
   or journal success.

## Coordination and interruption

`{"operation":"status"}` reports ownership, in-flight intent, safety stop and queue.
Admission has a separate kernel lock and remains available during publication.
Mutations are exclusive across CLI processes. Ownership persists between operations
and restarts with **no lease expiry or timeout takeover**. Manual jobs get priority
at the next **completed source** boundary; no mid-publication force-abort.
A container-lifetime kernel lock refuses a second live publisher on the same
state, so a competing container cannot impersonate a restart for recovery.

`{"operation":"cancel","job_id":"<ID>","confirmed":true}` cancels only a
private draft with no pending stage and a pristine live wiki. It closes the run
with an unfinished report, retains evidence and performs no live rollback/cleanup.
Unknown preparation/staging/cancellation outcomes, dirty/conflicting state,
missing/invalid ownership and uncertain publication require maintenance. There
is no general reset/unlock; never delete control state to resume.

For an interrupted **publication**:

1. Inspect private status/receipts. Stop other writers and clarify/repair foreign
   changes manually. This channel performs no automatic repair/rollback.
2. Restart the **whole container**, not just a CLI, to terminate any remaining
   Git/process descendants: `./karpathy-wiki.sh restart publisher`.
3. Request `{"operation":"recover","confirmed":true}`. This requires a different
   boot identity and persisted `sealing`, `sealed`, `installing` or `done` phase.
   Verified recovery reconciles commit/index/journal/report without a second commit.
   Unverifiable state stays blocked; interrupted prepare is never blindly redispatched.

Back up **all** `publisher/`, wiki `.git` and the entire private ingest-journal
together with writers stopped. Control state stores content-free queue/owner/intents
and replay digests; source-bearing replay payloads live separately under private
`incoming/ingest-journal/publisher-replays/`. Status is an explicit content-free
projection without replay payloads. Evidence uses `0700` directories and `0600`
files. No automatic retention cleanup or migrations.

## Validation and remaining gates

```sh
node --test tests/test_trusted_publisher.mjs
docker build --target publisher -t kw-publisher:integration -f opencode/Dockerfile .
sh tests/integration/trusted-publisher.sh
```

The disposable proof runs the shipped CLI beside pinned OpenCode 2.0.26 on the
same backing trees: private staging, executed reader API/shell denials, hard crash
at persisted install intent, container restart, confirmed verified recovery,
one commit/journal record/report, exact replay and manual handover. No host data,
exposed ports, external network or models. CI also retains the broader reader proof.

Full #157 still requires trusted-controller/worker dispatch, every manual workflow
route, session-descendant ownership, boot-without-chat activation, finite scheduling,
retries/quarantine, provider/quota pauses, controls and synthetic real-model evidence.
None is claimed complete by this operator-only foundation.
