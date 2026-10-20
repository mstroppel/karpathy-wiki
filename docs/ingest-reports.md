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
