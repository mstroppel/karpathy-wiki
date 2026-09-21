# Data Layout

All persistent data lives below `DATA_ROOT`. The installation directory only
contains the launcher, `.env`, optional secrets, and local Compose adoptions.

```text
${DATA_ROOT}/
├── sources/
│   ├── webdav/             # Sanitized WebDAV source files
│   └── paperless/          # Sanitized Paperless source files
├── wiki/                   # SilverBullet space and independent Git repository
├── quarantine/
│   ├── webdav/             # WebDAV files that could not be processed
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

## Removed data directories

The session PDF export was removed. Existing installations can manually delete
the now-unused `${DATA_ROOT}/exports/sessions` directory; per repository policy,
no automatic data-layout moves are performed before version 1.0.
