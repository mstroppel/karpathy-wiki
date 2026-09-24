# Paperless Ingestion

The optional importer selects Paperless-ngx documents by tag, reads OCR text and
selected metadata, replaces explicitly configured personal values locally, and
writes only sanitized Markdown into `/knowledge/sources/paperless`.

It does not download PDFs or images. It does not use an external redaction
service. Unknown personal values, unexpected spelling, and OCR variants may
remain, so this process must not be treated as guaranteed anonymization.

## Setup

1. Create a dedicated Paperless user with read access only to the selected
   documents.
2. Create a source tag and determine its positive numeric ID through the
   Paperless API.
3. Create an API token for the dedicated user.
4. Copy `redactions.example.json` to a private location and enter every known
   value and variant that must be replaced.
5. Store the token without a trailing newline and protect both files with mode
   `0600`.
6. Enable the profile and generated policy:

```env
COMPOSE_PROFILES=webdav,paperless
PAPERLESS_PUBLIC_URL=https://paperless.example.com
PAPERLESS_SOURCE_TAG_ID=123
PAPERLESS_TOKEN_FILE=/private/wiki/paperless-token
REDACTIONS_FILE=/private/wiki/redactions.json
```

Run a one-off synchronization before enabling continuous polling:

```bash
docker compose --profile paperless run --rm paperless-ingest paperless --once
```

Inspect sanitized output and the content-free error reports under
`${DATA_ROOT}/quarantine/paperless` locally. Do not send test documents to a
model provider until the review is complete.

## Revision Model

Published files contain `paperless_id`, `paperless_url`, and a deterministic
`source_revision`. Content or relevant redaction changes produce a new revision.
Documents are split into ID ranges of no more than 1000 items.

Each synchronization builds all sanitized documents and revocations in a new
private generation. The provider manifest points at the files under
`sources/paperless/generations/<id>/` rather than a mutable source path.
An atomic `current` symlink selects the active generation; the manifest is
replaced only after the complete generation is available. Failed fetches,
decoding, or writes leave the previous generation and manifest active.
Privacy validation failures are recorded as content-free errors and any
previously published copy is revoked in the new generation. The redaction file
is reloaded on each daemon cycle. Unchanged cycles retain the existing
generation; completed replacements retain only the active generation.

After an interrupted cycle, the next run removes abandoned `.staging-*`
directories. If the active pointer or manifest is corrupt, stop the importer,
inspect `current` and `manifest.json`, and restore a consistent pair from a
backup rather than changing individual files. The file `.generation.json`
contains only a redaction fingerprint, source revision hashes, the digest of
the manifest written for the generation, the source tag ID, and the configured
public URL; it does not contain source text or tokens.

**Existing pre-1.0 flat Paperless installations:** back up `DATA_ROOT`, stop the
Paperless service, and manually remove the old flat sanitized source files,
`revoked.md`, and `manifest.json` under `sources/paperless` before starting this
layout. The next cycle fetches and republishes the selected documents. Review
any previous revocations and wiki pages before re-importing. The importer
refuses a flat layout instead of silently mixing generations with old files.

### Durable cycles

With the Compose-provided `INGEST_STATE_PATH`, Paperless identifies each cycle
by the selected documents' revisions, the redaction fingerprint, source tag,
and public URL. It records a content-free accepted job in the shared SQLite
store before publishing. A lease with periodic renewal fences a worker that
loses authority; failed work backs off, and an unchanged failed input becomes
dead after repeated attempts. A restart checks the active manifest and
generation before retrying, so a cycle that published before its job completed
is recorded without a duplicate generation. A changed upstream snapshot
supersedes older pending jobs. Raw API documents are kept only in memory and
are never persisted in job payloads or health metrics.

The daemon's health record includes the shared store's queue depth, age,
failed/retried counts, last recorded source generation, and last committed
wiki publication. An unavailable or incompatible state store stops the
Paperless cycle instead of publishing without coordination.

The `paperless_url` frontmatter field is validated HTTPS and repeated as
trusted page frontmatter in the provider manifest, so every generated wiki
summary page can render a visible link to the Paperless original; the
`wiki-ingest` skill preserves the field and the link, and `wiki-lint` reports
Paperless pages that lose it.

Removing the source tag omits the document from the new generation and records
the ID in `current/revoked.md`. Existing derived wiki knowledge is reported but not automatically
deleted because it may be supported by additional sources.

After every cycle the plugin writes the provider manifest
`sources/paperless/manifest.json` describing the published documents, their
revisions, their wiki destinations, and any content-free errors.

Use `/ingest-new` in OpenCode to compare source revisions with existing wiki
pages and process new or changed documents sequentially.

## Network Boundary

Only `paperless-ingest` requires Paperless API access. Ensure the importer can
reach the Paperless-ngx API over the network it shares; do not attach OpenCode
to that network. OpenCode's policy also prohibits direct API access, and
`PAPERLESS_BLOCKED_HOSTNAME` maps the configured public hostname to loopback.
These application controls remain defense in depth rather than a replacement
for network isolation.
