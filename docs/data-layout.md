# Data Layout

All persistent data lives below `DATA_ROOT`. The installation directory only
contains the launcher, `.env`, optional secrets, and local Compose adoptions.

```text
${DATA_ROOT}/
├── sources/
│   ├── webdav/             # Published WebDAV generations (current + generations/)
│   └── paperless/          # Sanitized Paperless source files
├── wiki/                   # SilverBullet space and independent Git repository
├── state/
│   └── ingest.sqlite3      # Durable ingest jobs, leases, generations, publications
├── quarantine/
│   ├── webdav/             # Content-free WebDAV error reports
│   └── paperless/          # Content-free Paperless error reports
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

The state store's `metrics()` snapshot is also included in the WebDAV health
record: pending queue depth and oldest age, failed and retried job counts, the
last recorded source generation, and the last completed wiki publication (if
any). A publication is counted as completed only when its Git commit is
recorded. Publisher jobs have a database-enforced exclusive lease: two
distinct publish jobs cannot hold live leases simultaneously, and an expired
holder cannot renew or complete its job. The serialized publisher that will
consume these leases is still planned; these metrics do not imply automatic
wiki publication.

## Removed data directories

The session PDF export was removed. Existing installations can manually delete
the now-unused `${DATA_ROOT}/exports/sessions` directory; per repository policy,
no automatic data-layout moves are performed before version 1.0.
