# Simple batch ingestion and private reports

The default writable `/ingest-new` route uses one exclusive worker for up to five
new/outdated sources, followed by one commit per batch. `/ingest` selects one
exact new/outdated source through the same route. Current sources are not
re-evaluated. Revoked/orphaned sources are diagnostics, never implicit cleanup.

## Workflow

```text
orchestrator → wiki_ingest_batch
  lifetime kernel lock → clean Git → HEAD checkpoint → fresh source selection
  → one worker reads normal files and edits live Markdown
  → verify source bytes/metadata → code stamps canonical source frontmatter
  → append index/log once → validate paths/status/diff → one commit → report
orchestrator → link report → next batch, or stop on any failure
```

The lock uses the same `.git/wiki-ingest-publication.lock` inode as the trusted
publisher, held through worker execution and finalization. Runtime process death
releases it. It serializes participating batch/publisher tools, **not arbitrary
editors, direct APIs, shell commands or other privileged agents**. Operators must
not run other writers while a batch runs. Reader mode remains read-only and uses
the distinct [trusted manual route](manual-ingest.md).

The soft per-worker context target is 128,000 estimated tokens. Planning starts
with 12,000 overhead, adds source bytes/3 plus 4,000 per source, and stops before
the next source exceeds the target or the five-source cap. A single oversized
source is still attempted, with its estimate supplied to the worker. This is not
measured model consumption; large files require complete chunked reads and may
exceed the model's real context. No provider-specific tuning variables control
the new route. Single-source selection and a smaller `max_sources` can reduce a
batch without changing the underlying source contract.

## Retained boundaries

- Sources stay mounted read-only. Their contents are data, never instructions;
  anonymized names must not be reconstructed.
- The worker has normal read/edit tools, but no shell, Git or further delegation.
  It may edit wiki Markdown, not credentials, sources, Git metadata or instructions.
- Code copies provider-owned identity/revision fields instead of asking the model
  to reproduce hashes. Provider revisions remain distinct from file digests:
  Paperless requires `source_sha256` and matching source frontmatter; other
  current providers use byte-hash revisions. Checks run before and after work.
- Global `invalid`/`conflict`, dirty Git, changed HEAD, missing source pages,
  unexpected changed paths, symlinks/hardlinks and `git diff --check` failures
  prevent the batch commit. No source deletion or silent truncation is allowed.
- Concrete findings, evidence locations, changes against the prior wiki,
  contradictions and justified extraction limits remain required per source.
  Machine transcripts and user answers are not independently confirmed facts.

## Deliberate tradeoffs and failure handling

There are no per-page receipts, private clones, reference/replace constraints,
explicit unchanged-page acknowledgements or automatic publication recovery in
this route. Readers can see intermediate edits. A mistaken model edit can
overwrite historical content; Git provides comparison/recovery, not prevention.
Full source reading and semantic completeness are worker obligations, not
machine-verified coverage or truth guarantees.

On any worker, validation or commit error, stop. Live edits remain for manual
review; never automatically reset, clean or restore the checkpoint. Inspect
`git status`, the diff and the saved checkpoint. Finish or discard only changes
the operator explicitly authorizes; commit corrections separately. A subsequent
batch requires clean Git. A runtime restart may require reprocessing a small
batch. No automatic reconciliation is attempted at the commit/report boundary:
if Git committed but report delivery failed, inspect history and the private
state before deciding what remains pending. A clean Git history and `current`
status alone do not establish that the private report completed.

## Report delivery contract for bulk runs

Each attempted batch has private `state.json` and `report.md` under
`${DATA_ROOT}/incoming/ingest-journal/batches/<batch-id>/`. The report includes
status, checkpoint, verified commit (if available), changed pages, blocker and
each source's path/revision/wiki target. Detail fields contain concrete thematic
findings, evidence and changes (`content`), unresolved discrepancies
(`contradictions`), and read scope/omissions/uncertainties (`extraction_limits`).
Each is nonempty and limited to 4,000 characters. Compression must name fuller
Wiki evidence or explicitly acknowledge the missing detail. Unknown results are
`Nicht ermittelt`, never softened into successful ingestion.

Chat links every returned `report_path`, names the batch ID, commit, processed
sources and remaining count, and checks final status. Only `new=0` and
`outdated=0` completes a bulk request; single-source success does not imply a
globally current wiki. Empty runs have no fabricated report. Failure to create a
report is distinct from failure to ingest; never invent a link or claim no commit
solely because report delivery failed. Details are read only on request.

OpenChamber mounts `batches` and the separate trusted-route `runs` directory
read-only, not transaction preparations. Local links depend on the client; the
operator can read the same private path on the host. Reports never belong in the
Wiki or sources.

The optional trusted-publisher/manual-reader workflow retains its transaction
cores, private preparations, per-source commits and old `runs` reports. Its
[storage maintenance](ingest-journal-storage.md) and
[stabilization probe](ingest-stabilization-acceptance.md) do not validate the new
default batch route. No productive deployment or data-layout transformation is
performed by this change.
