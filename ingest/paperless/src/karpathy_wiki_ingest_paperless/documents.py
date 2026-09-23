"""Paperless document modelling, hashing, and source rendering."""

from __future__ import annotations

import datetime
import hashlib
import json
from collections import Counter
from typing import Any

SOURCE_FORMAT_VERSION = 2


def normalize_issued_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Paperless document creation date is not text")
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError("Paperless document creation date is not YYYY-MM-DD") from error


def source_hash(
    document: dict[str, Any],
    redaction_fingerprint: str,
    document_type: str | None,
    tags: list[str],
) -> str:
    selected = {
        "format_version": SOURCE_FORMAT_VERSION,
        "id": document.get("id"),
        "title": document.get("title") or "",
        "content": document.get("content") or "",
        "issued_date": document.get("created") or "",
        "document_type": document_type,
        "tags": tags,
        "redactions": redaction_fingerprint,
    }
    encoded = json.dumps(selected, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def render_document(
    document_id: int,
    title: str,
    content: str,
    public_url: str,
    counts: Counter[str],
    issued_date: str | None,
    document_type: str | None,
    tags: list[str],
    removed_person_tags: int,
    source_revision: str,
) -> str:
    source_url = f"{public_url}/documents/{document_id}"
    safe_title = " ".join(title.split())[:300] or f"Paperless-Dokument {document_id}"
    count_text = ", ".join(f"{kind}: {counts[kind]}" for kind in sorted(counts)) or "keine"
    document_type_field = json.dumps(document_type, ensure_ascii=False) if document_type else "null"
    return (
        "---\n"
        f"title: {json.dumps(safe_title, ensure_ascii=False)}\n"
        f"paperless_id: {document_id}\n"
        f"paperless_url: {json.dumps(source_url)}\n"
        f"source_revision: {json.dumps(source_revision)}\n"
        f"issued_date: {json.dumps(issued_date) if issued_date else 'null'}\n"
        f"document_type: {document_type_field}\n"
        f"tags: {json.dumps(tags, ensure_ascii=False)}\n"
        "anonymized: true\n"
        "---\n\n"
        f"# {safe_title}\n\n"
        f"[Dokument in Paperless öffnen]({source_url})\n\n"
        "## Gezielte Anonymisierung\n\n"
        f"Ersetzte konfigurierte Werte: {count_text}.\n\n"
        f"Entfernte Tags mit konfigurierten Personennamen: {removed_person_tags}.\n\n"
        "## Bereinigter OCR-Text\n\n"
        f"{content.strip()}\n"
    )
