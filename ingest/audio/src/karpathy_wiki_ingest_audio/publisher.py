"""Audio ingest service: discovery, transcription, redaction, publication.

One cycle:

1. rclone-mirror the upstream WebDAV folder into the private snapshot
   (audio extensions only are ever considered afterwards).
2. Resolve every discovered path to its persistent opaque source ID; the
   path-to-ID mapping lives in private provider storage and is persisted
   immediately so a crash never reassigns an already-published identity.
3. For each recording, load the structured speech result from the private
   cache keyed by audio SHA-256 plus the full processing key, or compute it
   through the replaceable speech backend.
4. Render each source with its own title and origin, redact with the shared
   anonymizer (span-aware across segment boundaries), validate, and stage a
   complete generation with the versioned provider manifest.
5. Publish atomically behind ``current``; any failure keeps the last
   successful generation active and reports content-free errors.

The cycle is a durable, idempotent job in the shared state store, keyed by
the upstream inventory, the redaction fingerprint, and the speech
processing identity, so re-submission never executes the same work twice.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import posixpath
import shutil
import signal
import socket
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from karpathy_wiki_ingest.manifest import (
    MANIFEST_FILENAME,
    ManifestItem,
    build_manifest,
    write_manifest,
)
from karpathy_wiki_ingest.shared import (
    TargetedAnonymizer,
    atomic_write,
    interval_seconds,
    required_env,
    write_health,
)
from karpathy_wiki_ingest.state import (
    JOB_DEAD,
    JOB_SUCCEEDED,
    JOB_SUPERSEDED,
    StateError,
    StateStore,
)
from karpathy_wiki_ingest_audio.connector import audio_inventory, synchronize
from karpathy_wiki_ingest_audio.identity import load_mapping, resolve_identity, save_mapping
from karpathy_wiki_ingest_audio.redaction import redact_transcript
from karpathy_wiki_ingest_audio.render import render_transcript_document
from karpathy_wiki_ingest_audio.speech_client import (
    SpeechTimeoutError,
    await_result,
    clear_failure,
    ensure_requested,
    load_result,
    stage_recording,
)
from karpathy_wiki_speech.types import (
    DEFAULT_LIMITS,
    TranscriptionLimits,
    TranscriptionOptions,
    TranscriptionResult,
    file_revision,
)

LOG = logging.getLogger("karpathy-wiki-audio")

SOURCE_NAME = "audio"
WIKI_ROOT = "audio"
GENERATIONS_DIRECTORY = "generations"
ACTIVE_SYMLINK = "current"
GENERATION_METADATA_FILENAME = ".generation.json"
STAGING_PREFIX = ".staging-"
TRANSCRIPT_DIRECTORY = "recordings"

RENDERER_VERSION = 1


@dataclass(frozen=True)
class Settings:
    incoming: Path
    sanitized: Path
    quarantine: Path
    mapping: Path
    speech_root: Path
    speech_timeout: int
    interval: int
    redactions: Path
    health_path: Path
    state_path: Path | None = None

    @classmethod
    def from_env(cls) -> Settings:
        state_path = os.getenv("INGEST_STATE_PATH", "").strip()
        return cls(
            incoming=Path(os.getenv("AUDIO_INCOMING_ROOT", "/data/incoming/audio")),
            sanitized=Path(os.getenv("AUDIO_SANITIZED_ROOT", "/data/sanitized/audio")),
            quarantine=Path(os.getenv("AUDIO_QUARANTINE_ROOT", "/data/quarantine/audio")),
            mapping=Path(os.getenv("AUDIO_MAPPING_ROOT", "/data/state/audio-identity")),
            speech_root=Path(os.getenv("SPEECH_ROOT", "/data/speech")),
            speech_timeout=int(os.getenv("SPEECH_TIMEOUT_SEC", "43200")),
            interval=interval_seconds(os.getenv("AUDIO_SYNC_INTERVAL", "1h")),
            redactions=Path(required_env("REDACTIONS_FILE")),
            health_path=Path(os.getenv("HEALTH_PATH", "/tmp/health.json")),
            state_path=Path(state_path) if state_path else None,
        )


@dataclass(frozen=True)
class SourceWork:
    """One discovered recording: identity, origin, and content revision."""

    source_id: str
    relative: str
    audio_sha256: str


def new_generation_id() -> str:
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{os.urandom(4).hex()}"


def wiki_path_of(source_id: str) -> str:
    return posixpath.join(WIKI_ROOT, f"recording-{source_id}", "index.md")


def source_path_of(generation: str, source_id: str) -> str:
    name = posixpath.join(TRANSCRIPT_DIRECTORY, f"{source_id}.md")
    return posixpath.join(GENERATIONS_DIRECTORY, generation, name)


def title_of(relative: str) -> str:
    """Render-time title source; the renderer publishes the anonymized form."""
    stem = relative.rsplit("/", 1)[-1]
    return stem.rsplit(".", 1)[0].replace("_", " ").strip() or "Aufzeichnung"


def build_sources(
    mapping: Path,
    inventory: dict[str, str],
    source_ids: dict[str, str],
) -> list[SourceWork]:
    """Resolve identities for every discovered recording.

    New identities are persisted immediately by ``resolve_identity`` so a
    crash never reassigns a source ID that was already published.
    """
    work: list[SourceWork] = []
    for relative, audio_sha256 in sorted(inventory.items()):
        source_id = resolve_identity(mapping, relative, source_ids)
        work.append(SourceWork(source_id=source_id, relative=relative, audio_sha256=audio_sha256))
    return work


def speech_options() -> TranscriptionOptions:
    return TranscriptionOptions(
        language=os.getenv("AUDIO_LANGUAGE", "").strip() or None,
        model=os.getenv("AUDIO_WHISPER_MODEL", "").strip() or None,
        compute_type=os.getenv("AUDIO_COMPUTE_TYPE", "").strip() or None,
        device=os.getenv("AUDIO_DEVICE", "").strip() or None,
        diarize=os.getenv("AUDIO_DIARIZE", "").strip().lower() in {"1", "true", "yes"},
    )


def requested_options_identity(options: TranscriptionOptions, speech_root: Path) -> str:
    """Include the active worker configuration in publication and durable job identity."""
    from karpathy_wiki_speech.cache import current_worker_identity, options_key

    worker = current_worker_identity(speech_root)
    if worker is None:
        raise RuntimeError("the speech worker has not announced its processing identity")
    return options_key(
        {
            "language": options.language,
            "diarize": options.diarize,
            "worker": worker,
        }
    )


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def speech_limits() -> TranscriptionLimits:
    return TranscriptionLimits(
        max_bytes=_int_env("AUDIO_MAX_BYTES", DEFAULT_LIMITS.max_bytes),
        max_duration_seconds=_int_env(
            "AUDIO_MAX_DURATION_SECONDS", DEFAULT_LIMITS.max_duration_seconds
        ),
        allowed_extensions=DEFAULT_LIMITS.allowed_extensions,
    )


def transcribe_source(
    work: SourceWork,
    snapshot: Path,
    speech_root: Path,
    options: TranscriptionOptions,
    limits: TranscriptionLimits,
    timeout_seconds: int,
) -> TranscriptionResult:
    """Prepare one recording and wait for the local speech worker.

    The worker receives only the staged audio path and the requested
    options; results (or content-free failures) arrive through the shared
    private cache. Raw audio and unredacted transcripts stay in the private
    speech root.
    """
    result = load_result(speech_root, work.audio_sha256, options)
    if result is not None:
        return result
    snapshot_file = snapshot / work.relative
    extension = work.relative.rsplit(".", 1)[-1].lower()
    audio_reference = stage_recording(
        speech_root,
        snapshot_file,
        work.audio_sha256,
        extension,
        limits.max_bytes,
    )
    clear_failure(speech_root, work.audio_sha256, options)
    ensure_requested(speech_root, work.audio_sha256, options, audio_reference)
    try:
        return await_result(
            speech_root,
            work.audio_sha256,
            options,
            timeout_seconds,
            poll_seconds=max(timeout_seconds // 120, 2),
        )
    except SpeechTimeoutError:
        LOG.error("Speech worker did not answer for source %s", work.source_id)
        raise SpeechTimeoutError(
            "the local speech worker did not answer before the cycle timeout"
        ) from None


def sanitize_into_generation(
    snapshot: Path,
    staging: Path,
    generation_name: str,
    anonymizer: TargetedAnonymizer,
    speech_root: Path,
    options: TranscriptionOptions,
    inventory: dict[str, str],
    source_ids: dict[str, str],
    mapping: Path,
    limits: TranscriptionLimits,
    previous_revisions: dict[str, str],
    quarantine: Path,
    speech_timeout: int,
) -> tuple[list[ManifestItem], int, int]:
    """Render, redact, and validate every source into the fresh generation.

    Sources that fail the speech worker, the redaction, or privacy
    validation are quarantined content-free and reported in the manifest;
    the caller refuses to publish a generation with failures.
    """
    items: list[ManifestItem] = []
    errors: list[dict[str, str]] = []
    failed = 0
    changed = 0
    for work in build_sources(mapping, inventory, source_ids):
        target = staging / TRANSCRIPT_DIRECTORY / f"{work.source_id}.md"
        try:
            # Public metadata (origin path, page title) is redacted before
            # rendering so the document, the manifest frontmatter, and the
            # wiki page all carry the sanitized value.
            safe_title, _ = anonymizer.anonymize(title_of(work.relative))
            safe_origin, _ = anonymizer.anonymize(work.relative)
            result = transcribe_source(work, snapshot, speech_root, options, limits, speech_timeout)
            segments, counts = redact_transcript(result.segments, anonymizer)
            document = render_transcript_document(
                title=safe_title,
                language=result.language,
                origin=safe_origin,
                audio_sha256=work.audio_sha256,
                segments=segments,
                backend=result.backend,
                model=result.model,
                options=dict(result.options),
                counts=counts,
            )
            # The full rendered document is the final redaction surface:
            # frontmatter, provenance, and every rendered entry must pass.
            output, _ = anonymizer.anonymize(document)
            revision = hashlib.sha256(output.encode("utf-8")).hexdigest()
            atomic_write(target, output)
            items.append(
                ManifestItem(
                    source_key=work.source_id,
                    source_path=source_path_of(generation_name, work.source_id),
                    wiki_path=wiki_path_of(work.source_id),
                    source_revision=revision,
                    frontmatter={
                        "source_adapter": SOURCE_NAME,
                        "source_path": safe_origin,
                        "source_revision": revision,
                    },
                    claim={"audio_source_id": work.source_id},
                )
            )
            if previous_revisions.get(work.source_id) != revision:
                changed += 1
            (quarantine / f"{work.source_id}.error").unlink(missing_ok=True)
        except (OSError, RuntimeError, ValueError) as error:
            failed += 1
            report_quarantine(quarantine, work.source_id, type(error).__name__, generation_name)
            errors.append(
                {
                    "source_key": work.source_id,
                    "path": posixpath.join(TRANSCRIPT_DIRECTORY, f"{work.source_id}.md"),
                    "error": f"processing:{type(error).__name__}",
                }
            )
            LOG.error("Audio source %s was quarantined", work.source_id)
    return items, changed, failed


def inventory_fingerprint(inventory: dict[str, str]) -> str:
    """Compare private upstream paths without publishing their original values."""
    encoded = json.dumps(inventory, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_generation_metadata(
    staging: Path,
    generation_name: str,
    fingerprint: str,
    inventory: dict[str, str],
    speech_identity: str,
) -> None:
    metadata = {
        "generation": generation_name,
        "created_at": int(time.time()),
        "redaction_fingerprint": fingerprint,
        "speech_identity": speech_identity,
        "inventory_fingerprint": inventory_fingerprint(inventory),
        "renderer_version": RENDERER_VERSION,
    }
    atomic_write(
        staging / GENERATION_METADATA_FILENAME,
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def report_quarantine(
    quarantine: Path, source_id: str, error_type: str, generation_name: str
) -> None:
    atomic_write(
        quarantine / f"{source_id}.error",
        f"source_key={source_id}\ngeneration={generation_name}\nerror_type={error_type}\n",
    )


def discard_abandoned_staging(generations: Path) -> None:
    for entry in sorted(generations.glob(f"{STAGING_PREFIX}*")):
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
        LOG.warning("Discarded abandoned generation staging %s", entry.name)


def prune_generations(generations: Path, keep: Path) -> None:
    for entry in sorted(generations.iterdir()):
        if entry == keep or entry.name == keep.name:
            continue
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def active_generation(sanitized: Path) -> Path | None:
    active = sanitized / ACTIVE_SYMLINK
    if not active.is_symlink():
        return None
    try:
        target = os.readlink(active)
    except OSError:
        return None
    return sanitized / target


def swap_active(sanitized: Path, generation: Path) -> None:
    """Atomically point ``current`` at a complete generation."""
    active = sanitized / ACTIVE_SYMLINK
    if active.exists() and not active.is_symlink():
        raise ValueError(
            f"{ACTIVE_SYMLINK} inside the audio source directory is reserved "
            "for the active-generation symlink; remove or rename the existing entry"
        )
    temporary = sanitized / f".{ACTIVE_SYMLINK}.{os.urandom(8).hex()}"
    temporary.symlink_to(posixpath.join(GENERATIONS_DIRECTORY, generation.name))
    os.replace(temporary, active)


def read_active_revisions(sanitized: Path) -> dict[str, str]:
    """Revision map of the currently published generation, keyed by source ID."""
    try:
        payload = json.loads((sanitized / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return {}
    revisions: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        source_key = item.get("source_key")
        revision = item.get("source_revision")
        if isinstance(source_key, str) and isinstance(revision, str):
            revisions[source_key] = revision
    return revisions


def revocations(
    items: list[ManifestItem],
    previous_revisions: dict[str, str],
    sanitized: Path,
) -> list[dict[str, Any]]:
    """Retain removal tombstones until their sources become live again."""
    published = set(previous_revisions)
    try:
        payload = json.loads((sanitized / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = {}
    previous_revoked = payload.get("revoked", []) if isinstance(payload, dict) else []
    if isinstance(previous_revoked, list):
        published.update(
            entry["source_key"]
            for entry in previous_revoked
            if isinstance(entry, dict) and isinstance(entry.get("source_key"), str)
        )
    live = {item.source_key for item in items}
    return [
        {"source_key": source_id, "claim": {"audio_source_id": source_id}}
        for source_id in sorted(published - live)
    ]


def publish_generation(
    snapshot: Path,
    sanitized: Path,
    quarantine: Path,
    mapping: Path,
    speech_root: Path,
    anonymizer: TargetedAnonymizer,
    options: TranscriptionOptions,
    inventory: dict[str, str],
    source_ids: dict[str, str],
    limits: TranscriptionLimits,
    speech_timeout: int,
    commit_guard: Callable[[], None] | None = None,
) -> tuple[int, int]:
    """Build a complete generation and publish it in one atomic step."""
    speech_identity = requested_options_identity(options, speech_root)
    sanitized.mkdir(parents=True, exist_ok=True)
    quarantine.mkdir(parents=True, exist_ok=True)
    generations = sanitized / GENERATIONS_DIRECTORY
    generations.mkdir(exist_ok=True)
    discard_abandoned_staging(generations)
    for report in sorted(quarantine.rglob("*.error")):
        report.unlink(missing_ok=True)

    staging = generations / f"{STAGING_PREFIX}{os.urandom(8).hex()}"
    generation = generations / new_generation_id()
    previous = active_generation(sanitized)
    previous_revisions = read_active_revisions(sanitized)
    try:
        staging.mkdir()
        items, changed, failed = sanitize_into_generation(
            snapshot,
            staging,
            generation.name,
            anonymizer,
            speech_root,
            options,
            inventory,
            source_ids,
            mapping,
            limits,
            previous_revisions,
            quarantine,
            speech_timeout,
        )
        write_generation_metadata(
            staging,
            generation.name,
            anonymizer.fingerprint,
            inventory,
            speech_identity,
        )
        if failed:
            # A generation is all-or-nothing: the last successful generation
            # and its manifest remain active, the rejection is reported
            # content-free, and retry backoff applies.
            shutil.rmtree(staging, ignore_errors=True)
            return 0, failed
        if requested_options_identity(options, speech_root) != speech_identity:
            raise RuntimeError("speech worker changed during publication; retry the cycle")
        staging.rename(generation)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if commit_guard is not None:
        commit_guard()
    swap_active(sanitized, generation)
    try:
        write_manifest(
            sanitized / MANIFEST_FILENAME,
            build_manifest(
                SOURCE_NAME,
                items,
                revoked=revocations(items, previous_revisions, sanitized),
                errors=[],
                wiki_root=WIKI_ROOT,
            ),
        )
    except BaseException:
        # Roll the publication back so readers keep the last coherent state.
        if previous is not None and previous.is_dir():
            swap_active(sanitized, previous)
        else:
            (sanitized / ACTIVE_SYMLINK).unlink(missing_ok=True)
        shutil.rmtree(generation, ignore_errors=True)
        raise
    prune_generations(generations, generation)
    return changed, failed


def cycle_idempotency_key(
    fingerprint: str,
    inventory: dict[str, str],
    speech_identity: str,
) -> str:
    """Stable identity of a cycle: upstream revisions, redactions, speech config."""
    encoded = json.dumps(
        {
            "fingerprint": fingerprint,
            "inventory": inventory,
            "speech": speech_identity,
        },
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return f"audio-generation:{hashlib.sha256(encoded).hexdigest()}"


def published_matches(
    sanitized: Path,
    anonymizer: TargetedAnonymizer,
    inventory: dict[str, str],
    speech_identity: str,
) -> bool:
    """Whether the active generation already publishes this exact cycle."""
    generation = active_generation(sanitized)
    if generation is None:
        return False
    try:
        metadata = json.loads(
            (generation / GENERATION_METADATA_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    if (
        metadata.get("redaction_fingerprint") != anonymizer.fingerprint
        or metadata.get("speech_identity") != speech_identity
        or metadata.get("inventory_fingerprint") != inventory_fingerprint(inventory)
        or metadata.get("renderer_version") != RENDERER_VERSION
    ):
        return False
    try:
        payload = json.loads((sanitized / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if payload.get("source") != SOURCE_NAME or payload.get("wiki_root") != WIKI_ROOT:
        return False
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return False
    files = {
        path.relative_to(generation).as_posix(): file_revision(str(path))
        for path in generation.rglob("*")
        if path.is_file() and path != generation / GENERATION_METADATA_FILENAME
    }
    if len(items) != len(files):
        return False
    parsed: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict):
            return False
        source_key = item.get("source_key")
        if not isinstance(source_key, str) or not source_key or source_key in parsed:
            return False
        relative = posixpath.join(TRANSCRIPT_DIRECTORY, f"{source_key}.md")
        if relative not in files:
            return False
        if item.get("source_path") != source_path_of(generation.name, source_key):
            return False
        if item.get("source_revision") != files[relative]:
            return False
        parsed[relative] = files[relative]
    return parsed == files


def record_generation(
    store: StateStore, sanitized: Path, generation: Path, fingerprint: str, *, now: int
) -> None:
    payload = json.loads((sanitized / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    items = payload.get("items") if isinstance(payload, dict) else None
    item_count = len(items) if isinstance(items, list) else 0
    manifest_revision = hashlib.sha256((sanitized / MANIFEST_FILENAME).read_bytes()).hexdigest()
    store.record_source_generation(
        SOURCE_NAME,
        generation.name,
        manifest_revision=manifest_revision,
        item_count=item_count,
        redaction_fingerprint=fingerprint,
        now=now,
    )


def publish_with_heartbeat(
    work: Callable[[Callable[[], None]], tuple[int, int]],
    store: StateStore,
    lease_id: str,
    lease_seconds: int,
) -> tuple[int, int]:
    """Run the publication while continuously renewing the job's lease."""
    outcome: dict[str, Any] = {}
    finished = threading.Event()
    renewed = threading.Event()
    renewed.set()

    def guard() -> None:
        if not renewed.is_set():
            raise StateError(f"audio ingest lease {lease_id} lost; aborting the publication")

    def target() -> None:
        try:
            outcome["result"] = work(guard)
        except BaseException as error:  # re-raised on the caller's thread
            outcome["error"] = error
        finally:
            finished.set()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    while not finished.wait(max(lease_seconds // 3, 1)):
        try:
            store.heartbeat(lease_id, lease_seconds=lease_seconds)
        except (sqlite3.Error, StateError, ValueError):
            renewed.clear()
            LOG.exception("Could not renew the audio ingest lease %s", lease_id)
            break
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    result = outcome["result"]
    assert isinstance(result, tuple)
    return result


def supersede_lost_inputs(store: StateStore, current_job_id: str, *, now: int) -> None:
    for pending in store.pending_jobs("ingest"):
        if pending.id == current_job_id or pending.payload.get("provider") != SOURCE_NAME:
            continue
        store.supersede(pending.id, now=now)
        LOG.info(
            "Superseded audio ingest job %s: its upstream inventory no longer exists", pending.id
        )


def process_cycle(
    snapshot: Path,
    sanitized: Path,
    quarantine: Path,
    mapping: Path,
    speech_root: Path,
    anonymizer: TargetedAnonymizer,
    options: TranscriptionOptions,
    source_ids: dict[str, str],
    limits: TranscriptionLimits,
    speech_timeout: int,
    store: StateStore | None,
    *,
    interval: int,
    now: int | None = None,
) -> tuple[int, int, bool]:
    """Run one ingest cycle as a durable, idempotent job.

    Returns ``(changed, failed, degraded)``; behavior mirrors the WebDAV
    plugin's durable model: accepted work keyed by inventory plus redaction
    fingerprint plus speech identity, backoff on failure, lease fencing,
    and degraded stateless publication when the store is unavailable.
    """
    moment = int(time.time() if now is None else now)
    inventory = audio_inventory(snapshot, limits)
    speech_identity = requested_options_identity(options, speech_root)
    if store is None:
        changed, failed = publish_generation(
            snapshot,
            sanitized,
            quarantine,
            mapping,
            speech_root,
            anonymizer,
            options,
            inventory,
            source_ids,
            limits,
            speech_timeout,
        )
        return changed, failed, False
    key = cycle_idempotency_key(anonymizer.fingerprint, inventory, speech_identity)
    lease_seconds = max(interval * 2, 300)
    published: tuple[int, int] | None = None
    publication_started = False
    try:
        store.expire_leases(now=moment)
        job, _ = store.enqueue(
            "ingest",
            {"provider": SOURCE_NAME, "upstream_files": len(inventory)},
            idempotency_key=key,
            now=moment,
        )
        supersede_lost_inputs(store, job.id, now=moment)
        if job.state == JOB_SUCCEEDED and published_matches(
            sanitized, anonymizer, inventory, speech_identity
        ):
            LOG.info(
                "Upstream and speech results unchanged; keeping generation %s",
                (job.result or {}).get("generation"),
            )
            return 0, 0, False
        if job.state in (JOB_DEAD, JOB_SUCCEEDED, JOB_SUPERSEDED):
            if job.state == JOB_DEAD:
                LOG.error(
                    "Audio ingest job %s is dead after repeated failures;"
                    " fix the rejected recording or redaction configuration",
                    job.id,
                )
                return 0, 1, False
            store.rearm(job.id, now=moment)
        lease = store.claim(
            job.id,
            holder=f"{socket.gethostname()}:{os.getpid()}",
            lease_seconds=lease_seconds,
            now=moment,
        )
        if lease is None:
            LOG.info(
                "Audio ingest job %s is not claimable yet; keeping the active generation", job.id
            )
            return 0, 1, False
        if published_matches(sanitized, anonymizer, inventory, speech_identity):
            # A previous cycle published the generation but died before
            # completing the job; record and complete without republishing.
            generation = active_generation(sanitized)
            assert generation is not None
            record_generation(store, sanitized, generation, anonymizer.fingerprint, now=moment)
            store.complete(
                lease.id, result={"generation": generation.name, "changed": 0}, now=moment
            )
            return 0, 0, False

        def run_publish(guard: Callable[[], None]) -> tuple[int, int]:
            return publish_generation(
                snapshot,
                sanitized,
                quarantine,
                mapping,
                speech_root,
                anonymizer,
                options,
                inventory,
                source_ids,
                limits,
                speech_timeout,
                commit_guard=guard,
            )

        publication_started = True
        try:
            changed, failed = publish_with_heartbeat(run_publish, store, lease.id, lease_seconds)
        except StateError:
            raise
        except Exception as error:
            store.fail(lease.id, error=f"publication:{type(error).__name__}", now=moment)
            raise
        published = (changed, failed)
        if failed:
            store.fail(lease.id, error=f"quarantined:{failed}", now=moment)
            return changed, failed, False
        save_mapping(mapping, source_ids)
        generation = active_generation(sanitized)
        assert generation is not None
        record_generation(store, sanitized, generation, anonymizer.fingerprint, now=moment)
        store.complete(
            lease.id, result={"generation": generation.name, "changed": changed}, now=moment
        )
        return changed, failed, False
    except sqlite3.Error:
        # Coordination is unavailable; publish without state, but never again
        # in the same cycle and never outside the lease when the work already
        # ran. The degraded cycle stays visible through the returned flag.
        LOG.exception("Durable ingest state is unavailable; publishing without state")
        if publication_started and published is None:
            raise
        if published is not None:
            changed, failed = published
            return changed, failed, True
        changed, failed = publish_generation(
            snapshot,
            sanitized,
            quarantine,
            mapping,
            speech_root,
            anonymizer,
            options,
            inventory,
            source_ids,
            limits,
            speech_timeout,
        )
        return changed, failed, True


def install_stop_handler() -> threading.Event:
    stop_event = threading.Event()

    def stop(_signum: Any, _frame: Any) -> None:
        stop_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)
    return stop_event


def run(once: bool) -> None:
    environment = Settings.from_env()
    incoming, sanitized, quarantine, mapping, interval, redactions, health_path = (
        environment.incoming,
        environment.sanitized,
        environment.quarantine,
        environment.mapping,
        environment.interval,
        environment.redactions,
        environment.health_path,
    )
    stop_event = install_stop_handler()
    incoming.mkdir(parents=True, exist_ok=True)
    store: StateStore | None = None
    try:
        while not stop_event.is_set():
            failed = 0
            degraded = False
            store_failed = False
            if environment.state_path is not None and store is None:
                try:
                    store = StateStore.open(environment.state_path)
                except (sqlite3.Error, OSError):
                    LOG.exception("Durable ingest state is unavailable; continuing without state")
                    store_failed = True
            try:
                # The redaction configuration is reloaded every cycle so a
                # changed redactions file regenerates every publication.
                anonymizer = TargetedAnonymizer.from_file(redactions)
                options = speech_options()
                limits = speech_limits()
                synchronize(incoming, os.environ["WEBDAV_PATH"])
                source_ids = load_mapping(mapping)
                changed, failed, degraded = process_cycle(
                    incoming,
                    sanitized,
                    quarantine,
                    mapping,
                    environment.speech_root,
                    anonymizer,
                    options,
                    source_ids,
                    limits,
                    environment.speech_timeout,
                    store,
                    interval=interval,
                )
                LOG.info(
                    "Audio synchronization complete: %s changed, %s quarantined", changed, failed
                )
            except KeyError as error:
                failed = 1
                LOG.exception("Audio configuration is incomplete: %s is missing", error)
            except subprocess.CalledProcessError:
                # rclone already retried internally; keep the daemon alive and
                # retry on the next synchronization interval.
                failed = 1
                LOG.exception("rclone synchronization failed")
            except (OSError, RuntimeError, ValueError):
                failed = 1
                LOG.exception("Audio synchronization failed")
            metrics = None
            if store is not None:
                try:
                    metrics = store.metrics()
                except sqlite3.Error:
                    LOG.exception("Durable ingest state metrics are unavailable")
                    degraded = True
            if degraded or store_failed:
                # A cycle without durable coordination is a real failure.
                failed = 1
            write_health(health_path, failed, metrics)
            if once:
                if failed:
                    raise SystemExit(1)
                return
            if stop_event.wait(interval):
                break
    finally:
        if store is not None:
            store.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synchronize, transcribe, and publish WebDAV audio sources"
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(args.once)


if __name__ == "__main__":
    main()
