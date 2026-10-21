# Wiki ingestion

`/ingest-new` invokes `wiki_ingest_batch` once. Code snapshots the pending selection
and processes it sequentially, with one fresh worker and one commit per source.
`/ingest <source>` uses the same implementation with an exact source-path filter.
Already-current sources are not reinterpreted. New arrivals wait for the next
explicit request; there is no scheduler or automatic retry loop.

## Failure handling

- Missing/unusable sources, invalid source metadata, conflicting identities and
  failures **before any write** affect only those sources. Independent sources
  continue. A broken provider manifest does not stop other providers.
- OCR gaps, contradictory accounts and missing details are extraction limits,
  not reasons to reject otherwise useful knowledge.
- Whitespace, Markdown hard breaks and report formatting are not publication
  gates. Worker reports are ordinary Markdown, not a strict JSON protocol.
- Report-storage failure is separate from ingestion: continue, retain verified
  commit results and state clearly that no complete report is available.
- Unrelated dirty Git at admission, unresolved live edits, real Git conflict
  markers, changed source bytes during writing or cancellation stop safely.
  Never reset, stash, discard edits or attempt automatic repair. A clean failure
  is skipped once; an uncertain shared worktree requires manual inspection.

For bulk requests, `done` means no pending/invalid/conflicting sources remain;
for a single-source request it means that source was committed. `partial` means the
selection was attempted with remaining or unusable sources; `blocked` means the
run could not safely continue. The returned counts and fresh status distinguish
these outcomes. A source counts as ingested only after its verified commit.

## Safety and reports

The lifetime kernel lock serializes cooperating ingest calls, **not arbitrary
editors**. Start from clean Git and keep other writers idle. Readers can see
intermediate edits. No automatic reconciliation or per-page transaction recovery
is provided. Source hashes and canonical frontmatter are verified; Paperless
requires `source_sha256` plus matching provider metadata. These checks do not prove
semantic correctness or that a model read every passage.

Code stamps source identities and appends index/log entries; workers edit only
Wiki Markdown and report findings, evidence, revision changes, contradictions and
extraction limits. Reports and `state.json` live privately under
`incoming/ingest-journal/batches/<id>/` (`0700` directories, `0600` files), with
per-source commits and changed pages. OpenChamber mounts only this report tree
read-only. Back up the entire journal and wiki `.git`; no automatic cleanup occurs.
Chat links the report and gives counts/blockers; full details are read on request.

## Worker admission and evidence

The plugin admits only its own active child sessions. Pasting a `batch_id` into
a standalone worker prompt grants no edit permission. The child receives its
assignment as a user message; it does not need its parent's tool transcript.
Code owns status/revocation and Git checks, frontmatter, index, log and commits.
Workers write the exact selected source page and any necessary subject pages or
overview, never index/log. Permission checks use normalized location-relative
paths; an attempted multi-file patch touching a forbidden file is denied in full.

Before stamping a revision, code requires a successful native mutation of the
selected source page in this worker attempt. A pre-existing page, session
`succeeded`, or prose success claim is not evidence of processing. An explicit
write of unchanged findings is allowed. Native tool errors (including reads and
skills) and failed sessions prevent publication; unclear live edits remain in
place for manual inspection.
Private reports distinguish proven source writes from failed tool attempts,
identify the worker and bounded code-owned failure reasons such as
`worker_permission_denied`, `worker_tool_failed`, `worker_session_failed` and
`worker_no_source_write`. Native error bodies are not copied into those reasons.

Runtime tests use a deterministic synthetic model to verify transport, tool
permissions and publication. They do not prove a real model interprets every
instruction correctly or that extracted facts are semantically correct.

### Operator acceptance (separate authorization)

Before deployment, confirm the running image/OpenCode version and inspect the
actual wiki instructions. The initializer never replaces an existing
`AGENTS.md`: manually align only ingest task ownership with the shipped template,
preserving local customizations and all source safety rules. Image updates alone
do not perform that edit.

After an authorized deployment, use synthetic new and changed sources to verify
the summary, subject-page changes, revision, one focused commit and private
report. A real-model acceptance run needs separate approval; deterministic tests
are not a substitute. Rerun pending sources only on explicit request, selecting
them through current status; do not reset, replay current sources, or clean up
revoked knowledge automatically.
