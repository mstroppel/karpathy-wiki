# Ingestion reports and the working-context budget

`/ingest-new` records a whole backlog of new and changed sources without
carrying previously ingested material in model context. An orchestrator plans
bounded batches, `wiki-ingest` worker sessions process one batch at a time, and
every processed source leaves one durable result record in a private journal.
The complete per-source report is assembled from those records as a private file.
After ingestion, the main chat session links that report with a short status
summary and its durable file path. Details remain outside model context.

## Workflow

```text
/wiki-ingest-orchestrator (chat session)
  ├── wiki_ingest_journal  run_start / next_batch / run_finish
  ├── wiki_ingest_status   summary + diagnostics
  ├── wiki-lint (read-only Git preflight / confirmed repair, sequential)
  └── wiki-ingest (one child session per batch, strictly sequential)
        ├── wiki_ingest_transaction  prepare / apply / validate / confirmed rollback
        ├── one focused commit per source
        └── wiki_ingest_journal  record (after each verified commit)
```

The orchestrator never reads sources and never writes the wiki; ingestion changes
come from a `wiki-ingest` worker, exactly one commit per source. Confirmed repairs
go to `wiki-lint`, sequentially, with separate correction commits. Each call to
`next_batch` performs a fresh status scan, so a run never works from stale list
positions. Successfully recorded or explicitly skipped sources are excluded, interrupted runs
resume with their records intact, and a source whose recorded commit did not
take effect is retried instead of being silently skipped.

## Ingest validation and repair

Provider revisions and file integrity are distinct. Paperless hashes its document
inputs and export settings, then embeds that revision in the rendered Markdown;
it cannot equal the SHA-256 of that same Markdown. The Paperless publisher records
the rendered file's `source_sha256` in its manifest and verifies those bytes before
reusing a published generation. Prepare requires that publisher digest, checks
identity/revision against the manifest and pins `source_sha256` in the private
receipt. Missing or mismatched publisher digests fail closed; they are never
inferred by accepting the current bytes. All subsequent steps recheck both. Other current providers
use byte-hash revisions, which are also verified against the file. Changed bytes,
identity, revision or publication path invalidate the preparation.

`wiki_ingest_transaction` prepares one selected source against a clean Git
checkout and a fresh source status. The worker declares all relative wiki paths
before writes and retains the returned `preparation_id`. `apply` accepts the
complete source-page draft, derives canonical frontmatter from the fresh status,
and preserves extra fields; supplied conflicting identity or revision is rejected.
`prepare.changed_pages` must include the source page, `overview.md`, `index.md`,
and `log.md`. For new thematic pages, `apply` takes a declared relative `page`
and a complete `draft`. Existing thematic pages accept only exact, sequential
`edits: [{old_text, new_text}]` or `append`; the untouched text remains on disk,
not in the model's reconstructed draft. Each `old_text` must match exactly once;
an absent or ambiguous match rejects the entire call before writing. `edits: []`
explicitly acknowledges a reviewed, unchanged overview or index. `log.md` accepts
only `append`, preserving all historical bytes. All worker writes use this API.
Validation requires all three shared pages to have been applied/reviewed and a
new log entry, preventing a source-only transaction from being approved for commit.
Overview/index need not change bytes when the reviewed findings or catalog entry
are unchanged. This is a structural completion gate, not a semantic quality test;
workers must still review the diff and evidence. A targeted edit can itself be
wrong, and an arbitrary shell commit is not prevented by this API.
`validate` accepts only baseline bytes or hashes owned by successful `apply`
operations, rejecting foreign edits even on declared paths before the worker commits.
Only those validated contents may be committed. An `ingested` journal record must
include `preparation_id`; the journal checks the actual commit tree, changed paths,
and source hash rather than trusting the worker's success claim. This validates
provenance and file contents, not semantic completeness of model extraction.
An explicit reread may leave findings unchanged; byte differences are not evidence
of reading. Reapplying a corrected draft requires the previous applied bytes still
to match and invalidates prior validation. Validate again before committing.
Pages and preparation receipts are written to exclusive sibling temporary files
and installed only after complete writes. Page publication first persists intent,
moves an existing destination into retained private `.git/ingest-backup-<random-id>/page`
evidence, checks the displaced bytes, then exclusively links the new draft into
the absent destination. There is a short absent-page interval, but no overwrite:
a concurrent destination creator wins; racing edits to the displaced inode remain
in its backup, including writes through old descriptors. A detected mismatch blocks
publication and restores the displaced inode only if the destination is absent.
Receipts retain every displaced-inode hash after handoff; missing evidence or later
descriptor edits block apply, validation, and journal commit acceptance.
Backups are never automatically deleted; see [data layout](data-layout.md).
A final receipt-write failure is retryable using the saved pending hashes: `apply`
or `validate` can finish the handoff only if the page and retained evidence match.
Pending writes also retain a request fingerprint: retrying the same `apply`
after a failed final receipt write acknowledges the installed incremental edit
without appending it again or rematching already replaced text. After a successful
call, another `append` is a new append; this is not general request deduplication.
If interrupted before installation, retry restores a copy of the verified previous
page into an absent destination, preserving the backup. This is not a lock against
arbitrary external editors or a host sandbox; external writers must be stopped
before confirmed cleanup. Calls for one preparation are serialized within the
server process; queued calls stop if their predecessor fails. The exclusive disk
lock still rejects another process. Abrupt process termination can leave a temporary file or lock;
treat those as blockers for confirmed maintenance, never permission to erase
unmatched work. Omitted extra
frontmatter fields remain preserved; `apply` does not remove them.

