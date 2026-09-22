from __future__ import annotations

import argparse
import hashlib
import logging
import os
import posixpath
import signal
import subprocess
import threading
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


def settings() -> tuple[Path, Path, Path, int, Path, Path]:
    interval = interval_seconds(os.getenv("WEBDAV_SYNC_INTERVAL", "15m"))
    return (
        Path(os.getenv("WEBDAV_INCOMING_ROOT", "/data/incoming/webdav")),
        Path(os.getenv("WEBDAV_SANITIZED_ROOT", "/data/sanitized/webdav")),
        Path(os.getenv("WEBDAV_QUARANTINE_ROOT", "/data/quarantine/webdav")),
        interval,
        Path(required_env("REDACTIONS_FILE")),
        Path(os.getenv("HEALTH_PATH", "/tmp/health.json")),
    )


def sanitize_once(
    incoming: Path, sanitized: Path, quarantine: Path, anonymizer: TargetedAnonymizer
) -> tuple[int, int]:
    incoming_files = {path.relative_to(incoming) for path in incoming.rglob("*") if path.is_file()}
    changed = failed = 0
    sanitized.mkdir(parents=True, exist_ok=True)
    quarantine.mkdir(parents=True, exist_ok=True)
    for relative in sorted(incoming_files):
        source = incoming / relative
        target = sanitized / relative
        if relative == Path(MANIFEST_FILENAME):
            # The manifest name is reserved at the source root; never publish
            # upstream content under it.
            failed += 1
            target.unlink(missing_ok=True)
            atomic_write(
                quarantine / f"{MANIFEST_FILENAME}.error",
                f"path={MANIFEST_FILENAME}\nerror_type=ReservedManifestName\n",
            )
            LOG.error(
                "WebDAV file %s uses the reserved manifest name and was quarantined", relative
            )
            continue
        try:
            content = source.read_text(encoding="utf-8")
            output, _ = anonymizer.anonymize(content)
            if not target.is_file() or target.read_text(encoding="utf-8") != output:
                atomic_write(target, output)
                changed += 1
            (quarantine / f"{relative}.error").unlink(missing_ok=True)
        except (OSError, UnicodeError, ValueError) as error:
            failed += 1
            target.unlink(missing_ok=True)
            atomic_write(
                quarantine / f"{relative}.error",
                f"path={relative.as_posix()}\nerror_type={type(error).__name__}\n",
            )
            LOG.error("WebDAV file %s was quarantined", relative)
    for path in sorted((path for path in sanitized.rglob("*") if path.is_file()), reverse=True):
        relative = path.relative_to(sanitized)
        if relative == Path(MANIFEST_FILENAME):
            continue
        if relative not in incoming_files:
            path.unlink()
            (quarantine / f"{relative}.error").unlink(missing_ok=True)
    # Reports for sources that no longer exist upstream must not keep failing
    # the manifest: drop reports whose relative source is not synchronized. A
    # reserved-name report is recreated by the sanitize loop on each cycle
    # while the upstream file is present.
    for report in sorted(quarantine.rglob("*.error")):
        source_relative = report.relative_to(quarantine)
        if source_relative.with_suffix("") not in incoming_files:
            report.unlink(missing_ok=True)
    return changed, failed


def build_manifest_items(sanitized: Path) -> list[ManifestItem]:
    items = []
    for path in sorted(path for path in sanitized.rglob("*") if path.is_file()):
        relative = path.relative_to(sanitized).as_posix()
        if relative == MANIFEST_FILENAME:
            continue
        content = path.read_text(encoding="utf-8")
        revision = hashlib.sha256(content.encode("utf-8")).hexdigest()
        items.append(
            ManifestItem(
                source_key=relative,
                source_path=relative,
                wiki_path=posixpath.join(WIKI_ROOT, relative, "index.md"),
                source_revision=revision,
                frontmatter={
                    "source_adapter": SOURCE_NAME,
                    "source_path": relative,
                    "source_revision": revision,
                },
                claim={"source_path": relative},
            )
        )
    return items


def collect_quarantine_errors(quarantine: Path) -> list[dict[str, str]]:
    errors = []
    for report in sorted(path for path in quarantine.rglob("*.error") if path.is_file()):
        fields = {}
        for line in report.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                fields[key] = value
        error = {"error": fields.get("error_type", "Error")}
        if fields.get("path"):
            error["path"] = fields["path"]
        errors.append(error)
    return errors


def write_source_manifest(sanitized: Path, quarantine: Path) -> None:
    manifest = build_manifest(
        SOURCE_NAME,
        build_manifest_items(sanitized),
        errors=collect_quarantine_errors(quarantine),
        wiki_root=WIKI_ROOT,
    )
    write_manifest(sanitized / MANIFEST_FILENAME, manifest)


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
    incoming, sanitized, quarantine, interval, redactions, health_path = settings()
    anonymizer = TargetedAnonymizer.from_file(redactions)
    stop_event = install_stop_handler()
    incoming.mkdir(parents=True, exist_ok=True)
    while not stop_event.is_set():
        failed = 0
        try:
            synchronize(incoming, os.environ["WEBDAV_PATH"])
            changed, failed = sanitize_once(incoming, sanitized, quarantine, anonymizer)
            write_source_manifest(sanitized, quarantine)
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
