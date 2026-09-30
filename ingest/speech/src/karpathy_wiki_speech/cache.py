"""Speech cache and worker queue: the shared filesystem between provider and worker.

The speech root holds private directories (never mounted into OpenCode or
the wiki):

``cache/``
    structured, unredacted speech results keyed by audio SHA-256 plus the
    full processing key, plus a per-audio index mapping the requested
    options to the newest result;
``requests/``
    provider submissions ``<request-id>.json`` (content-free, referencing an
    audio file inside the private recordings store);
``recordings/``
    immutable per-audio-hash copies the worker transcribes.

The audio provider owns requests + recordings; the worker processes each
request, writes the result atomically into the cache, and deletes the
request. Identical work replays from the cache; a changed worker image
supersedes older results for the same options.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path

from karpathy_wiki_speech.types import Segment, TranscriptionResult

REQUESTS_DIRECTORY = "requests"
CACHE_DIRECTORY = "cache"
RECORDINGS_DIRECTORY = "recordings"
FAILURES_DIRECTORY = "failures"
HEALTH_FILENAME = "worker-health.json"

REQUEST_ID_PATTERN = re.compile(r"^[a-f0-9]{64}-[a-f0-9]{64}$")


def requests_dir(root: Path) -> Path:
    return root / REQUESTS_DIRECTORY


def cache_dir(root: Path) -> Path:
    return root / CACHE_DIRECTORY


def recordings_dir(root: Path) -> Path:
    return root / RECORDINGS_DIRECTORY


def failures_dir(root: Path) -> Path:
    return root / FAILURES_DIRECTORY


def request_id(audio_sha256: str, options_key: str) -> str:
    """Deterministic request id: one queue entry per recording + options."""
    return f"{audio_sha256}-{options_key}"


def options_key(options: Mapping[str, object]) -> str:
    """Cache/request key of the provider-requested options."""
    encoded = json.dumps(
        options,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def valid_request_id(value: str) -> bool:
    return isinstance(value, str) and bool(REQUEST_ID_PATTERN.fullmatch(value))


def request_path(root: Path, identifier: str) -> Path:
    return requests_dir(root) / f"{identifier}.json"


def cache_result_path(root: Path, identity: str) -> Path:
    return cache_dir(root) / f"{identity}.json"


def cache_index_path(root: Path, audio_sha256: str) -> Path:
    return cache_dir(root) / f"{audio_sha256}.results.json"


def failure_path(root: Path, audio_sha256: str, options_key: str) -> Path:
    return failures_dir(root) / f"{audio_sha256}-{options_key}.json"


def recording_path(root: Path, audio_sha256: str, extension: str) -> Path:
    return recordings_dir(root) / audio_sha256[:2] / f"{audio_sha256}.{extension}"


def load_index(root: Path, audio_sha256: str) -> list[dict[str, object]]:
    """Per-audio index entries, newest last."""
    try:
        payload = json.loads(cache_index_path(root, audio_sha256).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    return [entry for entry in payload if isinstance(entry, dict)]


def newest_index_entry(
    root: Path,
    audio_sha256: str,
    options: Mapping[str, object],
) -> dict[str, object] | None:
    """The newest cached result for ``options`` (a changed worker supersedes)."""
    key = options_key(options)
    found: dict[str, object] | None = None
    for entry in load_index(root, audio_sha256):
        if entry.get("options_key") == key and isinstance(entry.get("identity"), str):
            found = entry
    return found


def find_cached_result(
    root: Path,
    audio_sha256: str,
    options: Mapping[str, object],
    worker_identity: str | None = None,
) -> Path | None:
    entry = newest_index_entry(root, audio_sha256, options)
    worker_identity = worker_identity or current_worker_identity(root)
    if not worker_identity or entry is None or entry.get("worker_identity") != worker_identity:
        return None
    identity = entry["identity"]
    assert isinstance(identity, str)
    if re.fullmatch(r"[a-f0-9]{64}", identity) is None:
        return None
    return cache_result_path(root, identity)


def parse_result_payload(payload: object) -> TranscriptionResult | None:
    """Validate a cached worker result; ``None`` marks it unusable."""
    if not isinstance(payload, dict):
        return None
    backend, model = payload.get("backend"), payload.get("model")
    if not isinstance(backend, str) or not isinstance(model, str):
        return None
    language = payload.get("language")
    if language is not None and not isinstance(language, str):
        return None
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list):
        return None
    segments = []
    for raw in raw_segments:
        if not isinstance(raw, dict):
            return None
        start_ms, end_ms, text = raw.get("start_ms"), raw.get("end_ms"), raw.get("text")
        speaker_id = raw.get("speaker_id")
        if (
            type(start_ms) is not int
            or type(end_ms) is not int
            or not isinstance(text, str)
            or (speaker_id is not None and type(speaker_id) is not int)
        ):
            return None
        segments.append(Segment(start_ms, end_ms, text, speaker_id))
    rendered = payload.get("options")
    return TranscriptionResult(
        segments=segments,
        language=language,
        backend=backend,
        model=model,
        options=rendered if isinstance(rendered, dict) else {},
    )


def load_cached_result(
    root: Path,
    audio_sha256: str,
    options: Mapping[str, object],
    worker_identity: str | None = None,
) -> TranscriptionResult | None:
    """Load a complete result for this recording, options, and active worker.

    The index is only a pointer: missing, unreadable, or malformed results
    are cache misses, so a queued request can repair them by transcribing.
    """
    worker_identity = worker_identity or current_worker_identity(root)
    path = find_cached_result(root, audio_sha256, options, worker_identity)
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    rendered = payload.get("options")
    if (
        payload.get("identity") != path.stem
        or payload.get("audio_sha256") != audio_sha256
        or payload.get("worker_identity") != worker_identity
        or not isinstance(rendered, dict)
        or any(key not in rendered or rendered[key] != value for key, value in options.items())
    ):
        return None
    return parse_result_payload(payload)


def current_worker_identity(root: Path) -> str | None:
    """The active worker announces its processing identity before consuming requests."""
    try:
        payload = json.loads((root / "worker-identity.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    identity = payload.get("identity") if isinstance(payload, dict) else None
    return identity if isinstance(identity, str) and identity else None
