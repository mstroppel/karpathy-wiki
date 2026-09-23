"""The Paperless ingest service: anonymization, publication, and revocation."""

from __future__ import annotations

import logging
import threading
import urllib.error
from collections import Counter
from typing import Protocol

from karpathy_wiki_ingest.manifest import (
    MANIFEST_FILENAME,
    ManifestItem,
    build_manifest,
    write_manifest,
)
from karpathy_wiki_ingest.shared import (
    PrivacyValidationError,
    TargetedAnonymizer,
    atomic_write,
    write_health,
)

from .client import PaperlessClient, PaperlessSource, resource_id
from .config import Settings
from .documents import normalize_issued_date, render_document, source_hash
from .storage import (
    document_directory,
    read_revoked_ids,
    source_document_ids,
    source_document_path,
    source_revision,
    write_revoked_ids,
)

LOG = logging.getLogger("karpathy-wiki-ingest")
SOURCE_NAME = "paperless"


class Anonymizer(Protocol):
    fingerprint: str

    def anonymize(self, text: str) -> tuple[str, Counter[str]]: ...

    def contains_person_name(self, text: str) -> bool: ...


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
        items: list[ManifestItem] = []
        errors: list[dict[str, str]] = []
        selected_ids = self.client.selected_document_ids()
        known_ids = source_document_ids(self.settings.sanitized_root)
        for document_id in selected_ids:
            source_path = f"{document_directory(document_id)}/document-{document_id}.md"
            try:
                document_changed, item = self.process(document_id)
                items.append(item)
                if document_changed:
                    changed += 1
            except PrivacyValidationError as error:
                failed += 1
                self.quarantine(document_id, error)
                errors.append(
                    {
                        "source_key": str(document_id),
                        "path": source_path,
                        "error": f"privacy-validation:{type(error).__name__}",
                    }
                )
                LOG.error("Document %s failed privacy validation", document_id)
            except Exception as error:
                failed += 1
                self.record_error(document_id, error)
                errors.append(
                    {
                        "source_key": str(document_id),
                        "path": source_path,
                        "error": f"operational:{type(error).__name__}",
                    }
                )
                LOG.error("Document %s failed with %s", document_id, type(error).__name__)
        self.reconcile(set(selected_ids), known_ids)
        revoked_ids = read_revoked_ids(self.settings.sanitized_root / "revoked.md")
        write_manifest(
            self.settings.sanitized_root / MANIFEST_FILENAME,
            build_manifest(
                SOURCE_NAME,
                items,
                revoked=[
                    {"source_key": str(document_id), "claim": {"paperless_id": str(document_id)}}
                    for document_id in sorted(revoked_ids)
                ],
                errors=errors,
            ),
        )
        write_health(self.settings.health_path, failed)
        LOG.info("Sync complete: %s changed, %s failed", changed, failed)
        return changed, failed

    def manifest_item(self, document_id: int, digest: str) -> ManifestItem:
        relative = f"{document_directory(document_id)}/document-{document_id}.md"
        wiki_relative = f"{document_directory(document_id)}/paperless-{document_id}.md"
        return ManifestItem(
            source_key=str(document_id),
            source_path=relative,
            wiki_path=wiki_relative,
            source_revision=digest,
            frontmatter={
                "paperless_id": document_id,
                "paperless_url": f"{self.settings.public_url}/documents/{document_id}",
                "source_revision": digest,
            },
            claim={"paperless_id": str(document_id)},
        )

    def process(self, document_id: int) -> tuple[bool, ManifestItem]:
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
        item = self.manifest_item(document_id, digest)
        target = source_document_path(self.settings.sanitized_root, document_id)
        if source_revision(target) == digest:
            self.set_revoked(document_id, False)
            return False, item
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
        return True, item

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
