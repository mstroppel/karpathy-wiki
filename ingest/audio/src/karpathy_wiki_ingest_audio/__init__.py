"""WebDAV audio ingest plugin: speech handoff, redaction, publication.

The connector synchronizes a WebDAV folder's audio files into one private
immutable snapshot, hands each recording through the replaceable
``karpathy_wiki_speech`` interface, caches structured speech results
privately by audio hash plus the full processing key, redacts transcripts
with the shared targeted anonymizer (including matches that span segment
boundaries), and publishes the sanitized Markdown as coherent generations
behind an atomic ``current`` symlink with a versioned provider manifest.

A failed cycle keeps the last successful generation active and reports the
failure content-free. This plugin writes raw audio and unredacted transcripts
only into the private input and cache directories; with the opt-in hosted
speech backend the worker additionally uploads staged recordings to its
transcription service before any redaction (see ``docs/audio.md``).
"""

from karpathy_wiki_ingest_audio.publisher import (
    Settings,
    main,
    run,
)

__all__ = ["Settings", "main", "run"]
