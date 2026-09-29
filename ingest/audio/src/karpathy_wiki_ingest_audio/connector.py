"""Private snapshot connector: rclone discovery of WebDAV audio files.

The audio provider keeps its own raw-audio directory; it never publishes raw
files under ``sources/``. A cycle first mirrors the complete upstream
inventory into the snapshot directory, then classifies files: supported
audio extensions become pending work, everything else is ignored
(Markdown-only files belong to the Markdown WebDAV provider).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from karpathy_wiki_speech.types import TranscriptionLimits


def synchronize(incoming: Path, path: str, rclone_binary: str = "rclone") -> None:
    """Mirror the upstream WebDAV folder into the private snapshot."""
    subprocess.run(
        [
            rclone_binary,
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


def audio_inventory(
    snapshot: Path,
    limits: TranscriptionLimits,
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
        inventory[relative] = file_revision(path)
    return inventory


def file_revision(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()