After confirmed repair, own uncommitted mistakes can be corrected and validated again in the same source
transaction. Foreign work is preserved and blocks ingestion. Already committed
mistakes require a confirmed, separate `wiki-lint` correction commit; maintenance
does not count as successful ingestion and never rewrites history. The orchestrator
can dispatch that maintenance directly without asking the user to switch agents.
Each run first asks `wiki-lint` for a targeted, read-only Git preflight; open Git
changes trigger confirmation before batch planning, rather than repeated worker
failures. Workers still check clean state at preparation to catch intervening work.
For repaired sources, if the run already contains final blocked records, finish it and deliver its report
before starting a fresh continuation run; otherwise those records would exclude
repaired-but-still-pending sources from planning. Closed runs and reports remain audit.
Workers stop on the first source, tool, Git or transaction error, even with a clean
worktree; remaining sources are not attempted or given speculative blocked records.
The planner returns an empty batch and `recovery_required: true` for any
unacknowledged blocked record, including records adopted from an interrupted run.
The orchestrator offers concrete repair, rollback-and-skip, or abort through
`question` before dispatching more ingestion work.

For an isolated failure, confirmed `rollback` restores only the failed
preparation's owned, uncommitted pages to their baseline, including removing its
new pages. It checks HEAD, index, ownership and retained evidence, requires no live
provider, preserves displaced files privately, and can resume an interrupted reset.
It never resets commits or discards foreign changes. Existing successful source
commits remain intact. Changed HEAD, foreign edits or stale locks require maintenance.
After rollback and a clean Git check, `skip_blocked` with `record_index` and
`confirmed: true` acknowledges exactly that failure for this run and excludes its
source identity (adapter and key) even if the provider republishes a new revision.
The rolled-back receipt must match the failed source's identity, revision and paths.
Without a
preparation (e.g. prepare failed), only the clean Git check is needed. The blocked
record remains in counts and the report; skipped sources are not reported as
ingested. Any later failure stops planning again. A later run retries pending
sources normally. Systematic provider failures should be repaired instead of
skipping each affected source; wiki maintenance must never fabricate a revision
or edit provider files to bypass an integrity check.

## Report

The report is written to a private file inside the journal directory and is
assembled deterministically from the durable records:

