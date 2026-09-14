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
COMPOSE_PROFILES=nextcloud,paperless
PAPERLESS_ENABLED=true
PAPERLESS_API_URL=http://paperless:8000
PAPERLESS_PUBLIC_URL=https://paperless.example.com
PAPERLESS_SOURCE_TAG_ID=123
PAPERLESS_TOKEN_FILE=/private/wiki/paperless-token
REDACTIONS_FILE=/private/wiki/redactions.json
PAPERLESS_NETWORK=paperless-backend
```

Run a one-off synchronization before enabling continuous polling:

```bash
docker compose --profile paperless run --rm paperless-ingest --once
```

Inspect sanitized output and quarantine metadata locally. Do not send test
documents to a model provider until the review is complete.

## Revision Model

Published files contain `paperless_id`, `paperless_url`, and a deterministic
`source_revision`. Content or relevant redaction changes produce a new revision.
Documents are split into ID ranges of no more than 1000 items.

Removing the source tag deletes the sanitized source and records the ID in
`revoked.md`. Existing derived wiki knowledge is reported but not automatically
deleted because it may be supported by additional sources.

Use `/ingest-new` in OpenCode to compare source revisions with existing wiki
pages and process new or changed documents sequentially.

## Network Boundary

Only `paperless-ingest` requires Paperless API access. Attach Paperless-ngx and
the importer to the external network configured by `PAPERLESS_NETWORK`; do not
attach OpenCode to that network. OpenCode's policy also prohibits direct API
access, and `PAPERLESS_BLOCKED_HOSTNAME` maps the configured public hostname to
loopback. These application controls remain defense in depth rather than a
replacement for network isolation.
