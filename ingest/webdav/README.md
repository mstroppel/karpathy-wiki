# karpathy-wiki-ingest-webdav

WebDAV ingest plugin for Karpathy Wiki. It synchronizes a WebDAV directory
with rclone, redacts UTF-8 `.md`, `.html`, and `.htm` files locally, and writes
only redacted content into the sanitized tree. HTML remains HTML under its
original filename: entity-aware and cross-tag matches are replaced in the
original source without conversion or serialization. Unsupported formats are
ignored; rejected files produce content-free quarantine reports and prevent
publication of the candidate generation.

HTML redaction is not HTML sanitization. Active content is retained, never
executed, and no linked resources are fetched. Treat the output as untrusted
source text, not as safe-to-render pages. See `docs/configuration.md` for matching
coverage and limitations.

Depends on the `karpathy-wiki-ingest` core distribution. Run it through the
core dispatcher (`python -m karpathy_wiki_ingest webdav`) or directly
(`python -m karpathy_wiki_ingest_webdav`). See `docs/ingest-modules.md` and
the versioned status contract in `contracts/ingest-status/`.
