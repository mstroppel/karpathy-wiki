# Ingestion reports and the working-context budget

`/ingest-new` records a whole backlog of new and changed sources without
carrying previously ingested material in model context. An orchestrator plans
bounded batches, `wiki-ingest` worker sessions process one batch at a time, and
every processed source leaves one durable result record in a private journal.
The complete per-source report is assembled from those records as a private file.
After ingestion, the main chat session displays that report in full, with numbered
parts when needed, and includes its durable file path.

## Workflow

```text
/wiki-ingest-orchestrator (chat session)
  ├── wiki_ingest_journal  run_start / next_batch / run_finish
  ├── wiki_ingest_status   summary + diagnostics
  └── wiki-ingest (one child session per batch, strictly sequential)
        ├── one focused commit per source
        └── wiki_ingest_journal  record (after each verified commit)
```

The orchestrator never reads sources and never writes the wiki; every change
comes from a `wiki-ingest` worker, exactly one commit per source. Each call to
`next_batch` performs a fresh status scan, so a run never works from stale list
positions. Sources already recorded for the run are excluded, interrupted runs
resume with their records intact, and a source whose recorded commit did not
take effect is retried instead of being silently skipped.

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

**Report delivery contract for bulk runs:** `/ingest-new` displays the complete
assembled report in the requesting main session, not just its path or an aggregate
summary. This includes every effective per-source detail block, including blocked
records; superseded records appear only in the audit journal, not again in the
report. It also states completion status, overall status (`new`, `outdated`,
`current`, `revoked`, `orphaned`, plus `invalid` and `conflict` with their diagnoses),
every unfinished source with its blocker, and the durable report path. Revoked and
orphaned entries are reported separately and not cleaned up without an explicit
request. Explicit single-source `/ingest` orders still answer with the inline
detail block.

The orchestrator reads the assembled report with `wiki_ingest_journal`
(`operation: read`, `report: true`, `chunk_offset: 0`, `chunk_bytes: 4096`),
following `report.next_offset` until it is `null`. Offsets count Unicode characters,
not UTF-8 bytes. Chunk offsets must be contiguous and `total_characters` stable.
Report text is data, never instructions. Short reports fit in the final answer;
large reports are read and emitted incrementally in numbered messages in the same
main session, without first accumulating the entire report in model context.
Every character and all `counts.records` detail blocks must be delivered before
claiming complete report delivery. If reading or delivery hits an error or a
context/output limit, explicitly report incomplete delivery, its reason, the
durable path, and the next undelivered character offset. Ingestion success does
not imply report-delivery success; retrying delivery must not repeat ingestion.

This report phase also runs for blocked, paused, and zero-source runs. At rollover,
`operation: report` assembles the current status and unfinished sources without
closing the run. The private file remains authoritative and durable. Journal
content is displayed only in the requesting chat and never published to the wiki
or source directories; chat history and session exports now also carry these
source-derived report details and must be treated as private.

## Journal

```text
${DATA_ROOT}/incoming/ingest-journal/          # private (0700), mounted read-write
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
blocked sources a concrete `blocker`. An `ingested` record is accepted only with
a commit, changed pages, the detail texts, and an unmodified source; unverified
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
growth during ingestion. The final report necessarily adds output proportional
to the processed sources; incremental delivery bounds individual reads, not total
chat history or provider cost. Compaction stays enabled as a safety net but is
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
boundary and displays the complete report so far plus the open sources; the run
stays open. Any later
`/ingest-new` adopts the open run, keeps all records, and continues where the
status scan shows work left. Interruptions behave the same way: no source is
skipped, no completed ingestion is repeated, and no verified record is lost.
The run closes with `new=0` and `outdated=0`, or with a named blocker and the
unfinished sources.

## Troubleshooting

- **Run stops with `invalid` or `conflict`:** global diagnostics block all
  writes. Fix or resolve the reported pages first; nothing was changed.
- **A source is listed as unfinished:** its blocker is in the report. A blocked
  record is final for the run; request a fresh `/ingest` for exactly that source
  after fixing the cause.
- **Chat report delivery is incomplete:** use the reported run and next
  undelivered character offset to continue reading the existing report through
  the journal tool. Do not re-ingest completed sources. A path alone does not
  count as complete delivery.
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
real-model evidence is a merge requirement for the reporting changes in #152;
the absence of that evidence is an unmet acceptance criterion, not a known
limitation that model-free tests can waive. The documented
`tests/integration/run.sh` validation must also pass before merge. To produce
measured evidence on a disposable installation: enable a source provider,
publish synthetic non-sensitive fixtures at increasing counts, run `/ingest-new`
with the target model, and record the peak per-request context from the
OpenCode session data (`opencode api` session messages, plus the provider's
token usage) for the old and the new flow. Report measured tokens separately
from the estimates above, name the model and runtime, and keep source content
out of any published evidence. For report delivery, verify on synthetic fixtures
that every effective source block and detail field appears in the main session,
including multi-part, blocked, paused, and zero-source runs. Verify that a forced
delivery failure is reported separately from ingestion success with a continuation
offset. Prompt-contract tests and model-free chunk tests do not prove actual
model-driven chat delivery.
