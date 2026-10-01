"""Timed speech processing: replaceable backends, one interface.

A backend receives an audio file path and a
:class:`karpathy_wiki_speech.TranscriptionOptions` object and returns timed
:class:`karpathy_wiki_speech.Segment` objects plus backend metadata. It never
receives provider identity, titles, WebDAV paths, or redaction configuration,
so the same interface serves any ingest provider that holds consented audio.

The package ships two implementations:

- :class:`karpathy_wiki_speech.FakeBackend` - deterministic segments for
  tests and CI; also a template for third-party backends.
- :class:`karpathy_wiki_speech.FasterWhisperBackend` - local faster-whisper
  transcription with optional pyannote.audio diarization (installed through
  the ``whisper``/``diarize`` extras).

Backends must never log or raise with transcript or audio content; a failure
surfaces as the exception type only.
"""

from karpathy_wiki_speech.backends import (
    BackendUnavailableError,
    FakeBackend,
    FasterWhisperBackend,
    SpeechBackend,
)
from karpathy_wiki_speech.types import (
    DEFAULT_LIMITS,
    DecodedAudio,
    LimitExceededError,
    Segment,
    TranscriptionLimits,
    TranscriptionOptions,
    TranscriptionResult,
    accept_audio,
    decode_audio,
    result_identity,
)

__all__ = [
    "DEFAULT_LIMITS",
    "BackendUnavailableError",
    "DecodedAudio",
    "FakeBackend",
    "FasterWhisperBackend",
    "LimitExceededError",
    "Segment",
    "SpeechBackend",
    "TranscriptionLimits",
    "TranscriptionOptions",
    "TranscriptionResult",
    "accept_audio",
    "decode_audio",
    "result_identity",
]
