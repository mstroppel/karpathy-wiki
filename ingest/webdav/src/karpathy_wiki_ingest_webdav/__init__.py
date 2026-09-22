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
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

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

LOG = logging.getLogger("karpathy-wiki-webdav")

SOURCE_NAME = "webdav"
WIKI_ROOT = "webdav"
GENERATIONS_DIRECTORY = "generations"
ACTIVE_SYMLINK = "current"
GENERATION_METADATA_FILENAME = ".generation.json"
STAGING_PREFIX = ".staging-"


@dataclass(frozen=True)
class Settings:
    incoming: Path
    sanitized: Path
    quarantine: Path
    interval: int
    redactions: Path
    health_path: Path

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            incoming=Path(os.getenv("WEBDAV_INCOMING_ROOT", "/data/incoming/webdav")),
            sanitized=Path(os.getenv("WEBDAV_SANITIZED_ROOT", "/data/sanitized/webdav")),
            quarantine=Path(os.getenv("WEBDAV_QUARANTINE_ROOT", "/data/quarantine/webdav")),
            interval=interval_seconds(os.getenv("WEBDAV_SYNC_INTERVAL", "15m")),
            redactions=Path(required_env("REDACTIONS_FILE")),
            health_path=Path(os.getenv("HEALTH_PATH", "/tmp/health.json")),
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
        if relative == Path(GENERATION_METADATA_FILENAME):
            # The generation metadata name is reserved inside every
            # generation; never publish upstream content under it.
            failed += 1
            report_quarantine(
                quarantine, relative, "ReservedGenerationMetadataName", generation_name
            )
            errors.append(
                {
                    "path": f"{ACTIVE_SYMLINK}/{relative.as_posix()}",
                    "error": "ReservedGenerationMetadataName",
                }
            )
            LOG.error(
                "WebDAV file %s uses the reserved generation metadata name and was quarantined",
                relative,
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
                    "path": f"{ACTIVE_SYMLINK}/{relative.as_posix()}",
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


def manifest_items(items: list[GenerationItem]) -> list[ManifestItem]:
    return [
        ManifestItem(
            source_key=item.source_key,
            source_path=f"{ACTIVE_SYMLINK}/{item.source_key}",
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
) -> tuple[int, int]:
    """Build a complete generation and publish it in one atomic step.

    The generation is built and validated in a private staging directory.
    ``current`` is switched only after the full sanitized tree exists; the
    provider manifest is rewritten immediately afterwards. Any failure before
    or during publication keeps the previous successful generation active.
    """
    sanitized.mkdir(parents=True, exist_ok=True)
    quarantine.mkdir(parents=True, exist_ok=True)
    generations = sanitized / GENERATIONS_DIRECTORY
    generations.mkdir(exist_ok=True)
    discard_abandoned_staging(generations)
    for report in sorted(quarantine.rglob("*.error")):
        report.unlink(missing_ok=True)

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
                manifest_items(items),
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
    incoming, sanitized, quarantine, interval, redactions, health_path = (
        environment.incoming,
        environment.sanitized,
        environment.quarantine,
        environment.interval,
        environment.redactions,
        environment.health_path,
    )
    stop_event = install_stop_handler()
    incoming.mkdir(parents=True, exist_ok=True)
    while not stop_event.is_set():
        failed = 0
        try:
            # The redaction configuration is reloaded for every cycle so a
            # changed redactions file regenerates every applicable file.
            anonymizer = TargetedAnonymizer.from_file(redactions)
            synchronize(incoming, os.environ["WEBDAV_PATH"])
            changed, failed = publish_generation(incoming, sanitized, quarantine, anonymizer)
            LOG.info("WebDAV synchronization complete: %s changed, %s quarantined", changed, failed)
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
        write_health(health_path, failed)
        if once:
            if failed:
                raise SystemExit(1)
            return
        if stop_event.wait(interval):
            break


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronize and anonymize WebDAV source files")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(args.once)


if __name__ == "__main__":
    main()
