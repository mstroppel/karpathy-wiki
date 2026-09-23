"""The Paperless ingest service: anonymization, publication, and revocation."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import shutil
import threading
import urllib.error
import uuid
from collections import Counter
from pathlib import Path
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
GENERATIONS = "generations"


def active_generation(root: Path) -> Path | None:
    pointer = root / "current"
    if not pointer.is_symlink():
        return None
    target = (root / os.readlink(pointer)).resolve()
    generations = (root / GENERATIONS).resolve()
    if target.parent != generations or not target.is_dir():
        raise ValueError("Paperless current pointer does not name a published generation")
    return target


def switch_generation(root: Path, generation: Path) -> None:
    pointer = root / "current"
    if pointer.exists() and not pointer.is_symlink():
        raise ValueError("Paperless current is reserved for the generation pointer")
    temporary = root / f".current.{uuid.uuid4().hex}"
    try:
        temporary.symlink_to(f"{GENERATIONS}/{generation.name}")
        os.replace(temporary, pointer)
    finally:
        temporary.unlink(missing_ok=True)


def same_sanitized_file(previous: Path, staging: Path, document_id: int) -> bool:
    try:
        return (
            source_document_path(previous, document_id).read_bytes()
            == source_document_path(staging, document_id).read_bytes()
        )
    except OSError:
        return False


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
        root = self.settings.sanitized_root
        with (root / ".ingest.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._run_locked()

    def _run_locked(self) -> tuple[int, int]:
        root = self.settings.sanitized_root
        if (root / MANIFEST_FILENAME).exists() and not (root / "current").is_symlink():
            raise ValueError(
                "Paperless flat source layout must be cleared manually before upgrading"
            )
        if (root / "revoked.md").exists() or source_document_ids(root):
            raise ValueError(
                "Paperless flat source layout must be cleared manually before upgrading"
            )
        generations = root / GENERATIONS
        generations.mkdir(exist_ok=True)
        previous = active_generation(root)
        # Staging directories are never published; discard interrupted builds.
        for abandoned in generations.glob(".staging-*"):
            if abandoned.is_dir():
                shutil.rmtree(abandoned)
        generation = generations / uuid.uuid4().hex
        staging = generations / f".staging-{generation.name}"
        staging.mkdir()
        try:
            return self._build_generation(staging, generation, previous)
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _build_generation(
        self, staging: Path, generation: Path, previous: Path | None
    ) -> tuple[int, int]:
        changed = 0
        failed = 0
        items: list[ManifestItem] = []
        errors: list[dict[str, str]] = []
        selected_ids = self.client.selected_document_ids()
        known_ids = source_document_ids(previous) if previous is not None else set()
        revoked_ids = read_revoked_ids(previous / "revoked.md") if previous is not None else set()
        for document_id in selected_ids:
            source_path = f"{document_directory(document_id)}/document-{document_id}.md"
            try:
                document_changed, item = self.process(
                    document_id, staging, previous, generation.name
                )
                items.append(item)
                if document_changed:
                    changed += 1
                revoked_ids.discard(document_id)
            except PrivacyValidationError as error:
                failed += 1
                self.quarantine(document_id, error)
                if document_id in known_ids:
                    revoked_ids.add(document_id)
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
                LOG.error("Document %s failed with %s", document_id, type(error).__name__)
                # Operational errors leave the last complete generation active.
                write_health(self.settings.health_path, failed)
                return 0, failed
        revoked_ids.update(known_ids - set(selected_ids))
        write_revoked_ids(staging / "revoked.md", revoked_ids)
        manifest = build_manifest(
            SOURCE_NAME,
            items,
            revoked=[
                {"source_key": str(document_id), "claim": {"paperless_id": str(document_id)}}
                for document_id in sorted(revoked_ids)
            ],
            errors=errors,
        )
        metadata = {
            "generation": generation.name,
            "redaction_fingerprint": self.anonymizer.fingerprint,
            "source_revisions": {item.source_key: item.source_revision for item in items},
        }
        atomic_write(staging / ".generation.json", json.dumps(metadata, sort_keys=True) + "\n")
        if previous is not None:
            try:
                old_metadata = json.loads((previous / ".generation.json").read_text())
                old_manifest = json.loads(
                    (self.settings.sanitized_root / MANIFEST_FILENAME).read_text()
                )
            except (OSError, ValueError):
                pass
            else:
                if (
                    isinstance(old_metadata, dict)
                    and isinstance(old_manifest, dict)
                    and old_metadata.get("redaction_fingerprint") == self.anonymizer.fingerprint
                    and old_metadata.get("source_revisions") == metadata["source_revisions"]
                    and isinstance(old_manifest.get("items"), list)
                    and all(isinstance(item, dict) for item in old_manifest["items"])
                    and [item.get("source_path") for item in old_manifest["items"]]
                    == [
                        item.source_path.replace(
                            f"{GENERATIONS}/{generation.name}/",
                            f"{GENERATIONS}/{previous.name}/",
                            1,
                        )
                        for item in items
                    ]
                    and all(
                        same_sanitized_file(previous, staging, int(item.source_key))
                        for item in items
                    )
                    and [
                        {key: value for key, value in item.items() if key != "source_path"}
                        for item in old_manifest["items"]
                    ]
                    == [
                        {key: value for key, value in item.items() if key != "source_path"}
                        for item in manifest["items"]
                    ]
                    and all(old_manifest.get(key) == manifest[key] for key in ("revoked", "errors"))
                ):
                    write_health(self.settings.health_path, failed)
                    return 0, failed
        staging.rename(generation)
        switched = False
        try:
            switch_generation(self.settings.sanitized_root, generation)
            switched = True
            write_manifest(self.settings.sanitized_root / MANIFEST_FILENAME, manifest)
        except BaseException:
            if switched:
                if previous is not None:
                    switch_generation(self.settings.sanitized_root, previous)
                else:
                    (self.settings.sanitized_root / "current").unlink(missing_ok=True)
            shutil.rmtree(generation)
            raise
        for obsolete in (self.settings.sanitized_root / GENERATIONS).iterdir():
            if obsolete != generation and obsolete.is_dir():
                shutil.rmtree(obsolete)
        write_health(self.settings.health_path, failed)
        LOG.info("Sync complete: %s changed, %s failed", changed, failed)
        return changed, failed

    def manifest_item(self, document_id: int, digest: str, generation: str) -> ManifestItem:
        relative = f"{document_directory(document_id)}/document-{document_id}.md"
        wiki_relative = f"{document_directory(document_id)}/paperless-{document_id}.md"
        return ManifestItem(
            source_key=str(document_id),
            source_path=f"{GENERATIONS}/{generation}/{relative}",
            wiki_path=wiki_relative,
            source_revision=digest,
            frontmatter={
                "paperless_id": document_id,
                "paperless_url": f"{self.settings.public_url}/documents/{document_id}",
                "source_revision": digest,
            },
            claim={"paperless_id": str(document_id)},
        )

    def process(
        self, document_id: int, staging: Path, previous: Path | None, generation: str
    ) -> tuple[bool, ManifestItem]:
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
        item = self.manifest_item(document_id, digest, generation)
        target = source_document_path(staging, document_id)
        old = source_document_path(previous, document_id) if previous is not None else None
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
        try:
            unchanged = (
                old is not None and source_revision(old) == digest and old.read_text() == output
            )
        except (OSError, UnicodeError):
            unchanged = False
        atomic_write(target, output)
        (self.settings.quarantine_root / f"document-{document_id}.txt").unlink(missing_ok=True)
        LOG.info("Document %s was anonymized", document_id)
        return not unchanged, item

    def quarantine(self, document_id: int, error: Exception) -> None:
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
