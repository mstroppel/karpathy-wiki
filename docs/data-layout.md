# Data Layout

All persistent data lives below `DATA_ROOT`. The installation directory only
contains the launcher, `.env`, optional secrets, and local Compose adoptions.

```text
${DATA_ROOT}/
├── sources/
│   ├── webdav/             # Published WebDAV generations (current + generations/)
│   ├── audio/              # Published audio transcripts (current + generations/)
│   ├── answers/            # Published locally redacted Q&A source revisions
│   └── paperless/          # Published Paperless generations (current + generations/)
├── wiki/                   # SilverBullet space and independent Git repository
├── incoming/
│   ├── audio/              # Private rclone snapshot of WebDAV recordings
│   ├── answers/            # Confirmed Q&A drafts, private to OpenCode and answer provider
│   └── ingest-journal/     # Private wiki ingestion journal: per-source result
│       └── runs/<run-id>/  #   records, run state, and assembled reports
├── speech/                 # Speech worker store: recordings, queue, failures,
│   └── cache/              #   and structured results (unredacted, private)
├── models/
│   └── audio/              # Persistent speech model cache
├── state/
│   ├── ingest.sqlite3      # Durable ingest jobs, leases, generations, publications
│   └── audio-identity/     # Private WebDAV path → opaque source ID mapping
├── quarantine/
│   ├── webdav/             # Content-free WebDAV error reports
│   ├── audio/              # Content-free audio error reports
│   └── paperless/          # Content-free Paperless error reports
├── openchamber/             # Private chat UI settings (not backend sessions)
├── publisher/               # Trusted queue/ownership/intents; optional private manual-controller state
└── opencode/
    ├── config/             # OpenCode configuration and generated policy
    ├── data/               # Credentials, sessions, messages, and logs
    └── state/              # OpenCode runtime state
```

Source directories are read-only inside OpenCode. Generated knowledge is written
only to `wiki/`; successful changes create focused Conventional Commits in that
independent repository. Source providers write a manifest per cycle that tracks
normalized paths and SHA-256
revisions so changed sources can be ingested without silently deleting knowledge
when a source is removed.

Use separate `DATA_ROOT` directories for separate installations. Absolute paths
are recommended in production, for example:

```env
DATA_ROOT=/srv/karpathy-wiki/personal
```

Secret files can live outside `DATA_ROOT` through `PAPERLESS_TOKEN_FILE` and
`REDACTIONS_FILE`. Do not store credentials, source material, wiki content, or
sessions in this repository.

## WebDAV source generations

