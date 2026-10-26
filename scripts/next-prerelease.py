#!/usr/bin/env python3
"""Choose the next numeric pre-release after all existing tags for a base version."""

import re
import subprocess
import sys


def next_prerelease(base: str, tags: list[str]) -> str:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", base):
        raise ValueError("Expected a base version in x.y.z form")
    pattern = re.compile(rf"v{re.escape(base)}-pre\.(0|[1-9][0-9]*)")
    counters = [int(match[1]) for tag in tags if (match := pattern.fullmatch(tag))]
    return f"{base}-pre.{max(counters, default=-1) + 1}"


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("Usage: next-prerelease.py BASE_VERSION")
    tags = subprocess.run(
        ["git", "tag", "--list"], check=True, capture_output=True, text=True
    ).stdout.splitlines()
    print(next_prerelease(sys.argv[1], tags))


if __name__ == "__main__":
    main()
