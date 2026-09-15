# OpenCode Session Export

The optional exporter polls the OpenCode API and creates PDFs for inactive root
sessions. PDFs contain visible user and assistant messages plus attachment names,
but exclude tool calls, reasoning, and internal metadata.

Exports use this layout:

```text
${DATA_ROOT}/exports/sessions/YYYY/MM/DD/YYYY-MM-DD Session title.pdf
```

Changed sessions are regenerated. Deleted sessions are removed from the mirror.
The destination must therefore be dedicated to this exporter because `rclone
sync` removes files that do not exist locally.

`${DATA_ROOT}/exports/sessions/.export-state.json` tracks exported revisions
locally and is excluded from the Nextcloud mirror.

## Configuration

```env
COMPOSE_PROFILES=webdav,session-export
OPENCODE_PUBLIC_URL=https://chat.example.com
WEBDAV_SESSION_PATH=OpenCode Sessions
SESSION_EXPORT_INTERVAL=15m
```

The exporter reuses the configured WebDAV credentials by default. Set the
corresponding `NEXTCLOUD_*` variables when it should use a separate Nextcloud
account or endpoint. Exported conversations remain confidential even though
internal tool details are omitted.

## Raw One-Off Export

Export one session and all recursive child sessions as JSON:

```bash
./session-export/export-session.sh SESSION_ID
```

The command requires Docker and `jq` and writes to the ignored
`session-export/output/` directory by default. Set `SESSION_EXPORT_OUTPUT_DIR`
to store it elsewhere.
