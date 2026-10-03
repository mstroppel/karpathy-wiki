"""Speech worker: consumes audio paths + options, produces structured results.

The queue is a private directory shared with the ingest providers. Each
cycle the worker processes every queued request: verify the recording
against its claimed hash, decode with PyAV (bundled FFmpeg), transcribe (or
replay the newest cached result for the same options), write the structured
unredacted result, and delete the request. A worker crash never loses work:
requests remain queued until their results exist in the cache.

Failure reports are content-free: failures carry the error type only,
never audio or transcript content.

Provider-requested options (language, diarize) decide the cache key; worker
configuration (backend, model, device, compute type) is stamped into every
result, so a changed worker image supersedes older results for otherwise
identical requests without invalidating shared caches. The backend is
explicitly selected (``SPEECH_BACKEND``): local transcription with
faster-whisper by default, the opt-in hosted Mistral backend, or the
deterministic fake backend for tests and CI.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from karpathy_wiki_speech.backends import FakeBackend, FasterWhisperBackend, SpeechBackend
from karpathy_wiki_speech.mistral import MistralBackend
from karpathy_wiki_speech.types import (
    DecodedAudio,
    TranscriptionOptions,
    TranscriptionResult,
    file_revision,
)

LOG = logging.getLogger("karpathy-wiki-speech")


def backend_name() -> str:
    return os.getenv("SPEECH_BACKEND", "faster-whisper").strip().lower()


def worker_backend() -> SpeechBackend:
    name = backend_name()
    if name == "fake":
        return FakeBackend()
    if name == "faster-whisper":
        return FasterWhisperBackend()
    if name == "mistral":
        # Opt-in hosted transcription: raw audio leaves the host before any
        # redaction (see docs/audio.md). The API key stays worker-only.
        return MistralBackend.from_env()
    raise ValueError(f"unsupported speech backend: {name}")


def worker_pipeline_options() -> TranscriptionOptions:
    """Worker-side processing configuration (never provider-specified)."""
    if backend_name() == "mistral":
        return TranscriptionOptions(model=os.getenv("SPEECH_MISTRAL_MODEL", "").strip() or None)
    return TranscriptionOptions(
        model=os.getenv("SPEECH_MODEL", "").strip() or None,
        compute_type=os.getenv("SPEECH_COMPUTE_TYPE", "").strip() or None,
        device=os.getenv("SPEECH_DEVICE", "").strip() or None,
    )


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def speech_limits():
    from karpathy_wiki_speech.types import DEFAULT_LIMITS, TranscriptionLimits

    return TranscriptionLimits(
        max_bytes=_int_env("SPEECH_MAX_BYTES", DEFAULT_LIMITS.max_bytes),
        max_duration_seconds=_int_env(
            "SPEECH_MAX_DURATION_SECONDS", DEFAULT_LIMITS.max_duration_seconds
        ),
        allowed_extensions=DEFAULT_LIMITS.allowed_extensions,
    )


def worker_identity(backend: SpeechBackend, pipeline: TranscriptionOptions) -> str:
    """Stable identity of the worker's processing configuration."""
    from importlib.metadata import distributions

    image_stamp = Path("/app/speech-runtime-id")
    documented = json.dumps(
        {
            "image": image_stamp.read_text() if image_stamp.is_file() else None,
            "backend": backend.describe(pipeline),
            "limits": vars(speech_limits()),
            "runtime": sorted((dist.metadata["Name"], dist.version) for dist in distributions()),
            "code": [
                path.read_text(encoding="utf-8")
                for path in sorted(Path(__file__).parent.glob("*.py"))
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    import hashlib

    return hashlib.sha256(documented.encode("utf-8")).hexdigest()


def run_cycle(
    root: Path,
    backend: SpeechBackend,
    pipeline: TranscriptionOptions,
    worker_tag: str,
    limits: Any,
    decode: bool = True,
) -> tuple[int, int]:
    """Process every queued request; returns ``(completed, failed)``."""
    from karpathy_wiki_speech.cache import requests_dir

    requests_dir(root).mkdir(parents=True, exist_ok=True)
    atomic_json(root / "worker-identity.json", {"identity": worker_tag})
    completed = 0
    failed = 0
    for request in sorted(requests_dir(root).glob("*.json")):
        identifier = request.stem
        if not identifier_be_valid(identifier):
            LOG.warning("Speech request %s has a malformed name; dropping", request.name)
            request.unlink(missing_ok=True)
            failed += 1
            continue
        try:
            payload = parse_request(request)
        except (OSError, ValueError):
            LOG.warning("Speech request %s is unreadable; dropping", request.name)
            request.unlink(missing_ok=True)
            failed += 1
            continue
        if payload is None:
            LOG.warning("Speech request %s is malformed; dropping", identifier)
            request.unlink(missing_ok=True)
            failed += 1
            continue
        try:
            completed += process_request(
                root, backend, pipeline, worker_tag, payload, limits, decode
            )
        except Exception as error:  # noqa: BLE001 - failure reports stay content-free
            LOG.error(
                "Speech request %s failed with %s",
                identifier_tail(identifier),
                type(error).__name__,
            )
            write_failure(root, identifier, f"processing:{type(error).__name__}")
            failed += 1
        request.unlink(missing_ok=True)
    return completed, failed


def identifier_be_valid(identifier: str) -> bool:
    from karpathy_wiki_speech.cache import valid_request_id

    return valid_request_id(identifier)


def parse_request(request: Path) -> dict[str, Any] | None:
    payload = json.loads(request.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return None
    reference = payload.get("audio")
    if not isinstance(reference, str) or not _reference_allowed(reference):
        return None
    digest = payload.get("audio_sha256")
    if not isinstance(digest, str) or not digest.isalnum() or len(digest) != 64:
        return None
    options = payload.get("options")
    if not isinstance(options, dict):
        return None
    return {"audio": reference, "audio_sha256": digest, "options": options}


def _reference_allowed(reference: str) -> bool:
    """Requests may reference content below ``recordings/`` only."""
    return (
        reference.startswith("recordings/")
        and "\\" not in reference
        and all(part not in {"", ".", ".."} for part in reference.split("/"))
    )


def identifier_tail(identifier: str) -> str:
    return identifier[:20]


def process_request(
    root: Path,
    backend: SpeechBackend,
    pipeline: TranscriptionOptions,
    worker_tag: str,
    payload: dict[str, Any],
    limits: Any,
    decode: bool = True,
) -> int:
    """One request: verify, transcribe (or replay from cache), store."""
    from karpathy_wiki_speech.cache import load_cached_result

    audio_reference = str(payload["audio"])
    audio_sha256 = str(payload["audio_sha256"])
    requested = TranscriptionOptions(
        language=_optional_text(payload["options"].get("language")),
        diarize=bool(payload["options"].get("diarize", False)),
        model=pipeline.model,
        compute_type=pipeline.compute_type,
        device=pipeline.device,
    )
    cached = load_cached_result(
        root,
        audio_sha256,
        {"language": requested.language, "diarize": requested.diarize},
        worker_tag,
    )
    if cached is not None:
        LOG.info(
            "Speech request %s replayed from cache",
            identifier_tail(identifier_of_request(payload)),
        )
        return 1
    audio_path = root / audio_reference
    if file_revision(str(audio_path)) != audio_sha256:
        raise ValueError("request audio hash does not match the recording")
    decoded = decode_for(audio_path, limits, decode)
    result = backend.transcribe(str(audio_path), decoded, requested)
    write_result(root, audio_sha256, requested, result, worker_tag)
    LOG.info("Speech request %s completed", identifier_tail(identifier_of_request(payload)))
    return 1


def identifier_of_request(payload: dict[str, Any]) -> str:
    digest = str(payload.get("audio_sha256", ""))
    options = payload.get("options") or {}
    from karpathy_wiki_speech.cache import options_key

    return f"{digest}-{options_key(options)}"


def _optional_text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def decode_for(audio_path: Path, limits: Any, decode: bool) -> DecodedAudio:
    """PyAV decode with limits; ``decode=False`` skips real decoding.

    Only the deterministic script-style backends used by tests and CI run
    without the bundled FFmpeg decoder.
    """
    from karpathy_wiki_speech.types import accept_audio, decode_audio

    input_bytes = accept_audio(str(audio_path), limits)
    if not decode:
        return DecodedAudio(
            duration_seconds=10.0,
            input_bytes=input_bytes,
            format_name="fake",
        )
    return decode_audio(str(audio_path), limits)


def write_result(
    root: Path,
    audio_sha256: str,
    requested: TranscriptionOptions,
    result: TranscriptionResult,
    worker_tag: str,
) -> None:
    """Store the structured result and its per-audio index entry."""
    from karpathy_wiki_speech.cache import (
        cache_dir,
        cache_index_path,
        cache_result_path,
        load_index,
        options_key,
    )
    from karpathy_wiki_speech.types import result_identity

    cache_dir(root).mkdir(parents=True, exist_ok=True)
    identity = result_identity(audio_sha256, requested, result)
    destination = cache_result_path(root, identity)
    document = {
        "identity": identity,
        "worker_identity": worker_tag,
        "audio_sha256": audio_sha256,
        "backend": result.backend,
        "model": result.model,
        "language": result.language,
        "options": {
            "diarize": requested.diarize,
            "language": requested.language,
            "model": requested.model,
            "compute_type": requested.compute_type,
            "device": requested.device,
        },
        "segments": [segment.to_dict() for segment in result.segments],
    }
    atomic_json(destination, document)
    entries = [
        entry for entry in load_index(root, audio_sha256) if entry.get("identity") != identity
    ]
    entries.append(
        {
            "options_key": options_key(
                {"language": requested.language, "diarize": requested.diarize}
            ),
            "identity": identity,
            "worker_identity": worker_tag,
            "backend": result.backend,
            "model": result.model,
            "recorded": int(time.time()),
        }
    )
    atomic_json(cache_index_path(root, audio_sha256), entries)


def write_failure(root: Path, identifier: str, error_type: str) -> None:
    """Persist a content-free failure report the provider will discover."""
    from karpathy_wiki_speech.cache import failure_path

    audio_sha256, _, options_key = identifier.partition("-")
    destination = failure_path(root, audio_sha256, options_key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    atomic_json(destination, {"error": f"speech:{error_type}"})


def clear_failure(root: Path, audio_sha256: str, options_key: str) -> None:
    from karpathy_wiki_speech.cache import failure_path

    path = failure_path(root, audio_sha256, options_key)
    if path.is_file():
        path.unlink()


def atomic_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def write_health(root: Path, failed: int) -> None:
    atomic_json(root / "worker-health.json", {"checked_at": int(time.time()), "failed": failed})


def install_stop_handler() -> threading.Event:
    stop_event = threading.Event()

    def stop(_signum: Any, _frame: Any) -> None:
        stop_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)
    return stop_event


def run(speech_root: Path, once: bool) -> None:
    backend = worker_backend()
    pipeline = worker_pipeline_options()
    worker_tag = worker_identity(backend, pipeline)
    limits = speech_limits()
    poll_seconds = int(os.getenv("SPEECH_POLL_SECONDS", "5"))
    if poll_seconds <= 0:
        raise ValueError("SPEECH_POLL_SECONDS must be positive")
    speech_root.mkdir(parents=True, exist_ok=True)
    from karpathy_wiki_speech.cache import requests_dir

    requests_dir(speech_root).mkdir(parents=True, exist_ok=True)
    stop_event = install_stop_handler()
    while not stop_event.is_set():
        failure_count = 0
        try:
            completed, failure_count = run_cycle(
                speech_root,
                backend,
                pipeline,
                worker_tag,
                limits,
                decode=not isinstance(backend, FakeBackend),
            )
            if completed or failure_count:
                LOG.info("Speech cycle completed: %s done, %s failed", completed, failure_count)
        except (OSError, ValueError, RuntimeError):
            LOG.exception("Speech worker cycle failed")
            failure_count = 1
        write_health(speech_root, failure_count)
        if once:
            if failure_count:
                raise SystemExit(1)
            return
        if stop_event.wait(poll_seconds):
            break


def health_check(path: Path) -> None:
    """Exit nonzero when the worker heartbeat is missing or failing."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SystemExit(1) from error
    if int(payload.get("failed", 1)) != 0:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Local speech processing worker")
    parser.add_argument("--once", action="store_true", help="process queued requests and exit")
    parser.add_argument(
        "--health-check",
        metavar="PATH",
        help="validate the worker heartbeat file and exit",
    )
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    speech_root = Path(os.getenv("SPEECH_ROOT", "/data/speech"))
    if arguments.health_check:
        health_check(Path(arguments.health_check))
        return
    run(speech_root, arguments.once)


if __name__ == "__main__":
    main()
