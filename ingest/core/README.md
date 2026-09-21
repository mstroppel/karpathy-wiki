# karpathy-wiki-ingest (core)

Shared core for Karpathy-Wiki ingest plugins. It provides the targeted
anonymizer, atomic writes, the health record helpers, and the plugin
dispatcher (`python -m karpathy_wiki_ingest PLUGIN`). It ships no plugins of
its own; the built-in WebDAV and Paperless plugins are separate distributions
(`karpathy-wiki-ingest-webdav`, `karpathy-wiki-ingest-paperless`) that depend
on this core and register an entry point in the
`karpathy_wiki_ingest.plugins` group.

See `docs/ingest-modules.md` in the repository and the versioned status
contract in `contracts/ingest-status/`.
