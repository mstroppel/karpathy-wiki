"""Shared private-snapshot filters for the WebDAV-backed providers."""

from collections.abc import Iterable
from pathlib import Path


def has_supported_suffix(path: Path, suffixes: Iterable[str]) -> bool:
    """Select supported basenames with a non-dot stem on every supported Python."""
    # Python 3.12/3.13 consider '..md' to have suffix '.md'; 3.14 does not.
    return bool(path.stem.strip(".")) and path.suffix.lower() in suffixes


def suffix_filter_args(suffixes: Iterable[str]) -> list[str]:
    """Match has_supported_suffix recursively and case-insensitively."""
    # Discovery ignores bare suffixes and all-dot stems. Require a non-dot
    # character in the basename before the suffix. A raw regex keeps slashes
    # out of character classes without rclone treating them as glob separators.
    return [
        "--ignore-case",
        *[
            flag
            for suffix in sorted(suffixes)
            for flag in ("--filter", "+ {{[^/]*[^/.][^/]*}}" + suffix)
        ],
        "--filter",
        "- *",
    ]
