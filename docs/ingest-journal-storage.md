# Ingest-journal storage

The journal is not just a log. Before #215, every source preparation retained
an independent **full-history Git clone**, a working copy and draft recovery
files indefinitely. Removing report detail would not remove those copies.
New preparations instead clone only their baseline commit over Git's transport
(`--no-local --depth=1`): no hardlinks, alternates or shared mutable worktrees.
The live wiki retains its complete history.

## Inspect and reclaim

Run from the installation checkout, with the trusted writer running but idle. Finish
or explicitly recover interrupted ingest/authoring first. Stop external/manual
writers; do not edit proposal directories. Keep a verified stopped-stack backup
of the wiki **including `.git`**, publisher state and the entire private journal.

```sh
# For the manual-ingest profile; use publisher for the operator-only profile.
writer_service=manual-ingest
# Inspection only; does not create locks, receipts or other files.
docker compose exec -T "$writer_service" node /opt/karpathy-wiki/publisher/journal_maintenance.mjs

# After reviewing the dry run and verifying the backup:
docker compose exec -T "$writer_service" node /opt/karpathy-wiki/publisher/journal_maintenance.mjs --confirm

# Alternative policy: retain the two most recently completed verified drafts.
docker compose exec -T "$writer_service" node /opt/karpathy-wiki/publisher/journal_maintenance.mjs --confirm --keep-completed 2
```

In direct writable runtime mode, stop OpenCode first, then use the maintenance
image as a one-shot operator command (append `--confirm` only after inspection):

```sh
docker compose stop opencode
docker compose --profile publisher run --rm --no-deps --entrypoint node publisher /opt/karpathy-wiki/publisher/journal_maintenance.mjs
```

Restart OpenCode after maintenance with `docker compose start opencode`.

An unused, empty publisher-state directory needs no UI controller state. Existing
publisher/controller authority must verify; missing initialized state is never
treated as an empty namespace. The command acquires existing run/report locks
as well as the wiki lock. It creates no authority or lock files itself.

Output contains preparation IDs, logical byte counts, candidate counts and
retention reasons, never source keys, source paths, document text or raw errors.
`storage` distinguishes proposal files, proposal Git data, nested draft evidence,
top-level receipts, run/report data, other journal files and live publication
backups. `candidate_bytes` is the removable file payload, not a promise about
filesystem blocks or compressed backup savings. Inspection hashes files and
verifies commits, so large journals can take time to inspect.
Live backup counts accept hardlinked regular files and count each directory entry's
logical size, not unique inode storage. If a backup cannot be counted safely,
`storage.live_backups_unreadable` is true and its byte count is partial; other
candidates can still be inspected. This never relaxes draft ownership or retained
backup evidence checks.

The observed production total of **3.8 GiB has not been independently broken
down**. Use this dry run on that installation; synthetic results below are not a
measurement of private production data.

## Deletion rules

Only preparations satisfying **all** of these checks are candidates:

1. Publication phase is `done`, without pending writes or rollback state.
2. The run is completed without unfinished sources; its original success records
   agree with the publication, and its report reproduces exactly from the journal.
3. The accepted commit remains reachable from live `HEAD`. Its single parent,
   complete changed-page set and every validated page hash match the receipt.
4. Live retained backups and nested draft evidence are intact. The draft has no
   foreign tracked, untracked or ignored edits, changed refs, hidden index flags,
   lock files, symlinks or multiply-linked files. Nested Git metadata must match
   the ownership hashes recorded when the clone was created. Only exact recorded
   backup files and integrity-checked objects reachable from the accepted commit
   may be added; unknown files or directories anywhere below `.git` retain the draft.
5. Exclusive existing controller/admission, trusted-writer and wiki-publication
   locks for the deployed writer mode are held, and existing publisher/controller
   state is readable and idle. The controller initialization marker and boot
   authority must both validate, without creating or rewriting either file.
   Report writers use the same per-run lock. This includes the model-facing
   journal `report` tool: it creates `records.lock` when absent, queues behind
   record appends/run completion in the same process, and refuses concurrent
   cross-process writers with `Ein anderer Schreiber hält den Ingest-Lock`.

Active, interrupted, blocked, failed, rolled-back, incomplete and orphaned work
is retained, regardless of age. Missing or inconsistent proof never becomes
permission to delete. Cleanup does not repair foreign wiki/index changes.

Before the first deletion, the parent receipt durably retains the small nested
transaction receipt and a hash manifest of the draft. Only manifest-owned files
are unlinked; directories are removed only when empty. Interrupted deletion can
be inspected and retried with the same command. New or changed files after an
interruption block that candidate. After completion, only compact proof and byte
counts remain, not the manifest or clone. Reports and audit records are unchanged.
Current-publication verification and idempotent `resume` still work; historical
verification checks original commits rather than today's provider revision.

