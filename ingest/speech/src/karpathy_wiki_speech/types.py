"""Structured speech results and bounded input decoding.

Segments carry the timing, text, and provenance the audio provider renders
into Markdown. :func:`result_identity` keys the private speech-result cache:
identical audio plus identical backend, model, device, compute type, and
options reproduce the same structured result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

# Input bounds. Recordings above them are rejected content-free; values are
# configurable so slow hosts can lower the duration instead of timing out.
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_MAX_DURATION_SECONDS = 4 * 3600
DEFAULT_ALLOWED_EXTENSIONS: tuple[str, ...] = (
    ".flac",
    ".m4a",
    ".mp3",
    ".mp4",
    ".ogg",
    ".opus",
    ".wav",
    ".webm",
)

SPEECH_FORMAT_VERSION = 1


class LimitExceededError(ValueError):
    """The input violates a configured content-free limit."""


@dataclass(frozen=True)
class TranscriptionLimits:
    max_bytes: int
    max_duration_seconds: int
    allowed_extensions: tuple[str, ...]

    def describe(self) -> dict[str, object]:
        return {
            "max_bytes": self.max_bytes,
            "max_duration_seconds": self.max_duration_seconds,
            "allowed_extensions": sorted(self.allowed_extensions),
        }

    def validate_path(self, path: str) -> None:
        extension = path[path.rfind(".") :].lower() if "." in path else ""
        if extension not in self.allowed_extensions:
            raise LimitExceededError("unsupported audio file extension")
        if not self.max_bytes or self.max_bytes <= 0:
            raise LimitExceededError("max_bytes must be positive")
        if not self.max_duration_seconds or self.max_duration_seconds <= 0:
            raise LimitExceededError("max_duration_seconds must be positive")


DEFAULT_LIMITS = TranscriptionLimits(
    max_bytes=DEFAULT_MAX_BYTES,
    max_duration_seconds=DEFAULT_MAX_DURATION_SECONDS,
    allowed_extensions=DEFAULT_ALLOWED_EXTENSIONS,
)


@dataclass(frozen=True)
class TranscriptionOptions:
    """Every knob that changes speech output; all of it enters the cache key."""

    language: str | None = None
    model: str | None = None
    compute_type: str | None = None
    device: str | None = None
    diarize: bool = False


@dataclass(frozen=True)
class Segment:
    """One timed transcript segment; ``speaker_id`` is an opaque cluster id."""

    start_ms: int
    end_ms: int
    text: str
    speaker_id: int | None = None

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "start_ms": self.start_ms,
            "end_ms": self.end_ms,
            "text": self.text,
        }
        if self.speaker_id is not None:
            payload["speaker_id"] = self.speaker_id
        return payload


@dataclass(frozen=True)
class TranscriptionResult:
    """Backend output plus the backend/model metadata recorded as provenance."""

    segments: list[Segment]
    language: str | None
    backend: str
    model: str
    options: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class DecodedAudio:
    """Decoded audio facts of one input file; format only, never content."""

    duration_seconds: float
    input_bytes: int
    format_name: str


def accept_audio(path: str, limits: TranscriptionLimits) -> int:
    """Validate one audio file against the extension and size limits.

    Returns the file size; content-free errors (the limit that rejected the
    file) never carry audio content. Decoding/probing happens in
    :func:`decode_audio` which additionally requires ffprobe.
    """
    limits.validate_path(path)
    try:
        input_bytes = size_of(path)
    except OSError as error:
        raise LimitExceededError("audio file is not readable") from error
    if input_bytes > limits.max_bytes:
        raise LimitExceededError("audio file exceeds the configured size limit")
    return input_bytes


def decode_audio(path: str, limits: TranscriptionLimits) -> DecodedAudio:
    """Probe one audio file with PyAV and enforce the duration limit.

    PyAV ships bundled FFmpeg libraries, so decoding needs no system
    packages and no external binary. Errors surface content-free.
    """
    input_bytes = accept_audio(path, limits)
    try:
        import av  # type: ignore[import-not-found]

        container = av.open(path, mode="r")
    except Exception as error:  # noqa: BLE001 - ffmpeg errors are opaque
        raise LimitExceededError("audio file could not be decoded") from error
    try:
        if container.duration is None or container.duration <= 0:
            raise LimitExceededError("audio file has no decodable duration")
        duration_seconds = container.duration / float(av.time_base)
        format_name = container.format.name if container.format is not None else "unknown"
        if not container.streams.audio:
            raise LimitExceededError("audio file has no audio stream")
    finally:
        container.close()
    if duration_seconds <= 0 or duration_seconds > limits.max_duration_seconds:
        raise LimitExceededError("audio file exceeds the configured duration limit")
    return DecodedAudio(
        duration_seconds=duration_seconds,
        input_bytes=input_bytes,
        format_name=format_name,
    )


def size_of(path: str) -> int:
    from pathlib import Path

    return Path(path).stat().st_size


def file_revision(path: str) -> str:
    """SHA-256 of a file, streamed; canonical content identity for audio."""
    import hashlib
    from pathlib import Path

    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def result_identity(
    audio_sha256: str,
    options: TranscriptionOptions,
    result: TranscriptionResult,
) -> str:
    """Cache identity of a speech result: audio hash plus full processing key.

    The backend and model versions come from the result itself, so a worker
    image upgrade automatically changes the identity of every result it
    produces from then on.
    """
    payload = {
        "format_version": SPEECH_FORMAT_VERSION,
        "audio_sha256": audio_sha256,
        "options": {
            "diarize": options.diarize,
            "language": options.language,
            "model": options.model,
            "compute_type": options.compute_type,
            "device": options.device,
        },
        "backend": result.backend,
        "model": result.model,
        "segments": [segment.to_dict() for segment in result.segments],
        "result_language": result.language,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
