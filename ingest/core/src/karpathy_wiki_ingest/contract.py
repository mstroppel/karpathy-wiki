"""Shared ingest status contract.

The versioned contract document and its conformance fixtures live in
``contracts/ingest-status/`` in the repository. The constants here mirror the
contract document; the fixture-driven tests in the ingest packages and the
JavaScript scanner tests keep every runtime in agreement.
"""

from __future__ import annotations

import json
import re
from typing import Any

REVISION_RE = re.compile(r"^[0-9a-f]{64}$")

STATUS_VALUES = ("new", "outdated", "current", "conflict", "revoked", "orphaned", "invalid")

_FRONTMATTER_FIELD_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*?)\s*$")


def parse_frontmatter_fields(text: str) -> dict[str, Any]:
    """Parse the frontmatter block of a source or wiki page.

    Mirrors the JavaScript adapter parsing: the block is delimited by ``---``
    lines, field names are unique, and values are plain text, single-quoted
    text, or a JSON-encoded value (a leading double quote). Raises
    ``ValueError`` for a missing or unclosed block, a duplicate field name, or
    an invalid JSON value.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or lines[0] != "---":
        raise ValueError("fehlendes Frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as error:
        raise ValueError("nicht abgeschlossenes Frontmatter") from error

    fields: dict[str, Any] = {}
    for line in lines[1:end]:
        match = _FRONTMATTER_FIELD_RE.fullmatch(line)
        if not match:
            raise ValueError(f"ungültige Frontmatter-Zeile: {line}")
        name, value = match.group(1), match.group(2)
        if name in fields:
            raise ValueError(f"{name} ist mehrfach vorhanden")
        if value.startswith('"'):
            try:
                fields[name] = json.loads(value)
            except json.JSONDecodeError as error:
                raise ValueError(f"{name} enthält keine gültige Zeichenkette") from error
        elif len(value) >= 2 and value.startswith("'") and value.endswith("'"):
            fields[name] = value[1:-1]
        else:
            fields[name] = value
    return fields