```text
${DATA_ROOT}/incoming/ingest-journal/runs/<run-id>/report.md
```

It contains run metadata, the final status counts, every unfinished source with
its blocker, and one detail block per processed source: exact source path,
source revision, verified commit, changed wiki pages, incorporated content,
contradictions or open questions, extraction limits, and whether the source file
stayed unmodified.

### Content depth

The worker's **Inhaltliche Berichtstiefe** section in
`config/skills/wiki-ingest/SKILL.md` is the authoritative writing and completeness
contract for both journal records and explicit single-source reports. A brief
worker handoff or chat summary does not justify a brief private report.

The existing `content` field carries thematic findings, evidence locations,
affected wiki paths, and a comparison with the previous wiki. The
`contradictions` field carries specific discrepancies and open questions;
`extraction_limits` accounts for unreadable passages, deliberate omissions and
report-budget compression. Machine-transcribed conversation statements remain
attributed statements, not independently verified findings. Reading every line
does not imply extracting every statement. A test source with little usable
content should be described honestly rather than padded.

The detail-text fields and budgets are unchanged: text fields accept single-line
text, at most 4,000 Unicode characters each, and the entire record must fit
8 KiB. Workers use thematic labels and separators, reduce repetition first, and
identify compressed topics with evidence locations and wiki paths to fuller
extractions (or explicitly state that none exists). Deterministic assembly
preserves those texts; it cannot judge their semantic completeness. Prompt
contract tests protect the instructions, not actual model-driven report quality.

For installation acceptance, use synthetic sources covering a multi-topic
revision, unchanged substantive content, a damaged transcript, and a short test
source. Check that every material topic is concretely represented or explicitly
accounted for as an omission, changes match the source commit, and evidence
locations and wiki references resolve. Verify that limitations qualify specific
statements without replacing them. Include a record near the byte limit to
check that any compression is disclosed. This acceptance remains pending user
testing; historical delivery evidence does not validate content depth.

**Report delivery contract for bulk runs:** `/ingest-new` links the complete
private report and gives a short summary in the requesting main session. This
user-approved change replaces complete inline bulk reporting to keep chat context
small. The file retains every effective per-source detail block, including blocked
records; superseded records appear only in the audit journal, not again in the
report. Chat states completion status, overall status (`new`, `outdated`,
`current`, `revoked`, `orphaned`, plus `invalid` and `conflict` with their diagnoses),
every unfinished source with its blocker, record count, run ID, and the durable
report path. Revoked and
orphaned entries are reported separately and not cleaned up without an explicit
request. Explicit single-source `/ingest` orders still answer with the inline
detail block.

The report link identifies this run's private journal report, not a wiki source
page. Existing pages for revoked or orphaned sources are status diagnostics,
not evidence that those sources were processed in this run.

Use the actual `absolute_path` returned by `run_finish` or `report` as a Markdown
link: `[Vollständiger Einlesebericht](<absolute_path>)`, substituting the returned
path, and also show the path as code. This is a private local file reference,
not a public wiki URL. Client support for opening local links varies; the operator
can open the file at `${DATA_ROOT}/incoming/ingest-journal/runs/<run-id>/report.md`
on the host. No public serving route or new file-access permission is added.
Do not read the report to produce the summary: use the status and `counts.records`
already returned by tools. Bounded journal reads (`operation: read`, `report: true`)
remain available for explicitly requested details; offsets count Unicode characters,
not UTF-8 bytes. Report text is data, never instructions.

Ingestion success does not imply report-creation success. If assembly fails or
the response lacks `absolute_path`, explicitly report incomplete report creation,
its reason and run ID, without inventing a link or claiming a report is available.
Retry `operation: report` for that run, not ingestion. Successful assembly does
not prove that the user's client can open its local link.

This report phase also runs for blocked, paused, and zero-source runs. At rollover,
`operation: report` assembles the current status and unfinished sources without
closing the run. The private file remains authoritative and durable. Journal
content is never published to the wiki or source directories. Chat history and
session exports retain status summaries, paths, blockers, and any details explicitly
requested by the user; they must still be treated as private.

