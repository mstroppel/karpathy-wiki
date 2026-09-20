from __future__ import annotations

import argparse
import logging
import os
import signal
import subprocess
import threading
from pathlib import Path

from ..shared import (
    TargetedAnonymizer,
    atomic_write,
    interval_seconds,
    required_env,
    write_health,
)

LOG = logging.getLogger("karpathy-wiki-webdav")


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
                f"path={relative}\nerror_type={type(error).__name__}\n",
            )
            LOG.error("WebDAV file %s was quarantined", relative)
    for path in sorted((path for path in sanitized.rglob("*") if path.is_file()), reverse=True):
        if path.relative_to(sanitized) not in incoming_files:
            path.unlink()
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
    incoming, sanitized, quarantine, interval, redactions, health_path = settings()
    anonymizer = TargetedAnonymizer.from_file(redactions)
    stop_event = install_stop_handler()
    incoming.mkdir(parents=True, exist_ok=True)
    while not stop_event.is_set():
        failed = 0
        try:
            synchronize(incoming, os.environ["WEBDAV_PATH"])
            changed, failed = sanitize_once(incoming, sanitized, quarantine, anonymizer)
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
