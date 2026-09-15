from __future__ import annotations

import argparse
import logging
import os
import subprocess
import time
from pathlib import Path

from .main import TargetedAnonymizer, atomic_write, required_env

LOG = logging.getLogger("karpathy-wiki-webdav")


def interval_seconds(value: str) -> int:
    units = {"s": 1, "m": 60, "h": 3600}
    try:
        amount, unit = float(value[:-1]), value[-1].lower()
        seconds = amount * units[unit]
    except (KeyError, ValueError, IndexError):
        seconds = float(value)
    if seconds <= 0:
        raise ValueError("WEBDAV_SYNC_INTERVAL must be greater than zero")
    return int(seconds)


def settings() -> tuple[Path, Path, Path, int, Path]:
    interval = interval_seconds(os.getenv("WEBDAV_SYNC_INTERVAL", "15m"))
    return (
        Path(os.getenv("WEBDAV_INCOMING_ROOT", "/data/incoming/webdav")),
        Path(os.getenv("WEBDAV_SANITIZED_ROOT", "/data/sanitized/webdav")),
        Path(os.getenv("WEBDAV_QUARANTINE_ROOT", "/data/quarantine/webdav")),
        interval,
        Path(required_env("REDACTIONS_FILE")),
    )


def sanitize_once(incoming: Path, sanitized: Path, quarantine: Path, anonymizer: TargetedAnonymizer) -> tuple[int, int]:
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
            atomic_write(quarantine / f"{relative}.error", f"path={relative}\nerror_type={type(error).__name__}\n")
            LOG.error("WebDAV file %s was quarantined", relative)
    for path in sorted((path for path in sanitized.rglob("*") if path.is_file()), reverse=True):
        if path.relative_to(sanitized) not in incoming_files:
            path.unlink()
    return changed, failed


def run(once: bool) -> None:
    incoming, sanitized, quarantine, interval, redactions = settings()
    anonymizer = TargetedAnonymizer.from_file(redactions)
    incoming.mkdir(parents=True, exist_ok=True)
    while True:
        subprocess.run(
            [
                "rclone",
                "sync",
                f"webdav:{os.environ['WEBDAV_PATH']}",
                str(incoming),
                "--create-empty-src-dirs",
                "--log-level",
                "INFO",
            ],
            check=True,
        )
        changed, failed = sanitize_once(incoming, sanitized, quarantine, anonymizer)
        LOG.info("WebDAV synchronization complete: %s changed, %s quarantined", changed, failed)
        if once:
            if failed:
                raise SystemExit(1)
            return
        time.sleep(interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Synchronize and anonymize WebDAV source files")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    run(args.once)


if __name__ == "__main__":
    main()