## Retention and remaining growth

`--keep-completed N` defaults to **0**. It retains the newest N verified completed
drafts by run completion time; ties use preparation ID. A deletion already begun
is finished rather than counted as a retained draft. This policy never applies
to unresolved work. Cleanup is **not enabled automatically**: schedule this
confirmed command in an operator-controlled maintenance window, or run it after
each completed ingestion. Review refusals instead of blindly retrying forever.
With cleanup after every successful run, completed draft count is bounded by N;
with periodic cleanup, add the work completed between maintenance windows.

This is not a fixed bound on the whole data directory. Live wiki history,
receipts, reports/audit records, publisher replay data, unresolved attempts and
live `.git/ingest-backup-*` files still grow. Drafts without creation-time Git
ownership evidence, with changed metadata or with unreachable extra objects also
remain untouched; maintenance never manufactures ownership for them. Backups can include displaced inodes
that another process still has open: cleanup **never removes live publication
backups or index candidates**. Inspect them separately with writers stopped;
never delete them merely because a commit is old. Keep verified private backups
covering these categories; do not exclude the journal to accelerate archiving.

## Refusal diagnostics

| Reason | Action |
| --- | --- |
| `journal_maintenance_refused` | Check initialized publisher/controller state, existing locks, idle writers and a clean wiki. The command is operator-only, not a model/HTTP tool. |
| `writer_lock_unavailable`, `wiki_index_lock_requires_operator` | A writer or unresolved Git operation prevents cleanup. Do not remove lock files to force progress. |
| `lock_file_missing` | Required lock initialization is absent (including a never-published installation). Initialize through the normal writer lifecycle; maintenance never creates missing lock inodes. |
| `live_backups_unreadable` (storage marker) | The live-backup byte count is partial. Inspect backups privately with writers stopped; cleanup never follows their symlinks or removes them. |
| `unresolved_preparation`, `run_not_successfully_closed` | Inspect the private transaction/run and complete explicit recovery; age is irrelevant. |
| `live_backup_evidence_changed` | Preserve the backup and investigate missing data or late writes through old descriptors. |
| `invalid_controller_fence` | Restore/repair the controller marker and boot authority with the writer stopped; maintenance never initializes authority. |
| `draft_git_ownership_missing`, `foreign_git_metadata` | Keep the draft and inspect its metadata privately; no deletion intent is created without creation-time ownership proof. |
| `commit_*`, `journal_evidence_mismatch`, `report_evidence_mismatch` | Compare the private receipt, journal, report and verified backup. Do not rerun ingestion just to manufacture proof. |
| `foreign_draft_*`, `hidden_draft_changes`, `unknown_draft_entries` | Preserve and inspect foreign work; it is not adopted or overwritten. |
| `cleanup_manifest_mismatch` | Preserve changed/new files after interrupted cleanup; restore intact evidence before retrying. |
| `draft_changed_during_cleanup`, `receipt_changed_during_cleanup` | Stop external edits and preserve changed evidence. Earlier candidates may already have been compacted; inspect before retrying. |
| `unverified_evidence`, orphan directories | Ownership or evidence cannot be established. Inspect privately; nothing authorizes automatic deletion. |

## Reproducible synthetic check

Run `node --test tests/test_journal_storage.mjs`. It tests dry-run non-mutation,
retention, interrupted cleanup/retry, writer exclusion, altered evidence,
historical verification and tar backup/restore. The benchmark uses six
deterministic obsolete 128-KiB historical versions and six successive ingestions
with acknowledgement retries. It prints allocated disk bytes, journal category
sizes and gzip backup size/time before/after compaction, and compares a
full-history clone with the new shallow clone. Timing varies by host and cache;
no private source data is used.

Example run on the development host (six imports, logical sizes depend slightly
on IDs and paths):

| Measure | Before compaction | After compaction |
| --- | ---: | ---: |
| Journal allocated disk | 1,937,408 B | 159,744 B |
| Journal gzip backup | 95,888 B / 14.0 ms | 6,409 B / 2.9 ms |
| Proposal Git / page payload | 166,043 / 4,878 B | 0 / 0 B |
| Draft evidence payload | 12,352 B | 0 B |
| Compact-parent receipt payload | 40,802 B | 36,408 B |
| Run/report payload | 14,259 B | 14,259 B |
| Live recovery-backup payload (not reclaimed) | 3,330 B | 3,330 B |

The comparison clone occupied 1,466,368 B with full history versus 278,528 B
with the shallow baseline. Whole-stack backup savings will be smaller because
the live wiki history and other data intentionally remain in the backup.
