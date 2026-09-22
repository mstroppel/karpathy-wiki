# Data Layout

All persistent data lives below `DATA_ROOT`. The installation directory only
contains the launcher, `.env`, optional secrets, and local Compose adoptions.

```text
${DATA_ROOT}/
├── sources/
│   ├── webdav/             # Published WebDAV generations (current + generations/)
│   └── paperless/          # Sanitized Paperless source files
├── wiki/                   # SilverBullet space and independent Git repository
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

## Removed data directories

The session PDF export was removed. Existing installations can manually delete
the now-unused `${DATA_ROOT}/exports/sessions` directory; per repository policy,
no automatic data-layout moves are performed before version 1.0.
