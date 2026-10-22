"""Bounded local XLSX extraction; only the parent crosses the redaction boundary.

The parser runs in a disposable, resource-limited process. Its stdout is a bounded
private temporary file, never a log. Workbook-derived strings remain separate
from Markdown syntax until the parent redacts them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from karpathy_wiki_ingest.shared import TargetedAnonymizer

EXTRACTION_VERSION = 1  # Bump for extraction, rendering, dependency or limit changes.
SPREADSHEET_SUFFIXES = {".xlsx", ".xls", ".xlsb", ".xlsm"}
MAX_COMPRESSED = 20 * 1024 * 1024
MAX_UNCOMPRESSED = 100 * 1024 * 1024
MAX_OUTPUT = 4 * 1024 * 1024
MAX_SHEETS = 32
MAX_ROWS = 10_000
MAX_COLUMNS = 256
MAX_EXAMINED_ROWS = 50_000
MAX_CELLS = 200_000
MAX_MERGES = 2_000
WALL_SECONDS = 45

DIAGNOSTICS = {
    "unsupported": "Export as an unencrypted .xlsx workbook; other Excel formats are unsupported.",
    "invalid": "Invalid or encrypted workbook; open it locally and save an unencrypted .xlsx copy.",
    "limits": "Workbook exceeds extraction limits; split it into smaller workbooks before upload.",
    "resources": "Workbook exceeded parser resources; simplify or split it before upload.",
    "formula": "Unsupported array/data-table formula; use simple formulas or static values.",
    "visibility": "Default-hidden layout unsupported; copy visible data to a new worksheet.",
}
EXIT_CODES = {
    "invalid": 2,
    "limits": 3,
    "unsupported": 4,
    "resources": 5,
    "formula": 6,
    "visibility": 7,
}


class WorkbookError(ValueError):
    """A fixed, content-free diagnostic safe for quarantine reports."""

    def __init__(self, code: str):
        self.code = code if code in DIAGNOSTICS else "invalid"
        super().__init__(DIAGNOSTICS[self.code])


def identity(relative: Path) -> str:
    return hashlib.sha256(relative.as_posix().encode("utf-8")).hexdigest()


def output_path(relative: Path) -> Path:
    return Path("xlsx") / identity(relative) / "workbook.md"


def escape(text: str) -> str:
    """Escape inline Markdown/HTML, keeping table rows and cells intact."""
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"([\\`*_{}\[\]()#+.!|~\-])", r"\\\1", text)
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br>")


def render(segments: list[Any], relative: Path, anonymizer: TargetedAnonymizer) -> str:
    def clean(text: str) -> str:
        return escape(anonymizer.anonymize(text)[0])

    parts = [f"# Workbook: {clean(relative.as_posix())}\n\n"]
    size = len(parts[0].encode("utf-8"))
    for segment in segments:
        # The subprocess emits only these two fields; fail closed on a broken protocol.
        if (
            not isinstance(segment, list)
            or len(segment) != 2
            or not isinstance(segment[0], bool)
            or not isinstance(segment[1], str)
        ):
            raise WorkbookError("invalid")
        text = clean(segment[1]) if segment[0] else segment[1]
        size += len(text.encode("utf-8"))
        if size > MAX_OUTPUT:
            raise WorkbookError("limits")
        parts.append(text)
    return "".join(parts)


def extract(source: Path, relative: Path, anonymizer: TargetedAnonymizer) -> str:
    if source.suffix.lower() != ".xlsx":
        raise WorkbookError("unsupported")
    if source.stat().st_size > MAX_COMPRESSED:
        raise WorkbookError("limits")
    # File-backed stdout avoids an unbounded PIPE allocation in the provider.
    with tempfile.TemporaryFile() as result:
        try:
            process = subprocess.run(
                [sys.executable, "-m", "karpathy_wiki_ingest_webdav.xlsx_worker", str(source)],
                stdin=subprocess.DEVNULL,
                stdout=result,
                stderr=subprocess.DEVNULL,
                timeout=WALL_SECONDS,
                env={**os.environ, "OPENPYXL_LXML": "False", "OPENPYXL_DEFUSEDXML": "True"},
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise WorkbookError("resources") from None
        if process.returncode != 0:
            code = {status: code for code, status in EXIT_CODES.items()}.get(
                process.returncode, "resources"
            )
            raise WorkbookError(code)
        result.seek(0)
        raw = result.read(MAX_OUTPUT + 1)
    if len(raw) > MAX_OUTPUT:
        raise WorkbookError("limits")
    try:
        segments = json.loads(raw)
    except (ValueError, UnicodeError):
        raise WorkbookError("invalid") from None
    if not isinstance(segments, list):
        raise WorkbookError("invalid")
    return render(segments, relative, anonymizer)
