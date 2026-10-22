# Wiki ingestion

`/ingest-new` starts `wiki_ingest_batch` once. Code snapshots the accepted pending
selection and processes it sequentially, with one commit per source.
`/ingest <source>` uses an exact source-path filter. Already-current sources are
not reinterpreted. Later arrivals wait for another explicit request. No scheduler,
background controller or per-source repository clone is introduced.

## Private per-source attempts

Each source gets one fresh child session and one private draft under
`incoming/ingest-journal/drafts/<batch>/<attempt>/wiki`. It contains the selected
source page and shared editable Markdown context, index/log and safety instructions,
not other source pages, Git history or configuration. Workers can read targeted
live wiki context but cannot write it. Read-only sources stay under `/knowledge/sources`.
Code alone owns frontmatter, index/log and publication. The initializer never
replaces existing `AGENTS.md`; manually align ingest task ownership and the private
draft exception while preserving all local safety rules.

Admission binds child, parent, exact source/target, draft and running Linux process
incarnation in private short-lived records. Prompt data or stale records grant no
admission. Workers cannot read other drafts/control files or change source material.
Directory reads of the draft root and live wiki root are allowed; write permissions
are not broadened.

Completion requires native evidence: fully covered source reads, successful skill
loading and an explicit mutation of the exact selected page, plus valid final files,
unchanged source identity/digest, permitted diff and a verified commit. Session
success or prose alone is insufficient. Rewriting unchanged supported findings is
allowed. Corrected exact missing-target reads, mandatory reads/skills and safe
patch corrections remain auditable; explicit permission rejections are never
forgiven. Pagination must cover the complete source. Native long-line truncation
does not certify unseen text and is reported as an extraction limitation.

There is no application context-budget planner, source chunker, rollover, automatic
repair or source retry. OpenCode V2 has no worker-scoped compaction setting; the
worker compaction hook stops the attempt before a summary request, using the native
boundary rather than another token estimate. This can occur before the provider's
hard context window. Other chats retain normal compaction. A provider context
overflow also leaves that source unpublished. The report explains that source,
wiki context and tool output together reached the model window, and suggests
splitting/preprocessing the source or choosing a larger-context model. A renewed
attempt requires an explicit new request, not routine per-source approval questions.

Independent sources continue after isolated failures, including post-write failures
and context limits. Provider/configuration failures, exhausted storage or foreign
wiki changes stop new dispatch. Native transient provider retries are bounded to
three attempts; no application-level retry loop or model-call/deadline budget is
added. OCR gaps and contradictions are reported, not automatic rejection reasons.

Defaults reserve 32 MiB per draft, limit files to 8 MiB and retained attempts to
256 MiB/16,384 entries. Unchanged copies in failed attempts are reclaimed; changed
or uncertain evidence is retained and never reused. Successful drafts are removed
only after durable publication. A cleanup failure reports `draft_cleanup_failed`
and stops new dispatch without downgrading the verified commit; already-absent
completed drafts are harmless. Capacity exhaustion never silently deletes evidence.
Back up the journal and wiki `.git`; existing retention policy applies.

## Results and interruption

`done` means the accepted selection completed; `partial` means sources failed or
metadata diagnostics remain; `blocked` means a shared technical blocker;
`cancelled` means interrupted. Counts and fresh status expose remaining, invalid
and conflicting sources separately. No automatic reconciliation of historical
live leftovers is performed. Dirty admission preserves edits and stops before
workers start; cleanup requires separate inspection and authorization.

Failed private work closes with a report, not an active recovery conversation.
A new explicit request selects pending sources afresh, never reuses an old draft
or approval. After process death during private work, the next entry closes that
interrupted batch without model work; another explicit request starts new work.

## Durable publication and reports

Control state is required and fsynced before publication; unavailable storage
prevents live writes. Reports are best effort in
`incoming/ingest-journal/batches/<id>/` (directories `0700`, files `0600`).
OpenChamber mounts only this report tree read-only, not drafts/admissions.

Code prepares a private Git index and focused commit object, recording parent,
tree, files and before/after hashes. It checks revision/revocation again, installs
only reviewed files, updates index/HEAD and durably records success. Only interrupted
publication remains active. A normal new call reports `publication_interrupted`
without finishing it. An explicit recovery call with its `batch_id`, plus a separate
`ingest_recovery` approval, verifies actual parent/tree/diff/hashes and completes
missing steps without another worker, duplicate commit or duplicate log entry.
It does not process remaining sources. Foreign deviations stop recovery without
overwriting edits. Commit messages or `current` status alone are never evidence.

The lifetime kernel lock serializes cooperating ingest calls, **not arbitrary
editors**. Keep other writers idle during publication. Readers can see intermediate
file installations; this is not an atomic multi-file reader transaction or OS barrier.
Cancellation during publication retains its checkpoint, never resets or restarts.
The original processing digest is persisted and rechecked, including Paperless
generations that retain a revision while changing bytes. Paperless requires
`source_sha256` plus matching metadata. Evidence checks do not prove semantic correctness,
understanding or faithful extraction by a real model.

`wiki_ingest_diagnostics` is read-only and available in both primary and
orchestrator chats. It returns the active batch's `owner_session`, dirty paths
(at most 100) and ten source statuses with bounded error codes/tool names and
worker session IDs. Paginate with `next_offset`. No transcripts, source contents
or drafts are returned. The observation is non-atomic; missing evidence does not
prove no action occurred. A missing report has null `report_path`. Idle diagnostics
do not create work. Loading an orchestrator skill does not grant its batch tool
to `build`.

Pinned-runtime acceptance uses a deterministic local model and disposable storage;
process-kill tests exercise publication checkpoints. These are not real-model
semantic acceptance or production deployment evidence.
