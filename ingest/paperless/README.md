# karpathy-wiki-ingest-paperless

Paperless-ngx ingest plugin for Karpathy Wiki. It selects documents by tag
through the Paperless API, replaces explicitly configured personal values
locally, and writes only sanitized Markdown into the sanitized tree.

Depends on the `karpathy-wiki-ingest` core distribution. Run it through the
core dispatcher (`python -m karpathy_wiki_ingest paperless`) or directly
(`python -m karpathy_wiki_ingest_paperless`). See `docs/paperless.md`,
`docs/ingest-modules.md`, and the versioned status contract in
`contracts/ingest-status/`.