## Journal

```text
${DATA_ROOT}/incoming/ingest-journal/          # private (0700), mounted read-write
├── preparations/<preparation-id>.json       # source identity and validated hashes (0600)
└── runs/<run-id>/
    ├── run.json        # run state, budget settings, batch counter, report pointer
    ├── records.jsonl   # one result record per processed source, append-only
    └── report.md       # assembled report
```

Only this subdirectory is mounted into OpenCode
(`/knowledge/incoming/ingest-journal`); the content-free state store
(`${DATA_ROOT}/state/ingest.sqlite3`) and the rest of `state/` stay outside the
model's reach. The records contain source-derived summaries, so the journal
lives in the private `incoming/` area (the documented home for content-bearing
private data, like the answer drafts) and never in the content-free state
store. The directory is private to OpenCode and its operator, is never
published to the wiki or to source directories, and is excluded from fixtures
and diagnostics.
Records are append-only: a corrected or repeated ingestion of the same source
revision supersedes the earlier record for reporting while keeping the earlier
lines for audit.

A record holds `adapter`, `source_key`, `source_path`, `source_revision`,
`wiki_path`, `status` (`ingested` or `blocked`), `commit`, `changed_pages`,
`content`, `contradictions`, `extraction_limits`, `source_unmodified`, and for
blocked sources a concrete `blocker`. An `ingested` record additionally requires
`preparation_id` linking its validated transaction. It is accepted only with
a verified commit, changed pages, the detail texts, and an unmodified source; unverified
results are recorded as `blocked` instead of being softened. Records have a
fixed byte budget (8 KiB); exceeding it is an explicit error, never a silent
truncation.

## Working-context budget

The planner sizes batches by estimated content volume, not by source count. All
figures are documented estimates (four characters per token) applied to
measured file sizes; they are not measured model tokens.

```text
worker session estimate = fixed overhead (4 000 tokens)
                        + per source: content estimate + 4 000 tokens overhead
```

The per-source allowance covers index, overview, log, diffs, commits, and
affected pages beyond the target page. Configuration (`.env`):

| Variable | Default | Meaning |
| --- | --- | --- |
| `WIKI_INGEST_BATCH_BUDGET_TOKENS` | `32000` | Estimated working context per worker session |
| `WIKI_INGEST_BATCH_MAX_SOURCES` | `4` | Upper bound of sources per batch |
| `WIKI_INGEST_RUN_MAX_BATCHES` | `12` | Batches per run before rollover (`0` = unlimited) |

Set the token budget to roughly *model context − output reservation − fixed
instructions*, and keep headroom for the worker's final answer and tool output
capped by `tool_output` in `opencode.json`. Smaller models want a lower budget
and smaller batches; the batch size is a cap, the budget is the constraint.

Deterministic before/after evidence from `tests/test_ingest_context_budget.mjs`
(synthetic fixtures of 12 KiB per source, budget 32 000, estimates):

| Sources | Single session (before) | Batch peak (after) | Batches |
| --- | --- | --- | --- |
| 10 | 74 720 | 25 216 | 4 |
| 25 | 180 800 | 25 216 | 9 |
| 50 | 357 600 | 25 216 | 17 |

The same fixtures with growing source sizes (10 sources, estimates):

| Bytes per source | Single session (before) | Batch peak (after) | Batches |
| --- | --- | --- | --- |
| 12 KiB | 74 720 | 25 216 | 4 |
| 48 KiB | 166 880 | 20 288 | 10 |
| 200 KiB | 556 000 | 59 200 (oversized, one source) | 10 |

A batch either fits the budget or is exactly one oversized source; the reported
peak then honestly exceeds the budget because that source runs alone in its own
session with staged reading.

