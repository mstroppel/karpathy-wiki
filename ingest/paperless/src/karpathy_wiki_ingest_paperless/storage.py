"""Persistence layout for sanitized Paperless sources and revocations."""

from __future__ import annotations

import re
from pathlib import Path

from karpathy_wiki_ingest.shared import atomic_write

SOURCE_FILE_RE = re.compile(r"^document-(\d+)\.md$")
DOCUMENTS_PER_DIRECTORY = 1000
SOURCE_REVISION_RE = re.compile(r"(?m)^source_revision:\s*[\"']?([0-9a-f]{64})[\"']?\s*$")
REVOKED_ID_RE = re.compile(r"(?m)^- (\d+)$")
REVOKED_TITLE = "# Widerrufene Paperless-Dokumente"


def document_directory(document_id: int) -> str:
    start = document_id // DOCUMENTS_PER_DIRECTORY * DOCUMENTS_PER_DIRECTORY
    end = start + DOCUMENTS_PER_DIRECTORY - 1
    return f"{start:04d}-{end:04d}"


def source_document_path(root: Path, document_id: int) -> Path:
    return root / document_directory(document_id) / f"document-{document_id}.md"


def source_document_ids(root: Path) -> set[int]:
    ids = set()
    for path in root.glob("*/document-*.md"):
        match = SOURCE_FILE_RE.fullmatch(path.name)
        if match:
            ids.add(int(match.group(1)))
    return ids


def source_revision(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    match = SOURCE_REVISION_RE.search(text)
    return match.group(1) if match else None


def read_revoked_ids(path: Path) -> set[int]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    try:
        return parse_revoked_ids(text)
    except ValueError as error:
        raise ValueError(f"Invalid revoked document list: {path}") from error


def parse_revoked_ids(text: str) -> set[int]:
    """Parse revocation list content per the shared ingest status contract."""
    lines = text.splitlines()
    if not lines or lines[0] != REVOKED_TITLE:
        raise ValueError("Invalid revoked document list")
    ids = set()
    for line in lines[1:]:
        if not line:
            continue
        match = REVOKED_ID_RE.fullmatch(line)
        if not match:
            raise ValueError("Invalid revoked document list")
        ids.add(int(match.group(1)))
    return ids


def write_revoked_ids(path: Path, ids: set[int]) -> None:
    lines = [REVOKED_TITLE, ""]
    lines.extend(f"- {document_id}" for document_id in sorted(ids))
    lines.append("")
    atomic_write(path, "\n".join(lines))
