"""WebDAV ingest plugin: publish upstream files as coherent sanitized generations.

Every synchronization cycle builds a complete generation in a private staging
directory, anonymizes and validates every file with a freshly loaded redaction
configuration, and only then exposes it to readers by atomically switching the
``current`` symlink and rewriting the provider manifest. A failed cycle keeps
the last successful generation active.
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
import uuid
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
    StateError,
    StateStore,
)

LOG = logging.getLogger("karpathy-wiki-webdav")

SOURCE_NAME = "webdav"
WIKI_ROOT = "webdav"
GENERATIONS_DIRECTORY = "generations"
ACTIVE_SYMLINK = "current"
GENERATION_METADATA_FILENAME = ".generation.json"
STAGING_PREFIX = ".staging-"

# Reserved source file names are never published into a generation: the
# provider manifest contract reserves `manifest.json` at the root of every
# source directory, and `.generation.json` is the generation's own metadata.
RESERVED_SOURCE_FILENAMES = {
    MANIFEST_FILENAME: "ReservedManifestName",
    GENERATION_METADATA_FILENAME: "ReservedGenerationMetadataName",
}


@dataclass(frozen=True)
class Settings:
    incoming: Path
    sanitized: Path
    quarantine: Path
    interval: int
    redactions: Path
    health_path: Path
    state_path: Path | None = None

    @classmethod
    def from_env(cls) -> Settings:
        state_path = os.getenv("INGEST_STATE_PATH", "").strip()
        return cls(
            incoming=Path(os.getenv("WEBDAV_INCOMING_ROOT", "/data/incoming/webdav")),
            sanitized=Path(os.getenv("WEBDAV_SANITIZED_ROOT", "/data/sanitized/webdav")),
            quarantine=Path(os.getenv("WEBDAV_QUARANTINE_ROOT", "/data/quarantine/webdav")),
            interval=interval_seconds(os.getenv("WEBDAV_SYNC_INTERVAL", "15m")),
            redactions=Path(required_env("REDACTIONS_FILE")),
            health_path=Path(os.getenv("HEALTH_PATH", "/tmp/health.json")),
            state_path=Path(state_path) if state_path else None,
        )


@dataclass(frozen=True)
class GenerationItem:
    """One sanitized file staged for publication."""

    source_key: str
    revision: str


def upstream_inventory(incoming: Path) -> dict[str, str]:
    """Content hashes of the synchronized upstream tree.

    The inventory records revisions only, never content, and is stored as
    generation metadata so every generation documents what it published.
    """
    return {
        path.relative_to(incoming).as_posix(): file_revision(path)
        for path in sorted(incoming.rglob("*"))
        if path.is_file()
    }


def file_revision(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def new_generation_id() -> str:
    return f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{uuid.uuid4().hex[:8]}"


def write_generation_metadata(
    staging: Path, generation_name: str, fingerprint: str, inventory: dict[str, str]
) -> None:
    """Record the generation identity inside its own sanitized tree."""
    metadata = {
        "generation": generation_name,
        "created_at": int(time.time()),
        "redaction_fingerprint": fingerprint,
        "upstream_inventory": inventory,
    }
    atomic_write(
        staging / GENERATION_METADATA_FILENAME,
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )


def sanitize_into_generation(
    incoming: Path,
    staging: Path,
    generation_name: str,
    anonymizer: TargetedAnonymizer,
    quarantine: Path,
) -> tuple[list[GenerationItem], list[dict[str, str]], int]:
    """Anonymize every upstream file into the fresh generation directory.

    Files that cannot be decoded or that fail privacy validation never enter
    the generation; they are reported content-free and counted as failed.
    """
    incoming_files = {path.relative_to(incoming) for path in incoming.rglob("*") if path.is_file()}
    items: list[GenerationItem] = []
    errors: list[dict[str, str]] = []
    failed = 0
    for relative in sorted(incoming_files):
        source = incoming / relative
        target = staging / relative
        reserved_error_type = RESERVED_SOURCE_FILENAMES.get(relative.as_posix())
        if reserved_error_type is not None:
            # Reserved names (`manifest.json`, `.generation.json`) never
            # carry upstream content; the generation publishes only its own
            # metadata and the provider manifest stays at the source root.
            failed += 1
            report_quarantine(quarantine, relative, reserved_error_type, generation_name)
            errors.append(
                {
                    "path": f"{GENERATIONS_DIRECTORY}/{generation_name}/{relative.as_posix()}",
                    "error": reserved_error_type,
                }
            )
            LOG.error(
                "WebDAV file %s uses the reserved name %s and was quarantined",
                relative,
                reserved_error_type,
            )
            continue
        try:
            content = source.read_text(encoding="utf-8")
            output, _ = anonymizer.anonymize(content)
            revision = hashlib.sha256(output.encode("utf-8")).hexdigest()
            atomic_write(target, output)
            items.append(
                GenerationItem(
                    source_key=relative.as_posix(),
                    revision=revision,
                )
            )
            (quarantine / f"{relative}.error").unlink(missing_ok=True)
        except (OSError, UnicodeError, ValueError) as error:
            failed += 1
            report_quarantine(quarantine, relative, type(error).__name__, generation_name)
            errors.append(
                {
                    "path": f"{GENERATIONS_DIRECTORY}/{generation_name}/{relative.as_posix()}",
                    "error": type(error).__name__,
                }
            )
            LOG.error("WebDAV file %s was quarantined", relative)
    return items, errors, failed


def report_quarantine(
    quarantine: Path, relative: Path, error_type: str, generation_name: str
) -> None:
    report = quarantine / f"{relative}.error"
    atomic_write(
        report,
        f"path={relative.as_posix()}\ngeneration={generation_name}\nerror_type={error_type}\n",
    )


def discard_abandoned_staging(generations: Path) -> None:
    """Remove staging directories left behind by interrupted cycles."""
    for entry in sorted(generations.glob(f"{STAGING_PREFIX}*")):
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)
        LOG.warning("Discarded abandoned generation staging %s", entry.name)


def prune_generations(generations: Path, keep: Path) -> None:
    """Retention: only the generation referenced by the active pointer stays."""
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
        # A leftover real file or directory from a pre-generation layout is
        # never replaced; recovery is manual (see the data layout docs).
        raise ValueError(
            f"{ACTIVE_SYMLINK} inside the WebDAV source directory is reserved "
            "for the active-generation symlink; remove or rename the existing entry"
        )
    temporary = sanitized / f".{ACTIVE_SYMLINK}.{uuid.uuid4().hex}"
    temporary.symlink_to(posixpath.join(GENERATIONS_DIRECTORY, generation.name))
    os.replace(temporary, active)


def read_active_revisions(sanitized: Path) -> dict[str, str]:
    """Revision map of the currently published generation, keyed by source key."""
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


def manifest_items(items: list[GenerationItem], generation_name: str) -> list[ManifestItem]:
    return [
        ManifestItem(
            source_key=item.source_key,
            # The manifest is written separately from the active symlink. Point
            # at the immutable generation so a reader cannot combine an old
            # manifest with bytes from a newer generation during publication.
            source_path=f"{GENERATIONS_DIRECTORY}/{generation_name}/{item.source_key}",
            wiki_path=posixpath.join(WIKI_ROOT, item.source_key, "index.md"),
            source_revision=item.revision,
            frontmatter={
                "source_adapter": SOURCE_NAME,
                "source_path": item.source_key,
                "source_revision": item.revision,
            },
            claim={"source_path": item.source_key},
        )
        for item in items
    ]


def publish_generation(
    incoming: Path,
    sanitized: Path,
    quarantine: Path,
    anonymizer: TargetedAnonymizer,
    inventory: dict[str, str] | None = None,
) -> tuple[int, int]:
    """Build a complete generation and publish it in one atomic step.

    The generation is built and validated in a private staging directory.
    ``current`` is switched only after the full sanitized tree exists; the
    provider manifest is rewritten immediately afterwards. Any failure before
    or during publication keeps the previous successful generation active.
    ``inventory`` may carry the upstream revision map computed earlier in the
    cycle so it is only hashed once.
    """
    sanitized.mkdir(parents=True, exist_ok=True)
    quarantine.mkdir(parents=True, exist_ok=True)
    generations = sanitized / GENERATIONS_DIRECTORY
    generations.mkdir(exist_ok=True)
    discard_abandoned_staging(generations)
    for report in sorted(quarantine.rglob("*.error")):
        report.unlink(missing_ok=True)

    if inventory is None:
        inventory = upstream_inventory(incoming)
    staging = generations / f"{STAGING_PREFIX}{uuid.uuid4().hex}"
    generation = generations / new_generation_id()
    previous = active_generation(sanitized)
    previous_revisions = read_active_revisions(sanitized)
    try:
        staging.mkdir()
        items, errors, failed = sanitize_into_generation(
            incoming, staging, generation.name, anonymizer, quarantine
        )
        write_generation_metadata(staging, generation.name, anonymizer.fingerprint, inventory)
        if failed:
            # A generation is all-or-nothing: quarantine reports describe the
            # rejected candidate, while the last successful generation and its
            # manifest remain untouched.
            shutil.rmtree(staging, ignore_errors=True)
            return 0, failed
        staging.rename(generation)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    swap_active(sanitized, generation)
    try:
        write_manifest(
            sanitized / MANIFEST_FILENAME,
            build_manifest(
                SOURCE_NAME,
                manifest_items(items, generation.name),
                errors=errors,
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
    changed = sum(1 for item in items if previous_revisions.get(item.source_key) != item.revision)
    return changed, failed


def cycle_idempotency_key(fingerprint: str, inventory: dict[str, str]) -> str:
    """Stable identity of an ingest cycle: upstream revisions plus redactions.

    The identical upstream state with the identical redaction configuration is
    the same accepted work, so re-submission cannot execute a second cycle.
    """
    encoded = json.dumps(
        {"fingerprint": fingerprint, "inventory": inventory},
        ensure_ascii=False,
        sort_keys=True,
    ).encode("utf-8")
    return f"webdav-generation:{hashlib.sha256(encoded).hexdigest()}"


def published_matches(
    sanitized: Path, anonymizer: TargetedAnonymizer, inventory: dict[str, str]
) -> bool:
    """Whether the active generation already publishes this exact cycle.

    Compares the generation's own metadata (upstream inventory hashes and
    redaction fingerprint) with the current cycle, so a completed job whose
    publication was later lost is detected instead of skipped.
    """
    generation = active_generation(sanitized)
    if generation is None:
        return False
    try:
        metadata = json.loads(
            (generation / GENERATION_METADATA_FILENAME).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return False
    return (
        metadata.get("redaction_fingerprint") == anonymizer.fingerprint
        and metadata.get("upstream_inventory") == inventory
    )


def record_generation(
    store: StateStore, sanitized: Path, generation: Path, fingerprint: str, *, now: int
) -> None:
    """Record the published generation in the durable state store."""
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
    work: Callable[[], tuple[int, int]],
    store: StateStore,
    lease_id: str,
    lease_seconds: int,
) -> tuple[int, int]:
    """Run the publication while continuously renewing the job's lease.

    Long publications must never outlive the lease: an expired lease would let
    another process recover and claim the same job while the first one is
    still mutating the generation directory. The work itself runs unchanged;
    only the lease bookkeeping is interleaved.
    """
    outcome: dict[str, Any] = {}
    finished = threading.Event()

    def target() -> None:
        try:
            outcome["result"] = work()
        except BaseException as error:  # re-raised on the caller's thread
            outcome["error"] = error
        finally:
            finished.set()

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    while not finished.wait(max(lease_seconds // 3, 1)):
        try:
            store.heartbeat(lease_id, lease_seconds=lease_seconds)
        except (sqlite3.Error, StateError):
            # Renewal failed; never abandon running work, just stop renewing.
            # The lease is left to expire and the outcome is reported, so the
            # recovery path never republishes concurrently.
            LOG.exception("Could not renew the ingest lease %s", lease_id)
            break
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    result = outcome["result"]
    assert isinstance(result, tuple)
    return result


def process_cycle(
    incoming: Path,
    sanitized: Path,
    quarantine: Path,
    anonymizer: TargetedAnonymizer,
    store: StateStore | None,
    *,
    interval: int,
    now: int | None = None,
) -> tuple[int, int, bool]:
    """Run one ingest cycle as a durable, idempotent job.

    Returns ``(changed, failed, degraded)`` where ``degraded`` marks a cycle
    that ran without durable state. Without a state store the cycle behaves
    exactly as before. With one, the cycle is an accepted job keyed by the
    upstream inventory and redaction fingerprint: identical accepted work is
    never executed twice, unchanged upstream content keeps the active
    generation, failures back off instead of hammering every interval, and
    expired leases from interrupted cycles are recovered. A failing state
    store degrades to the stateless publication instead of losing ingest
    availability, but the degraded cycle stays visible through the health
    record.
    """
    moment = int(time.time() if now is None else now)
    inventory = upstream_inventory(incoming)
    if store is None:
        changed, failed = publish_generation(
            incoming, sanitized, quarantine, anonymizer, inventory=inventory
        )
        return changed, failed, False
    key = cycle_idempotency_key(anonymizer.fingerprint, inventory)
    lease_seconds = max(interval * 2, 300)
    published: tuple[int, int] | None = None
    try:
        store.expire_leases(now=moment)
        job, _ = store.enqueue(
            "ingest",
            {"provider": SOURCE_NAME, "upstream_files": len(inventory)},
            idempotency_key=key,
            now=moment,
        )
        if job.state == JOB_SUCCEEDED and published_matches(sanitized, anonymizer, inventory):
            LOG.info(
                "Upstream and redactions unchanged; keeping generation %s",
                (job.result or {}).get("generation"),
            )
            return 0, 0, False
        if job.state in (JOB_DEAD, JOB_SUCCEEDED):
            # A dead job with unchanged content stays dead: the input is
            # deterministic and will fail again. A succeeded job whose
            # publication is no longer active is rearmed to restore it.
            if job.state == JOB_DEAD:
                LOG.error(
                    "Ingest job %s is dead after repeated failures;"
                    " fix the rejected source content",
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
            LOG.info("Ingest job %s is not claimable yet; keeping the active generation", job.id)
            return 0, 1, False
        if published_matches(sanitized, anonymizer, inventory):
            # A previous cycle published the generation but died before
            # completing the job; record and complete without republishing.
            generation = active_generation(sanitized)
            assert generation is not None
            record_generation(store, sanitized, generation, anonymizer.fingerprint, now=moment)
            store.complete(
                lease.id, result={"generation": generation.name, "changed": 0}, now=moment
            )
            return 0, 0, False
        changed, failed = publish_with_heartbeat(
            lambda: publish_generation(
                incoming, sanitized, quarantine, anonymizer, inventory=inventory
            ),
            store,
            lease.id,
            lease_seconds,
        )
        published = (changed, failed)
        if failed:
            store.fail(lease.id, error=f"quarantined:{failed}", now=moment)
            return changed, failed, False
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
        if published is not None:
            changed, failed = published
            return changed, failed, True
        changed, failed = publish_generation(
            incoming, sanitized, quarantine, anonymizer, inventory=inventory
        )
        return changed, failed, True


def synchronize(incoming: Path, path: str) -> None:
    subprocess.run(
        [
            "rclone",
            "sync",
            f"webdav:{path}",
            str(incoming),
            "--create-empty-src-dirs",
            "--retries",
            "3",
            "--low-level-retries",
            "10",
            "--log-level",
            "INFO",
        ],
        check=True,
    )


def install_stop_handler() -> threading.Event:
    stop_event = threading.Event()

    def stop(_signum, _frame) -> None:
        stop_event.set()

    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, stop)
    return stop_event


def run(once: bool) -> None:
    environment = Settings.from_env()
    incoming, sanitized, quarantine, interval, redactions, health_path, state_path = (
        environment.incoming,
        environment.sanitized,
        environment.quarantine,
        environment.interval,
        environment.redactions,
        environment.health_path,
        environment.state_path,
    )
    stop_event = install_stop_handler()
    incoming.mkdir(parents=True, exist_ok=True)
    store: StateStore | None = None
    try:
        while not stop_event.is_set():
            failed = 0
            degraded = False
            store_failed = False
            if state_path is not None and store is None:
                # Keep retrying the store: a transient outage must not disable
                # durability for the daemon's whole lifetime. An incompatible
                # schema is not retried; it fails the daemon visibly.
                try:
                    store = StateStore.open(state_path)
                except (sqlite3.Error, OSError):
                    LOG.exception("Durable ingest state is unavailable; continuing without state")
                    store_failed = True
            try:
                # The redaction configuration is reloaded for every cycle so a
                # changed redactions file regenerates every applicable file.
                anonymizer = TargetedAnonymizer.from_file(redactions)
                synchronize(incoming, os.environ["WEBDAV_PATH"])
                changed, failed, degraded = process_cycle(
                    incoming, sanitized, quarantine, anonymizer, store, interval=interval
                )
                LOG.info(
                    "WebDAV synchronization complete: %s changed, %s quarantined", changed, failed
                )
            except KeyError as error:
                failed = 1
                LOG.exception("WebDAV configuration is incomplete: %s is missing", error)
            except subprocess.CalledProcessError:
                # rclone already retried internally; keep the daemon alive and
                # retry on the next synchronization interval.
                failed = 1
                LOG.exception("rclone synchronization failed")
            except (OSError, UnicodeError, ValueError):
                failed = 1
                LOG.exception("WebDAV synchronization failed")
            metrics = None
            if store is not None:
                try:
                    metrics = store.metrics()
                except sqlite3.Error:
                    LOG.exception("Durable ingest state metrics are unavailable")
                    degraded = True
            if degraded or store_failed:
                # A cycle without durable coordination is a real failure: keep
                # the daemon unhealthy until the store works again.
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
    parser = argparse.ArgumentParser(description="Synchronize and anonymize WebDAV source files")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(args.once)


if __name__ == "__main__":
    main()
