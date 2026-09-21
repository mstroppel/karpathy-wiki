from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import os
import re
import signal
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..shared import (
    PrivacyValidationError,
    TargetedAnonymizer,
    atomic_write,
    required_env,
    write_health,
)

LOG = logging.getLogger("karpathy-wiki-ingest")
SOURCE_FORMAT_VERSION = 2
SOURCE_FILE_RE = re.compile(r"^document-(\d+)\.md$")
DOCUMENTS_PER_DIRECTORY = 1000
SOURCE_REVISION_RE = re.compile(r'(?m)^source_revision:\s*["\']?([0-9a-f]{64})["\']?\s*$')
REVOKED_ID_RE = re.compile(r"(?m)^- (\d+)$")
REVOKED_TITLE = "# Widerrufene Paperless-Dokumente"


class Anonymizer(Protocol):
    fingerprint: str

    def anonymize(self, text: str) -> tuple[str, Counter[str]]: ...

    def contains_person_name(self, text: str) -> bool: ...


class PaperlessSource(Protocol):
    def selected_document_ids(self) -> list[int]: ...

    def document(self, document_id: int) -> dict[str, Any]: ...

    def document_type_name(self, value: Any) -> str | None: ...

    def tag_names(self, values: Any) -> list[str]: ...


@dataclass(frozen=True)
class Settings:
    public_url: str
    source_tag_id: int
    token: str
    redactions_path: Path
    interval_seconds: int
    sanitized_root: Path
    quarantine_root: Path
    health_path: Path

    @classmethod
    def from_env(cls) -> Settings:
        token = read_secret("PAPERLESS_TOKEN", "PAPERLESS_TOKEN_FILE")
        source_tag_id = int(required_env("PAPERLESS_SOURCE_TAG_ID"))
        if source_tag_id <= 0:
            raise ValueError("PAPERLESS_SOURCE_TAG_ID must be greater than zero")
        interval_seconds = int(os.getenv("SYNC_INTERVAL_SECONDS", "900"))
        if interval_seconds <= 0:
            raise ValueError("SYNC_INTERVAL_SECONDS must be greater than zero")
        public_url = os.getenv("PAPERLESS_PUBLIC_URL", "https://paperless.rafatz.de").rstrip("/")
        if urllib.parse.urlsplit(public_url).scheme != "https":
            raise ValueError("PAPERLESS_PUBLIC_URL must use HTTPS")
        return cls(
            public_url=public_url,
            source_tag_id=source_tag_id,
            token=token,
            redactions_path=Path(required_env("REDACTIONS_FILE")),
            interval_seconds=interval_seconds,
            sanitized_root=Path(os.getenv("SANITIZED_ROOT", "/data/sanitized/paperless")),
            quarantine_root=Path(os.getenv("QUARANTINE_ROOT", "/data/quarantine/paperless")),
            health_path=Path(os.getenv("HEALTH_PATH", "/tmp/health.json")),
        )


def read_secret(value_name: str, file_name: str) -> str:
    direct = os.getenv(value_name, "").strip()
    if direct:
        return direct
    path = required_env(file_name)
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"secret file for {value_name} is empty")
    return value


