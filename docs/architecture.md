# Architecture

The core services are `init`, `opencode`, `openchamber`, and `silverbullet`. Optional source
and export services are enabled with Compose profiles. The `webdav-ingest`,
`audio-ingest`, `paperless-ingest`, and `email-ingest` services use dedicated images
(`karpathy-wiki-ingest-webdav`, `karpathy-wiki-ingest-audio`,
`karpathy-wiki-ingest-paperless`, and `karpathy-wiki-ingest-email`) that contain
only the shared ingest core and their own plugin; audio additionally uses the
separate local speech worker image (`karpathy-wiki-ingest-speech`). Ingest is
plugin-based; additional modules can live in separate repositories (see
`ingest-modules.md`).

```text
WebDAV ------- rclone -> local redaction -> sources/webdav/ ------+
                          (coherent generations,                  |
                           atomic current switch)                 |
WebDAV audio -- rclone snapshot -> local speech worker ->         |
                  local redaction -> sources/audio/ --------------+
                                        (coherent generations)     |
Paperless ---- local redaction (optional) -> sources/paperless/ --+--> OpenCode
                                        (coherent generations)      |
Confirmed Q&A -> local inbox -> local redaction -> sources/answers/ -+
TLS IMAP ---- private original archive + ledger -> targeted delete  |
                -> local body/header extraction -> local redaction  |
                -> sources/email/ (coherent generations) -----------+
                                                                        |
                                                                        v
                                                                   wiki/ + Git
                                                                        |
                                                                        v
                                                                   SilverBullet
```

The opt-in `email` profile keeps complete, unredacted `.eml` originals (including
attachments) and its durable ledger in `${DATA_ROOT}/incoming/email`, mounted
only into `email-ingest`. Verified archive and ledger commits precede targeted
server deletion; extraction and redaction follow locally. Only redacted body
text and headers cross the atomic manifest boundary into `sources/email/` for
OpenCode. Originals, mailbox credentials and attachments are not exposed to
OpenCode, SilverBullet or `raw-files`. Offline replay uses the private archive
without contacting IMAP; wiki ingestion still requires explicit `/ingest-new`.
See [email ingestion](email.md) for trust, deletion and retention boundaries.

OpenChamber exposes the private OpenCode backend's sessions and wiki commands;
sessions are not exported to SilverBullet. The frontend has a separate private
settings directory, a read-only wiki mount and read-only ingest reports.
See [chat deployment](chat.md).

OpenCode reads source directories and writes generated Markdown to `wiki/`.
Writable ingestion uses one lifetime lock over the selected backlog, sequential
fresh workers, one Git commit per source and a private report.
Readers may see intermediate edits; other manual/agent writers must stay idle.
Failures retain work for manual review instead of per-page transaction recovery.
See [ingestion reports](ingest-reports.md).
In opt-in reader mode it cannot write protected data. There is no alternate
publisher/controller route. Automatic wiki ingestion is deferred until after 1.0.
Source-provider acquisition and its SQLite leases remain independent.
SilverBullet serves the wiki space from a read-only mount. Services do not publish host ports; the
reverse proxy reaches them through `WEBPROXY_NETWORK` using these aliases:

```text
${STACK_ID}-silverbullet:3000
${STACK_ID}-openchamber:3000
${STACK_ID}-raw-files:8080
```

`COMPOSE_PROJECT_NAME` separates containers and private networks. `STACK_ID`
provides unique aliases on the shared proxy network, allowing multiple
installations to run independently with different environment files and data
roots. Only trusted proxy infrastructure should join `WEBPROXY_NETWORK`.

Source acquisition is coordinated by a durable, content-free SQLite state store
below `${DATA_ROOT}/state` (`state.py` in the ingest core): jobs, attempts, leases
and immutable source generations. WebDAV and Paperless record accepted cycles
and published source generations. Wiki authoring instead uses the explicit
ingest request, lifetime lock and Git commits described above. The
experimental analysis UI is not part of this stack. It can be added later
without changing the source and wiki contracts. Saved analyses and their
printable HTML views live inside the wiki space (see
[analysis export](analysis-export.md)), so SilverBullet alone serves and
prints them; no export service is involved.
