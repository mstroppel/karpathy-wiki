# karpathy-wiki-ingest-webdav

WebDAV ingest plugin for Karpathy Wiki. It synchronizes a WebDAV directory
with rclone, anonymizes every file with the shared targeted anonymizer, and
writes only sanitized content into the sanitized tree.

Depends on the `karpathy-wiki-ingest` core distribution. Run it through the
core dispatcher (`python -m karpathy_wiki_ingest webdav`) or directly
(`python -m karpathy_wiki_ingest_webdav`). See `docs/ingest-modules.md` and
the versioned status contract in `contracts/ingest-status/`.
