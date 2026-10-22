"""Private snapshot connector: rclone discovery of WebDAV audio files.

The audio provider keeps its own raw-audio directory; it never publishes raw
files under ``sources/``. A cycle mirrors only supported audio extensions
into the snapshot directory, then inventories them as pending work.
Everything else is excluded from synchronization
(Markdown-only files belong to the Markdown WebDAV provider).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from karpathy_wiki_ingest.rclone import suffix_filter_args
from karpathy_wiki_speech.types import DEFAULT_LIMITS, TranscriptionLimits, file_revision


def probe(path: str, rclone_binary: str = "rclone") -> None:
    """Prove remote access before startup readiness, without listing private names."""
    subprocess.run(
        [rclone_binary, "lsf", f"webdav:{path}", "--max-depth", "1", "--retries", "1"],
        check=True,
        timeout=60,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def synchronize(
    incoming: Path,
    path: str,
    rclone_binary: str = "rclone",
    *,
    timeout_seconds: int = 3600,
    limits: TranscriptionLimits = DEFAULT_LIMITS,
) -> None:
    """Mirror supported audio files into the private snapshot."""
    subprocess.run(
        [
            rclone_binary,
            "sync",
            f"webdav:{path}",
            str(incoming),
            *suffix_filter_args(limits.allowed_extensions),
            "--create-empty-src-dirs",
            "--retries",
            "3",
            "--low-level-retries",
            "10",
            "--log-level",
            "INFO",
        ],
        check=True,
        timeout=timeout_seconds,
    )


def audio_inventory(
    snapshot: Path,
    limits: TranscriptionLimits,
    progress: Callable[[], None] | None = None,
) -> dict[str, str]:
    """Content hashes of every supported audio file in the snapshot.

    The returned mapping is keyed by the stable WebDAV-relative path. Files
    with unsupported extensions are ignored entirely (they are either
    upstream Markdown for the other provider or arbitrary private files);
    supported recordings are hashed so idempotent discovery can compare
    cycles by content.
    """
    inventory: dict[str, str] = {}
    for path in sorted(snapshot.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in limits.allowed_extensions:
            continue
        relative = path.relative_to(snapshot).as_posix()
        if progress is not None:
            progress()
        inventory[relative] = file_revision(str(path))
    return inventory
