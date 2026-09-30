"""Provider side of the speech worker handoff (concept step 2).

The audio provider stages the recording into the private recordings store,
submits a content-free request, and polls the shared cache for the
structured result. Neither raw audio nor unredacted transcripts are ever
written outside the speech root. The transcription interface stays
independent of WebDAV, so other providers (for example email ingest) can
submit attachments to the same worker.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from karpathy_wiki_speech.cache import (
    failure_path,
    load_cached_result,
    recording_path,
    request_id,
    request_path,
)
from karpathy_wiki_speech.types import (
    LimitExceededError,
    TranscriptionOptions,
    TranscriptionResult,
)


class TimeoutError(RuntimeError):
    """The worker did not produce a result before the cycle timeout."""


def requested_options(options: TranscriptionOptions) -> dict[str, object]:
    """The options that take part in the speech processing cache key."""
    request: dict[str, object] = {
        "language": options.language,
        "diarize": options.diarize,
    }
    return request


def result_identity(options: TranscriptionOptions) -> str:
    from karpathy_wiki_speech.cache import options_key as key_of

    return key_of(requested_options(options))


def load_result(
    speech_root: Path,
    audio_sha256: str,
    options: TranscriptionOptions,
) -> TranscriptionResult | None:
    """Latest structured result for the requested options, if complete."""

    return load_cached_result(speech_root, audio_sha256, requested_options(options))


def load_failure(
    speech_root: Path,
    audio_sha256: str,
    options: TranscriptionOptions,
) -> str | None:
    """Content-free worker failure for this recording + options, if any."""
    try:
        payload = json.loads(
            failure_path(speech_root, audio_sha256, result_identity(options)).read_text(
                encoding="utf-8"
            )
        )
    except (OSError, ValueError):
        return None
    if isinstance(payload, dict) and isinstance(payload.get("error"), str):
        return str(payload["error"])
    return None


def clear_failure(speech_root: Path, audio_sha256: str, options: TranscriptionOptions) -> None:
    failure_path(speech_root, audio_sha256, result_identity(options)).unlink(missing_ok=True)


def stage_recording(
    speech_root: Path,
    snapshot_file: Path,
    audio_sha256: str,
    extension: str,
    max_bytes: int,
) -> str:
    """Copy the snapshot file into the immutable recordings store.

    Recording copies are content-addressed by hash, so re-syncing the same
    upstream audio is idempotent and the worker never reads a file rclone
    may still be updating. ``ValueError`` when a limit is violated.
    """
    file_bytes = snapshot_file.stat().st_size
    if file_bytes > max_bytes:
        raise LimitExceededError("recording exceeds the configured size limit")
    destination = recording_path(speech_root, audio_sha256, extension)
    if destination.is_file():
        digest = _sha256(destination)
        if digest != audio_sha256:
            destination.unlink(missing_ok=True)
        else:
            return destination.relative_to(speech_root).as_posix()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=f".{destination.stem}.", delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        try:
            # A hard link keeps identical recordings cheap; snapshot changes
            # replace the source inode, so the copy stays immutable content.
            os.link(snapshot_file, temporary)
        except OSError:
            shutil.copyfile(snapshot_file, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination.relative_to(speech_root).as_posix()


def _sha256(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def ensure_requested(
    speech_root: Path,
    audio_sha256: str,
    options: TranscriptionOptions,
    audio_reference: str,
) -> None:
    """Submit one content-free request when no result or failure exists."""
    identifier = request_id(audio_sha256, result_identity(options))
    destination = request_path(speech_root, identifier)
    if destination.exists():
        return
    if load_result(speech_root, audio_sha256, options) is not None:
        return
    if load_failure(speech_root, audio_sha256, options) is not None:
        return
    (speech_root / "requests").mkdir(parents=True, exist_ok=True)
    payload = {
        "id": identifier,
        "audio": audio_reference,
        "audio_sha256": audio_sha256,
        "options": requested_options(options),
    }
    atomic_json(destination, payload)


def atomic_json(path: Path, payload: object) -> None:
    from karpathy_wiki_ingest.shared import atomic_write

    atomic_write(path, json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", mode=0o600)


def await_result(
    speech_root: Path,
    audio_sha256: str,
    options: TranscriptionOptions,
    timeout_seconds: int,
    poll_seconds: int = 5,
) -> TranscriptionResult:
    """Poll for the result or content-free failure within the timeout.

    Returns the parsed result, raises :class:`SpeechTimeoutError` when the
    worker does not reply, and re-raises the asynchronous failure type for
    content-free failure reports so the caller quarantines the source.
    """
    deadline = time.monotonic() + max(timeout_seconds, 0)
    while True:
        failure = load_failure(speech_root, audio_sha256, options)
        if failure is not None:
            raise SpeechProcessingError(failure)
        result = load_result(speech_root, audio_sha256, options)
        if result is not None:
            clear_failure(speech_root, audio_sha256, options)
            return result
        if time.monotonic() >= deadline:
            raise SpeechTimeoutError("the speech worker did not answer in time")
        time.sleep(max(poll_seconds, 1))


class SpeechProcessingError(ValueError):
    """A content-free worker failure; the message names the error type only."""


class SpeechTimeoutError(RuntimeError):
    """The worker did not answer in time; the message carries no content."""
