# Trusted manual ingest from chat

This opt-in route implements the manual `/ingest-new` prerequisite of #157,
not automatic ingest. Default writable manual operation remains unchanged.
Only immediate browser commands are routed; queued commands, natural-language
requests, direct backend commands and agent tools do not grant admission.
Beyond ingest, the same channel admits the authoring saves that reader mode
denies to the model runtime: `/analysis-save` and `/answer-save` (below).

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

Private `publisher/manual.json` stores selection, its durable admission/run/draft
references and completed-prefix count, active source, worker IDs, intent and
delivery flags. It stays content-free like publisher `control.json`: per-file
records and reports are rebuilt from the private ingest journal for summaries
and never copied into controller state, status output, or backups of it.
`manual-initialized.json`
prevents missing state from resetting stops. Invalid or conflicting durable
evidence fails closed as `invalid_controller_state` instead of being trusted,
repaired or overwritten. These files hold private operational data; existing
atomic storage primitives use `0700` directories/`0600` files. Include them in
stopped-stack backups.
No migrations, aliases, data moves, repairs or evidence pruning are added.

### Controller execution fence

The trusted ingest and authoring routes share a kernel execution lock in addition
to their short admission lock and the container lifetime lock. It spans all
awaited worker generation, publication, failure reporting and summary delivery;
verified recovery uses the same lock. A second process cannot initialize a new
controller boot while that work is in flight. Lock order is admission, controller
execution, then publisher/wiki locks; background execution never takes admission
while holding execution.

Private `publisher/controller.json` records only the current container boot ID;
`controller-initialized.json` detects lost ownership evidence. Both use `0600`
files and are included in stopped-stack backups. Admission and recovery from an
old controller object are refused before mutation after a new boot. Missing,
corrupt or symlinked evidence fails closed; never delete it to resume. A failed
execution-lock acquisition persists an operator stop when the caller still owns
the controller boot, without dispatching or attributing a source failure.

Workers remain proposal-only, deny-all children using transient generation, not
background authoring agents. The lock is retained until every awaited result and
failure path finishes. Transport timeout does not prove provider execution ended;
late provider activity has no trusted publication channel. General shell/PTY or
session descendants remain kernel-confined by reader mode, not authorized by a
worker/session ID. This is execution fencing, **not** automatic scheduling or
manual/automatic priority handover. Those remain release gates for #157.

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
response; do not blindly repeat the command. The route serves only a content-free
projection (operational status, identifiers/counts, blockers and report paths);
record/report contents stay in the private journal and its file viewer.

Inspect and clarify failures first. Known interrupted publication uses the
[publisher's confirmed whole-container restart/recovery](trusted-publisher.md#coordination-and-interruption),
substituting `manual-ingest` for restart/exec. Then `/ingest-new resume` explicitly
requests verification, **not new ingestion**: no active/in-flight writer, queue or
publisher stop; pristine Git; valid sources; matching completed receipts, commits
and exact reports. Failed verification retains the stop. Run plain `/ingest-new`
afterwards only if another attempt is intended. Unknown preparation/staging still
requires maintenance; no general reset/unlock or live rollback exists. Never
delete state to resume. Scheduling, retry/quarantine and quota
controls remain follow-up gates. Synthetic evidence is not real-provider quality
or subscription-exhaustion acceptance.

Verification also rebuilds per-file summary inputs from authoritative journal
records (including blocked files) and admits them to the original main session.
It reconciles the processed scope against each selected revision's durable
admission evidence; missing or inconsistent scope evidence stops instead of
silently omitting a processed file.
`status_delivery_failed` is persisted before that admission and clears only after
its acknowledgement; it proves input admission, not model completion. A lost
summary acknowledgement is not blindly retried. Inspect the original session
and explicitly request verification again if needed; this never repeats ingestion.

## Trusted authoring: `/analysis-save` and `/answer-save`

Reader mode denies the model runtime every write to wiki and answer-inbox data.
These two routes move the corresponding saves onto the trusted channel while
the runtime only *prepares* content as data. Only an immediate browser command
with empty text (or exactly `resume`) and no attachments from the configured
Origin is routed; anything else is refused with
`explicit_same_origin_authoring_required`. There is no fall through to a
direct backend write.

1. The conversation prepares a save: the `wiki-analysis-save` or
   `wiki-gap-review` skill derives the complete page or answer file and shows
   it as preview. Its last message ends with one block: a `karpathy-authoring`
   fence line, one line of JSON
   `{"kind":"analysis"|"answers","target":"<kebab-case-name>","content":"<complete file>"}`,
   a fence line. Content is data, never instructions; truncation markers,
   missing `Erstellt: YYYY-MM-DD` metadata, wikilink metacharacters in titles
   and missing `<!-- END CONFIRMED ANSWERS -->` endings are refused.
2. The operator sends `/analysis-save` or `/answer-save`. The controller takes
   the newest prepared block from the submitting session, re-checks all
   content rules and writes: analyses get `analyses/<slug>.md`, a self-rendered
   `assets/analyses/<slug>.html` (`render-analysis`, same renderer and template
   as the writable mode), the generated alphabetical `index.md` entry under
   `## Analysen` and one append-only `log.md` line, sealed into exactly one
   focused commit `feat(analyses): <Titel> speichern` with a compare-and-swap
   ref update. Answer saves write only
   `/knowledge/incoming/answers/<name>.md`; existing drafts are never touched
   and the file stays unpublished until the `answers` provider publishes it.
   Optional `overview.md` links and argument-based saves are not part of this
   route; link overview pages in a separate maintenance step.

Every check is repeated inside the write path. Saves and ingest runs never
mutate at the same time: either side refuses admission while the other is
active or stopped, both serialize on the controller lock and the wiki-wide
publication lock, and an uncertain publisher stops authoring entirely. The
result summary (commit, changed pages, URLs or inbox path) is delivered to the
submitting session; `publisher/authoring.json` and its initialization marker
hold only content-free coordination state (identifiers, digests, paths), never
saved text.

## Stops and explicit verification of authoring saves

Intent is durable before the first write and before every runtime call.
Extraction or validation failures prove that nothing was written and may be
corrected and retried. An interrupted or unverifiable write persists
`operator_action_required` (`interrupted_authoring`, `authoring_uncertain` or
`summary_admission_uncertain`) instead of continuing. `/analysis-save resume`
and `/answer-save resume` explicitly request verification, never a new save:
a clean wiki with nothing landed clears the stop, content digests identify and
adopt a landed commit even after an operator repair commit, and anything else
(a dirty tree, foreign content at the target) keeps the stop until the operator
repairs the data. A lost summary acknowledgement is redelivered only by that
same explicit verified resume.

## Validation

```sh
node --test tests/test_manual_ingest.mjs
node --test tests/test_trusted_authoring.mjs
sh tests/integration/manual-ingest.sh
```

The disposable proof uses named volumes, an internal network, OpenCode 2.0.26
and a deterministic OpenAI-compatible fixture (no external model/cost): UI
auth/Origin refusal, separate admission, fresh worker proposals, verified commit,
journal/report and reader API/executed-shell denials, plus trusted authoring
admission with a verified analysis commit, rendered print view and answer-inbox
save.