The previous `/ingest-new` grew linearly with the backlog in one session (a
reported run reached about 191 016 tokens); batch sessions stay at the same
peak, and during ingestion the orchestrator's own context stays compact because details go to
the journal and batch handoffs are one line per source. These figures are
estimates applied to synthetic fixtures of known size, not measured model
tokens; `tests/test_ingest_context_budget.mjs` also varies source sizes and
verifies that a batch either fits the budget or contains exactly one oversized
source. Measured numbers come from the opt-in run below and are reported
separately, naming the model and runtime.

## Orchestration mechanisms

Three mechanisms were compared for the bulk path (issue #152 asks for the
smallest supported approach):

| Mechanism | Worker context | Orchestrator context | Why not chosen alone |
| --- | --- | --- | --- |
| One fresh session per source | minimal | grows with source count | most session starts; shared pages re-read per source |
| One session for the whole backlog (previous `/ingest-new`) | grows linearly | n/a | the reported 191 016-token run; degraded late sources |
| OpenCode compaction in one session | lossy summary | n/a | compaction is lossy and is not a provenance or resume mechanism |

Bounded batches combine the first two: per-worker context is capped by the
budget and the source cap, and the run rollover caps the orchestrator's own
growth during ingestion. The complete report grows on disk, not in chat context.
The summary still grows with the number of unfinished sources and blockers;
linked reporting is not a constant-context guarantee for those cases.
Compaction stays enabled as a safety net but is
never relied on: resume and reporting come from the journal.

OpenCode V2 support was verified against the V2 documentation rather than
assumed: the `subagent` tool starts child sessions with fresh context and the
`subagent` command field decides child versus current session; the default
nesting depth is one, so the orchestrator runs in the current session
(`/ingest-new` sets `subagent: false`) and dispatches workers one level deep.
Runtime behaviour is covered by the opt-in acceptance run, not by CI.

## Cost and latency

- Each worker session pays fixed instruction and tool overhead once per batch:
  larger batches amortize that overhead (fewer session starts, fewer repeated
  reads of `index.md`, `overview.md`, and `log.md`) at the price of more
  context per session. Smaller batches cost more starts and re-reads but fit
  smaller models.
- Sequential processing is deliberate: one wiki writer at a time with one
  commit per source. Backlogs therefore cost wall-clock time roughly linear in
  the number of batches; parallel workers are not an option.
- Sources that were already processed cost nothing twice: planning reads fresh
  status, so committed sources leave the pending set and are never re-read.
- Rollover adds one extra `/ingest-new` invocation per
  `WIKI_INGEST_RUN_MAX_BATCHES` batches; the resumed run re-reads no completed
  work.

A source that alone exceeds the budget is never dropped: it is planned as a
single-source batch, marked `oversized`, and the worker must read it in
validated stages (`read` with `offset` and `limit`) or record a concrete
blocker. Silent truncation or a false claim of complete extraction is never
allowed.

## Rollover and resume

After `WIKI_INGEST_RUN_MAX_BATCHES` batches the run stops cleanly at a batch
boundary and links the complete report so far plus lists the open sources; the run
stays open. Any later
`/ingest-new` adopts the open run, keeps all records, and continues where the
status scan shows work left. Interruptions behave the same way: no source is
skipped, no completed ingestion is repeated, and no verified record is lost.
The run closes with `new=0` and `outdated=0`, or with a named blocker and the
unfinished sources.

## Troubleshooting

- **Run stops with `invalid` or `conflict`:** global diagnostics block all
  ingestion writes. The orchestrator offers a concrete repair through `wiki-lint`
  and requests confirmation unless its scope was already explicitly approved.
  After repair it rechecks source status and requires a clean Git status before
  replanning. A failed repair closes the run with blockers, not a retry loop.
  Already committed errors may exist: a clean Git status is not proof of valid
  provenance. A closed run stays closed; continuation starts a new run.
- **Local Git changes:** inspect and identify foreign work before writing. Commit
  it separately only with explicit consent; do not silently stage or discard it.