WebDAV publishes each synchronization as a coherent generation below
`sources/webdav`: sanitized files live in `generations/<id>/`, the `current`
symlink points at the active generation, and `manifest.json` describes it.
The generation is exposed only after the complete sanitized tree exists, so
readers never observe a partially published cycle and the last successful
generation stays active after any failure. See
[configuration](configuration.md#source-generations) for the publication
model, retention, and recovery.

## Audio recordings and the speech worker

Audio transcripts publish the same generation model below `sources/audio`
(`wiki_root: audio`). Each recording keeps one persistent opaque source ID
mapped from its WebDAV path in `state/audio-identity` (private provider
storage): renames revoke the old source, identical audio at different paths
stays distinct, and redaction changes never change identity. Raw audio
(`incoming/audio`), immutable worker copies, the unredacted speech-result
cache, and model weights live under `incoming/audio`, `speech/`, and
`models/audio`; those directories are private to the ingest and speech
containers and never mounted into OpenCode. See
[audio ingest](audio.md) for the processing options and retention.

## Durable ingest state

The ingest services record accepted work in a small embedded SQLite state
store below `${DATA_ROOT}/state`: ingest jobs with state transitions, attempts,
and leases, the immutable source generations they published, and wiki
publications. The store is content-free: it records identifiers, revisions,
counts, and content-free error strings, never source or wiki content. It
contains no credentials.

The store makes ingest work recoverable and idempotent: a restart neither
loses accepted work nor re-executes an accepted cycle whose publication is
already active, interrupted cycles are recovered through lease expiry, and
failed work backs off instead of retrying every synchronization interval.
Jobs whose rejected input is unchanged stay dead and keep the failure visible
until the input changes or the job is rearmed explicitly. Deleting the
database file resets only this bookkeeping: the next ingest cycle re-records
the active generation and re-coordinates from there, and republishes a
generation when the published one no longer matches the upstream content.
Include the file in backups of
`DATA_ROOT`; it is recreated automatically when missing.

The state store's `metrics()` snapshot is included in both ingest health
records: pending queue depth and oldest age, failed and retried job counts, the
last recorded source generation, and the last completed wiki publication (if
any). A publication is counted as completed only when its Git commit is
recorded. Publisher jobs have a database-enforced exclusive lease: two
distinct publish jobs cannot hold live leases simultaneously, and an expired
holder cannot renew or complete its job. The serialized publisher that will
consume these leases is still planned; the operator-only trusted publisher uses
separate non-expiring admission state. These metrics do not imply automatic
wiki publication.

## Wiki ingestion journal

Bulk wiki ingestion records its per-source results in
`incoming/ingest-journal/runs/<run-id>/`: append-only result records, the run
state with its working-context budget, and the assembled per-source report.
`incoming/ingest-journal/preparations/<preparation-id>.json` holds private
transaction receipts (`0600`): selected source identity, a separate source-byte
SHA-256, baseline commit and file hashes, and the exact validated changed-page hashes. Include these receipts
in journal backups so pending commits remain verifiable.
Receipts also track transaction-owned page hashes, retained-backup hashes, pending
publication intent and resumable rollback progress (`rollback_started`,
`rollback_pending`, `rolled_back`). Run metadata records confirmed skipped failure
indices; the original blocked result records remain in the journal and report.
Each preparation now also owns `preparations/<preparation-id>/wiki/`, an isolated
Git clone containing wiki history and proposed changes, and `/journal/`, its
draft transaction evidence. Receipts retain section references, reading/proposal
budgets, publication phase, intended commit/report payload and owned index-lock
identity. Include the entire preparation directory in private backups; clones
cost disk proportional to history and processed sources. They are not served.
The model-facing publisher uses a stable `wiki/.git/wiki-ingest-publication.lock`
inode with a kernel lock released on process exit, rather than stale-file removal.
Run audit writes similarly use `runs/<run-id>/records.lock`; complete audit,
run and report files replace private files atomically. An unverifiable
`wiki/.git/index.lock` requires confirmed maintenance; only a proven owned lock
can be completed during resume. There is no automatic clone/backup cleanup.
Wiki pages displaced during publication or confirmed rollback retain their original inode at
`wiki/.git/ingest-backup-<random-id>/page`, inside an exclusive `0700` directory.
These private recovery files are outside Git's content tree and public wiki pages;
include `.git` in wiki backups. They are never automatically removed, since an
external writer may still hold a descriptor to the displaced inode. Inspect and
clean them only during explicitly confirmed maintenance with writers stopped.
These records hold source-derived summaries of processed sources, so they live
in the private `incoming/` area alongside the answer drafts instead of the
content-free state store. The directory is private to OpenCode and its operator
(`0700`), only `incoming/ingest-journal` is mounted into the OpenCode container
(`/knowledge/incoming/ingest-journal`), and nothing from it is published to the
wiki, to source directories, or into fixtures and diagnostics. Include it in
backups of `DATA_ROOT` to keep completed reports. See
[ingestion reports](ingest-reports.md) for the record schema, resume behavior,
and the report contract.

## Removed data directories

The session PDF export was removed. Existing installations can manually delete
the now-unused `${DATA_ROOT}/exports/sessions` directory; per repository policy,
no automatic data-layout moves are performed before version 1.0.
