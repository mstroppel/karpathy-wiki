"""Persistent opaque source identity for WebDAV audio paths.

Each WebDAV path is assigned a random opaque source ID the first time it is
seen; the mapping lives in private provider storage (a JSON file next to the
snapshot), never in the content-free shared job store and never derived from
redacted paths. Consequences:

- changing redaction rules never changes identity or merges distinct
  sources;
- a renamed path is a new source and the old one is revoked;
- identical audio at different paths keeps distinct source identities.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from karpathy_wiki_ingest.shared import atomic_write

MANIFEST_MAPPING_NAME = "sources.json"


def load_mapping(directory: Path) -> dict[str, str]:
    path = directory / MANIFEST_MAPPING_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        key: value
        for key, value in payload.items()
        if isinstance(key, str) and isinstance(value, str)
    }


def save_mapping(directory: Path, mapping: dict[str, str]) -> None:
    atomic_write(
        directory / MANIFEST_MAPPING_NAME,
        json.dumps(mapping, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        mode=0o600,
    )


def resolve_identity(
    directory: Path,
    relative: str,
    mapping: dict[str, str] | None = None,
) -> str:
    """Return the persistent source ID for one WebDAV path (assign if new).

    ``mapping`` may be passed by callers that already loaded it; the
    authoritative assignment is persisted immediately so a crash cannot
    reassign an ID that was already published.
    """
    own = mapping if mapping is not None else load_mapping(directory)
    source_id = own.get(relative)
    if source_id and is_valid_source_id(source_id):
        return source_id
    source_id = new_source_id()
    own[relative] = source_id
    save_mapping(directory, own)
    return source_id


def new_source_id() -> str:
    return uuid.UUID(bytes=os.urandom(16), version=4).hex


def is_valid_source_id(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 32 and all(c in "0123456789abcdef" for c in value)
    )
