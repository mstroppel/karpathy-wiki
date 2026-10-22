"""Shared private-snapshot filters for the WebDAV-backed providers."""

from collections.abc import Iterable


def suffix_filter_args(suffixes: Iterable[str]) -> list[str]:
    """Match discovery's case-insensitive Path.suffix selection recursively."""
    # Path.suffix ignores bare suffixes and all-dot stems. Require a non-dot
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
