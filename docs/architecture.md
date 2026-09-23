# Architecture

The core services are `init`, `opencode`, and `silverbullet`. Optional source
and export services are enabled with Compose profiles. The `webdav-ingest` and
`paperless-ingest` services use dedicated images (`karpathy-wiki-ingest-webdav`
and `karpathy-wiki-ingest-paperless`) that contain only the shared ingest core
and their own plugin. Ingest is
plugin-based; additional modules can live in separate repositories (see
`ingest-modules.md`).

```text
WebDAV ------- rclone -> local redaction -> sources/webdav/ ------+
                          (coherent generations,                  |
                           atomic current switch)                 |
Paperless ---- local redaction (optional) -> sources/paperless/ --+--> OpenCode
                                        (coherent generations)      |
                                                                        |
                                                                        v
                                                                   wiki/ + Git
                                                                        |
                                                                        v
                                                                   SilverBullet
```

OpenCode sessions remain directly accessible in OpenCode; they are no longer
exported.

OpenCode reads source directories and writes generated Markdown to `wiki/`.
SilverBullet serves the wiki space from a read-only mount. Services do not publish host ports; the
reverse proxy reaches them through `WEBPROXY_NETWORK` using these aliases:

```text
${STACK_ID}-silverbullet:3000
${STACK_ID}-opencode:4096
${STACK_ID}-raw-files:8080
```

`COMPOSE_PROJECT_NAME` separates containers and private networks. `STACK_ID`
provides unique aliases on the shared proxy network, allowing multiple
installations to run independently with different environment files and data
roots. Only trusted proxy infrastructure should join `WEBPROXY_NETWORK`.

Ingest work is coordinated by a durable, content-free SQLite state store
below `${DATA_ROOT}/state` (`state.py` in the ingest core): ingest jobs with
state transitions, attempts, and leases, the immutable source generations,
and wiki publications. WebDAV and Paperless both record accepted cycles and
published source generations. Git remains the human-readable audit history, but no
longer carries job state or concurrency control; a restart neither loses
accepted ingest work nor executes an accepted publication twice. The
experimental analysis UI is not part of this stack. It can be added later
without changing the source and wiki contracts. Saved analyses and their
printable HTML views live inside the wiki space (see
[analysis export](analysis-export.md)), so SilverBullet alone serves and
prints them; no export service is involved.
