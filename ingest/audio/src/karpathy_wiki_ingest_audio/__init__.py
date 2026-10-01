"""WebDAV audio ingest plugin: local transcription, redaction, publication.

The connector synchronizes a WebDAV folder's audio files into one private
immutable snapshot, hands each recording through the replaceable
``karpathy_wiki_speech`` interface, caches structured speech results
privately by audio hash plus the full processing key, redacts transcripts
with the shared targeted anonymizer (including matches that span segment
boundaries), and publishes the sanitized Markdown as coherent generations
behind an atomic ``current`` symlink with a versioned provider manifest.

A failed cycle keeps the last successful generation active and reports the
failure content-free. Raw audio and unredacted transcripts never leave the
private input and cache directories.
"""

from karpathy_wiki_ingest_audio.publisher import (
    Settings,
    main,
    run,
)

__all__ = ["Settings", "main", "run"]
