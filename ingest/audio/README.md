# karpathy-wiki-ingest-audio

WebDAV audio ingest plugin for Karpathy Wiki (#15). It discovers audio files
in a configured WebDAV folder, synchronizes only supported audio extensions
into a private snapshot with rclone (recursively and case-insensitively),
transcribes locally through the
`karpathy-wiki-speech` interface, redacts every transcript with the shared
targeted anonymizer, and publishes sanitized Markdown sources as coherent
generations with the versioned provider manifest (`sources/audio`,
`wiki_root: audio`).

The sync filter uses the same extension allowlist as audio discovery:
`.flac`, `.m4a`, `.mp3`, `.mp4`, `.ogg`, `.opus`, `.wav`, and `.webm`.
Excluded files already in the local snapshot are left untouched; synchronization
does not use `--delete-excluded`.

Raw audio, decoded audio, and unredacted transcripts never enter the
published tree, model prompts, wiki assets, or content-bearing logs. Source
identity comes from a persistent opaque mapping of WebDAV paths; renaming a
recording revokes the old source and creates a new one. Identical audio at
two paths keeps two distinct sources.

Depends on the `karpathy-wiki-ingest` core distribution and the
`karpathy-wiki-speech` speech interface. Run it through the core dispatcher
(`python -m karpathy_wiki_ingest audio`) or directly
(`python -m karpathy_wiki_ingest_audio`). See `docs/ingest-modules.md`,
`docs/audio-ingest-concept.md`, and the versioned status contract in
`contracts/ingest-status/`.