- **Interrupted worker replaced a shared page with a partial draft:** pause writers
  and back up the wiki and private journal. Have `wiki-lint` inspect the exact
  uncommitted diff against the last clean commit. Confirm a repair scoped to the
  identified worker-owned files; preserve unrelated edits and retained backup
  evidence. Recheck clean Git and source diagnostics, close a run with final
  blocked records, then start a fresh `/ingest-new` run. This workflow does not repair
  existing installations automatically or alter production data.
- **A partial source-only commit already made a source `current`:** the normal
  backlog scan will not retry it. Confirm separate maintenance of the missing
  overview/index/log updates, or explicitly request `/ingest <source>` to reread
  that current source. Preserve the original commit and blocked audit record;
  do not rewrite history or count maintenance as successful ingestion.
- **Revoked or orphaned entries:** maintenance is separate from ingestion. Confirm
  a concrete cleanup plan, including derived claims; missing sources alone do
  not authorize deletion. Never replace an old revision with the current hash
  without evidence that the content was actually evaluated at that revision.
- **A source is listed as unfinished:** its blocker is in the report. A blocked
  record is final for the run; request a fresh `/ingest` for exactly that source
  after fixing the cause.
- **The client cannot open the report link:** open the stated file in the private
  host journal directory, or explicitly request bounded details through the journal
  tool. Do not publish it to the wiki or re-ingest completed sources.
- **Report creation failed:** retry `operation: report` with the same run ID;
  do not re-ingest completed sources or claim that a report is available.
- **The report file is missing or incomplete:** it is assembled at `run_finish` and
  after every `report` call from the journal records. A run that rolled over
  reports its path even while open.
- **Manual mitigation:** a single source can always be ingested in its own fresh
  session with `/ingest <source>`; that session touches only that source and
  records its own result.

## Validation

Ordinary CI is model-free: `tests/test_wiki_ingest_journal.mjs` covers run
lifecycle, record validation and supersede semantics, bounded listing and
chunked retrieval (including complete multi-chunk report reconstruction), report
assembly, batch planning, oversized sources, and
rollover; `tests/test_ingest_context_budget.mjs` produces the deterministic
before/after table above; `tests/test_wiki_ingest_orchestration.py` pins the
orchestration and reporting contract in the skills, command, and permissions.

An opt-in acceptance run with a real model is not part of CI, but passing
real-model evidence is a merge requirement for delivery-contract changes such as
the reporting changes in #152; the absence of that evidence is an unmet
acceptance criterion, not a known limitation that model-free tests can waive.
That evidence exists for the earlier inline contract
([ingest-report-acceptance.md](ingest-report-acceptance.md)); the linked-report
contract replaces it on explicit maintainer request, and its acceptance status
is stated below as an open follow-up. The documented
`tests/integration/run.sh` validation must also pass before merge. To produce
measured evidence on a disposable installation: enable a source provider,
publish synthetic non-sensitive fixtures at increasing counts, run `/ingest-new`
with the target model, and record the peak per-request context from the
OpenCode session data (`opencode api` session messages, plus the provider's
token usage) for the old and the new flow. Report measured tokens separately
from the estimates above, name the model and runtime, and keep source content
out of any published evidence. Installation-based acceptance of linked reporting
is pending user testing and stays an open follow-up rather than a waived
requirement. Verify on synthetic fixtures that the main session shows
the correct private link, path, status, blockers, record count, and run ID without
reading or copying the report, including large, blocked, paused, and zero-source
runs. Open the file and verify every effective source block and detail field.
Force a report-creation failure and verify it is reported separately from ingestion
success without a fabricated link or repeated ingestion. Check the chosen client's
local-link behavior and the host-path fallback. Prompt-contract tests and model-free
chunk tests do not prove actual model-driven linked reporting.

See [historical inline-report acceptance evidence](ingest-report-acceptance.md)
for the earlier opt-in run. Those observations do not validate the linked contract.