class PaperlessClient:
    def __init__(self, public_url: str, token: str, source_tag_id: int) -> None:
        self.public_url = public_url.rstrip("/")
        self.source_tag_id = source_tag_id
        self.headers = {
            "Accept": "application/json; version=10",
            "Authorization": f"Token {token}",
            "User-Agent": "karpathy-wiki-ingest/1",
        }
        self.resource_names: dict[tuple[str, int], str] = {}

    def selected_document_ids(self) -> list[int]:
        self.resource_names.clear()
        document_ids: set[int] = set()
        page = 1
        while True:
            query = urllib.parse.urlencode(
                {
                    "tags__id__all": self.source_tag_id,
                    "ordering": "id",
                    "page": page,
                    "page_size": 100,
                }
            )
            payload = self._get_json(f"{self.public_url}/api/documents/?{query}")
            results = payload.get("results")
            if not isinstance(results, list):
                raise ValueError("Paperless document list has no results array")
            try:
                document_ids.update(int(item["id"]) for item in results)
            except (KeyError, TypeError, ValueError) as error:
                raise ValueError("Paperless document list has malformed entries") from error
            if not payload.get("next"):
                break
            page += 1
        return sorted(document_ids)

    def document(self, document_id: int) -> dict[str, Any]:
        payload = self._get_json(f"{self.public_url}/api/documents/{document_id}/")
        if int(payload.get("id", -1)) != document_id:
            raise ValueError(f"Paperless returned the wrong document for {document_id}")
        return payload

    def document_type_name(self, value: Any) -> str | None:
        if value is None:
            return None
        return self._resource_name("document_types", value)

    def tag_names(self, values: Any) -> list[str]:
        if not isinstance(values, list):
            raise ValueError("Paperless document tags are not an array")
        return [self._resource_name("tags", value) for value in values]

    def _resource_name(self, resource: str, value: Any) -> str:
        if isinstance(value, dict):
            name = value.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Paperless {resource} entry has no name")
            return " ".join(name.split())
        try:
            resource_id = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Paperless {resource} entry has no numeric ID") from error
        key = (resource, resource_id)
        if key not in self.resource_names:
            payload = self._get_json(f"{self.public_url}/api/{resource}/{resource_id}/")
            name = payload.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Paperless {resource} entry has no name")
            self.resource_names[key] = " ".join(name.split())
        return self.resource_names[key]

    def _get_json(self, url: str) -> dict[str, Any]:
        return self._request_json(url)

    def _request_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise ValueError("Paperless returned a non-object JSON response")
        return payload


def resource_id(value: Any, resource: str) -> int:
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Paperless {resource} entry has no numeric ID") from error


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


