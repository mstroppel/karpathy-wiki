# Wiki ingestion

`/ingest-new` starts `wiki_ingest_batch` once. Code snapshots the accepted pending
selection and processes it sequentially, with one commit per source.
`/ingest <source>` uses an exact source-path filter. Already-current sources are
not reinterpreted. Later arrivals wait for another explicit request. No scheduler,
background controller or per-source repository clone is introduced.

## Drafts, evidence and repair

Workers edit private Markdown copies without Git history under
`incoming/ingest-journal/drafts/<batch>/<attempt>/wiki`. Read-only source material
stays under `/knowledge/sources`. Native read/write/edit/patch operations use the
draft Location. Absolute Live-Wiki writes are denied, not redirected. Code alone
owns frontmatter, index/log and publication. Wiki safety instructions remain binding.
The initializer never replaces existing `AGENTS.md`; operators must manually align
task ownership and the private draft exception while preserving local safety rules.

Admission is bound to the child, parent, exact source/target, draft and running
Linux process incarnation, in private short-lived capability records. Prompt
data, a copied batch ID or stale records after restart grant no admission. Ordinary
file tools cannot edit control state; workers cannot read other drafts/control files.

The completion contract requires native evidence: a fully covered source read,
successful skill loading and an explicit mutation of the exact selected page,
plus valid final files, unchanged source identity/digest, permitted diff and a
verified commit. Session success or model prose alone is insufficient. Rewriting
unchanged supported findings is allowed. Native errors remain in the audit, but
a corrected exact missing-target read, mandatory read/skill or patch no longer
poisons a verified result. Explicit permission rejections are never forgiven.
Catalog-containing patches are rejected before permission admission with a
code-owned correction; the worker must recreate only permitted changes.
Native long-line truncation does not count as a fully read line. If the source
cannot be fully read, ingestion pauses with an extraction limitation rather than
publishing unseen content or repeatedly retrying the same shortened read.

At most three worker attempts and 300 seconds per source per accepted continuation
are allowed. Each child has at most 120 seconds; 96 model requests and a shared
allowance of two runtime provider retries per source. Runtime backoff is retained
only within that allowance/deadline; failed providers pause subsequent dispatch.
Valid partial drafts are inspected and carried forward only with the same base
and original source digest. Allowed repair-file hashes are durable, so an approved
continuation can inspect and reuse them after reopening; changed draft bytes are
not silently adopted. Invalid/unbound drafts are retained but never reused.
Changed sources require fresh processing; removed/revoked sources cannot publish.
Independent sources continue after isolated failures, including post-write failures.
OCR gaps and contradictions are extraction limits, not automatic rejection reasons.

Defaults reserve 32 MiB for a draft, limit individual files to 8 MiB and retained
attempts to 256 MiB/16,384 entries. Unchanged copied files in failed attempts are
reclaimed; uncertain evidence is retained. Successful source attempts are removed
only after durable publication. Capacity exhaustion is a visible technical pause,
never silent deletion of unresolved evidence. Control records/reports remain under
the existing journal retention policy; back up the complete journal and wiki `.git`.

## Questions and continuation

Exhausted automatic repair means `paused`, not silent skipping or definitive
failure. Questions contain opaque decision IDs, exact targets, reasons, proposals
and allowed actions. Chat shows them and waits; subsequent calls may inspect more
questions (`question_offset`) or answer only the supplied `batch_id`/`decision_id`.
Display and binding use the same fresh selection; revision-specific approvals
also pin subsequent worker dispatch to that revision, frontmatter and digest.
Answers require a separate `ingest_decision` approval with no saved wildcard,
in the initiating chat. Bindings cover identity/revision, proposal/drafts, source
bytes, base/index and dirty-file hashes. Changed/stale answers produce new questions;
duplicate or wrong-session answers cannot act. Approval is revalidated after waiting.

- `retry`: a new bounded continuation of the exact open work, not a new selection.
- `reprocess`: only when offered for one exact dirty source at admission; inspect
  and freshly process it after confirmation, never stamp it as already successful.
- `defer` / `unresolved`: explicit user decisions, never budget-exhaustion defaults.
- `cancel`: stop the accepted work; never wake it automatically.

Source states distinguish pending/running/repairing, awaiting user/technical action,
ingested, explicitly deferred/unresolved/cancelled. `done` means the accepted
selection is complete; `partial` records explicit deferral/unresolved work;
`paused` means questions/integrity problems; `cancelled` means explicitly stopped.
Counts and fresh status expose remaining/invalid/conflicting sources separately.
No automatic reconciliation of historical live leftovers is performed. Dirty
admission returns hashes/paths and a bound proposal instead of a naked error.
Production cleanup still needs fresh inspection and separate authorization.

## Durable publication and reports

Control state is required and fsynced before publication; unavailable control
storage prevents live writes. Markdown reports are best effort, independently
stored in `incoming/ingest-journal/batches/<id>/` (directories `0700`, files `0600`).
OpenChamber mounts only the report tree read-only, not drafts/admissions.

Code prepares a private Git index and a focused commit object, recording exact
parent/tree/files and before/after hashes. It validates source/revocation again,
then installs only reviewed files, updates index/HEAD and durably records success.
After process termination, the next explicit entry reconstructs open work and
offers continuation. A confirmed prepared publication verifies actual parent,
tree, diff and hashes and completes missing steps, without another worker, duplicate
commit or duplicate log entry. Foreign deviations are preserved and stop publication.
Commit messages or `current` status alone are never recovery evidence.

The lifetime kernel lock serializes cooperating ingest calls, **not arbitrary
editors**. Keep other writers idle during publication. Readers can see intermediate
file installations; this is not an atomic multi-file reader transaction or an OS
barrier against foreign writers. Cancellation during publication leaves a visible
technical recovery question, never an automatic reset or unattended restart.
Its separately approved `retry` only finishes the prepared publication; the
remaining accepted work stays cancelled. After completion, a new explicit request
can accept remaining sources. Original processing digests are persisted and
required for both prepared publication and partial-draft reuse, including
Paperless generations that retain a revision while changing their independent digest.
Source hashes and canonical frontmatter are verified; Paperless requires
`source_sha256` plus matching metadata. These checks and read coverage do not prove
semantic correctness, understanding or faithful extraction by a real model.

Runtime acceptance uses the pinned OpenCode and a deterministic local model.
Process-kill tests exercise publication checkpoints. Production deployment, old
live leftovers and synthetic real-model acceptance (new/outdated source,
correction, question/answer/resumption and cancellation) require separate approval.
