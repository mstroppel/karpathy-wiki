#!/usr/bin/env python3
"""Retag the exact images built by this CI run; never resolve mutable CI tags."""

import json
import os
import re
import subprocess
import sys
import time

IMAGES = (
    "opencode",
    "openchamber",
    "ingest",
    "ingest-webdav",
    "ingest-audio",
    "ingest-speech",
    "ingest-paperless",
    "ingest-email",
)


def main():
    version = os.environ.get("IMAGE_VERSION", "")
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?", version):
        raise ValueError("IMAGE_VERSION must be a release version")
    with open(sys.argv[1], encoding="utf-8") as source:
        metadata = json.load(source)
    commands = []
    # Validate the entire artifact before publishing any tags. Repository names
    # are fixed here; the artifact supplies only sha256 digests, never commands.
    for image in IMAGES:
        digest = metadata[f"{image}-release"]["containerimage.digest"]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise ValueError(f"Invalid image digest for {image}")
        repository = f"ghcr.io/mstroppel/karpathy-wiki-{image}"
        commands.append(
            [
                "docker",
                "buildx",
                "imagetools",
                "create",
                "--tag",
                f"{repository}:{version}",
                f"{repository}@{digest}",
            ]
        )
    for command in commands:
        for attempt in range(3):
            result = subprocess.run(command, check=False)
            if result.returncode == 0:
                break
            if attempt == 2:
                raise SystemExit(result.returncode)
            time.sleep((attempt + 1) * 30)


if __name__ == "__main__":
    main()