class Ingestor:
    def __init__(self, settings: Settings, anonymizer: Anonymizer) -> None:
        self.settings = settings
        self.anonymizer = anonymizer
        self.client: PaperlessSource = PaperlessClient(
            settings.public_url, settings.token, settings.source_tag_id
        )
        settings.sanitized_root.mkdir(parents=True, exist_ok=True)
        settings.quarantine_root.mkdir(parents=True, exist_ok=True)

    def run_once(self) -> tuple[int, int]:
        changed = 0
        failed = 0
        selected_ids = self.client.selected_document_ids()
        known_ids = source_document_ids(self.settings.sanitized_root)
        for document_id in selected_ids:
            try:
                if self.process(document_id):
                    changed += 1
            except PrivacyValidationError as error:
                failed += 1
                self.quarantine(document_id, error)
                LOG.error("Document %s failed privacy validation", document_id)
            except Exception as error:
                failed += 1
                self.record_error(document_id, error)
                LOG.error("Document %s failed with %s", document_id, type(error).__name__)
        self.reconcile(set(selected_ids), known_ids)
        write_health(self.settings.health_path, failed)
        LOG.info("Sync complete: %s changed, %s failed", changed, failed)
        return changed, failed

    def process(self, document_id: int) -> bool:
        document = self.client.document(document_id)
        issued_date = normalize_issued_date(document.get("created"))
        document_type = self.client.document_type_name(document.get("document_type"))
        content_tag_values = [
            value
            for value in document.get("tags", [])
            if resource_id(value, "tag") != self.settings.source_tag_id
        ]
        tag_names = sorted(self.client.tag_names(content_tag_values))
        digest = source_hash(document, self.anonymizer.fingerprint, document_type, tag_names)
        target = source_document_path(self.settings.sanitized_root, document_id)
        if source_revision(target) == digest:
            self.set_revoked(document_id, False)
            return False
        content = document.get("content") or ""
        if not isinstance(content, str) or not content.strip():
            raise PrivacyValidationError("Paperless document has no OCR text")
        title = document.get("title") or ""
        if not isinstance(title, str):
            raise ValueError("Paperless document title is not text")
        safe_title, title_counts = self.anonymizer.anonymize(title)
        safe_content, content_counts = self.anonymizer.anonymize(content)
        safe_document_type = None
        metadata_counts: Counter[str] = Counter()
        if document_type:
            safe_document_type, metadata_counts = self.anonymizer.anonymize(document_type)
        safe_tags: list[str] = []
        removed_person_tags = 0
        for tag_name in tag_names:
            if self.anonymizer.contains_person_name(tag_name):
                removed_person_tags += 1
                continue
            safe_tag, tag_counts = self.anonymizer.anonymize(tag_name)
            safe_tags.append(safe_tag)
            metadata_counts.update(tag_counts)
        output = render_document(
            document_id,
            safe_title,
            safe_content,
            self.settings.public_url,
            title_counts + content_counts + metadata_counts,
            issued_date,
            safe_document_type,
            safe_tags,
            removed_person_tags,
            digest,
        )
        atomic_write(target, output)
        self.set_revoked(document_id, False)
        (self.settings.quarantine_root / f"document-{document_id}.txt").unlink(missing_ok=True)
        LOG.info("Document %s was anonymized", document_id)
        return True

    def quarantine(self, document_id: int, error: Exception) -> None:
        target = source_document_path(self.settings.sanitized_root, document_id)
        if target.is_file():
            self.set_revoked(document_id, True)
        target.unlink(missing_ok=True)
        message = (
            f"document_id={document_id}\n"
            f"category=privacy-validation\nerror_type={type(error).__name__}\n"
        )
        atomic_write(self.settings.quarantine_root / f"document-{document_id}.txt", message)

    def record_error(self, document_id: int, error: Exception) -> None:
        message = (
            f"document_id={document_id}\ncategory=operational\nerror_type={type(error).__name__}\n"
        )
        atomic_write(self.settings.quarantine_root / f"document-{document_id}.txt", message)

    def reconcile(self, selected_ids: set[int], known_ids: set[int]) -> None:
        revoked_path = self.settings.sanitized_root / "revoked.md"
        revoked_ids = read_revoked_ids(revoked_path)
        removed_ids = known_ids - selected_ids
        revoked_ids.update(removed_ids)
        if removed_ids or not revoked_path.exists():
            write_revoked_ids(revoked_path, revoked_ids)
        for document_id in removed_ids:
            source_document_path(self.settings.sanitized_root, document_id).unlink(missing_ok=True)
            LOG.warning("Document %s was revoked because its tag was removed", document_id)

    def set_revoked(self, document_id: int, revoked: bool) -> None:
        path = self.settings.sanitized_root / "revoked.md"
        ids = read_revoked_ids(path)
        if (document_id in ids) == revoked:
            return
        if revoked:
            ids.add(document_id)
        else:
            ids.discard(document_id)
        write_revoked_ids(path, ids)


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
    lines = text.splitlines()
    if not lines or lines[0] != REVOKED_TITLE:
        raise ValueError(f"Invalid revoked document list: {path}")
    ids = set()
    for line in lines[1:]:
        if not line:
            continue
        match = REVOKED_ID_RE.fullmatch(line)
        if not match:
            raise ValueError(f"Invalid revoked document list: {path}")
        ids.add(int(match.group(1)))
    return ids


def write_revoked_ids(path: Path, ids: set[int]) -> None:
    lines = [REVOKED_TITLE, ""]
    lines.extend(f"- {document_id}" for document_id in sorted(ids))
    lines.append("")
    atomic_write(path, "\n".join(lines))


def run_continuously(ingestor: Ingestor, settings: Settings, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            ingestor.anonymizer = TargetedAnonymizer.from_file(settings.redactions_path)
            ingestor.run_once()
        except (urllib.error.URLError, OSError, ValueError):
            LOG.exception("Paperless synchronization failed")
            write_health(settings.health_path, 1)
        if stop_event.wait(settings.interval_seconds):
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="Redact tagged Paperless documents")
    parser.add_argument("--once", action="store_true", help="run one synchronization")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    anonymizer = TargetedAnonymizer.from_file(settings.redactions_path)
    ingestor = Ingestor(settings, anonymizer)
    if arguments.once:
        _, failed = ingestor.run_once()
        if failed:
            raise SystemExit(1)
        return
    stop_event = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stop_event.set())
    run_continuously(ingestor, settings, stop_event)
