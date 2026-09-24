"""The Paperless ingest service: anonymization, publication, and revocation."""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import shutil
import socket
import sqlite3
import threading
import time
import urllib.error
import uuid
from collections import Counter
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Protocol

from karpathy_wiki_ingest.manifest import (
    MANIFEST_FILENAME,
    ManifestItem,
    build_manifest,
    validate_manifest,
    write_manifest,
)
from karpathy_wiki_ingest.shared import (
    PrivacyValidationError,
    TargetedAnonymizer,
    atomic_write,
    write_health,
)
from karpathy_wiki_ingest.state import (
    JOB_DEAD,
    JOB_SUCCEEDED,
    JOB_SUPERSEDED,
    StateError,
    StateStore,
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
PreparedDocument = tuple[dict[str, Any], str | None, list[str]]


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

    def prepare_document(self, document_id: int) -> PreparedDocument:
        document = self.client.document(document_id)
        document_type = self.client.document_type_name(document.get("document_type"))
        tags = [
            value
            for value in document.get("tags", [])
            if resource_id(value, "tag") != self.settings.source_tag_id
        ]
        return document, document_type, sorted(self.client.tag_names(tags))

    def snapshot(self) -> tuple[list[int], dict[str, str]]:
        """Identify upstream work with bounded memory; never persist raw documents."""
        selected = self.client.selected_document_ids()
        revisions = {}
        for document_id in selected:
            document, document_type, tags = self.prepare_document(document_id)
            revisions[str(document_id)] = source_hash(
                document, self.anonymizer.fingerprint, document_type, tags
            )
        return selected, revisions

    def cycle_key(self, revisions: dict[str, str]) -> str:
        identity = json.dumps(
            {
                "revisions": revisions,
                "redactions": self.anonymizer.fingerprint,
                "tag": self.settings.source_tag_id,
                "public_url": self.settings.public_url,
            },
            sort_keys=True,
        ).encode()
        return f"paperless-generation:{hashlib.sha256(identity).hexdigest()}"

    def published_matches(self, revisions: dict[str, str]) -> bool:
        root = self.settings.sanitized_root
        generation = active_generation(root)
        if generation is None:
            return False
        try:
            metadata = json.loads((generation / ".generation.json").read_text())
            manifest = validate_manifest(
                json.loads((root / MANIFEST_FILENAME).read_text()), SOURCE_NAME
            )
        except (OSError, ValueError):
            return False
        if (
            not isinstance(metadata, dict)
            or not isinstance(metadata.get("source_revisions"), dict)
            or metadata.get("generation") != generation.name
            or metadata.get("manifest_digest") != self.manifest_digest(manifest)
            or metadata.get("redaction_fingerprint") != self.anonymizer.fingerprint
            or metadata.get("input_revisions") != revisions
            or metadata.get("public_url") != self.settings.public_url
            or metadata.get("source_tag_id") != self.settings.source_tag_id
        ):
            return False
        items = manifest["items"]
        for item in items:
            source_key = item["source_key"]
            if not source_key.isdecimal():
                return False
            relative = source_document_path(Path("."), int(source_key)).as_posix()
            if item["source_path"] != f"{GENERATIONS}/{generation.name}/{relative}":
                return False
            if item["source_revision"] != metadata["source_revisions"].get(source_key):
                return False
            if not (generation / relative).is_file():
                return False
        return len(items) == len(source_document_ids(generation))

    @staticmethod
    def manifest_digest(manifest: dict[str, Any]) -> str:
        """Digest binding a manifest to the generation it was written for.

        The published manifest must be the one built alongside the active
        generation. Item paths distinguish generations only when the manifest
        has items, so the digest is the binding that also works for empty
        manifests, such as a privacy revocation of the only document.
        """
        return hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()

    def record_generation(self, store: StateStore) -> None:
        root = self.settings.sanitized_root
        generation = active_generation(root)
        assert generation is not None
        manifest = (root / MANIFEST_FILENAME).read_bytes()
        payload = validate_manifest(json.loads(manifest), SOURCE_NAME)
        store.record_source_generation(
            SOURCE_NAME,
            generation.name,
            manifest_revision=hashlib.sha256(manifest).hexdigest(),
            item_count=len(payload["items"]),
            redaction_fingerprint=self.anonymizer.fingerprint,
        )

    def rollback_generation(
        self, generation: Path, previous: Path | None, previous_manifest: str | None
    ) -> None:
        root = self.settings.sanitized_root
        if not generation.exists():
            return
        active = active_generation(root)
        if active is not None and active.resolve() == generation.resolve():
            if previous is None:
                (root / "current").unlink()
            else:
                switch_generation(root, previous)
            manifest_path = root / MANIFEST_FILENAME
            if previous_manifest is None:
                manifest_path.unlink(missing_ok=True)
            else:
                atomic_write(manifest_path, previous_manifest)
        shutil.rmtree(generation)

    def prune_generations(self, current: Path) -> None:
        """Delete every generation directory other than the active one.

        Both sides are resolved before comparing: a relative ``SANITIZED_ROOT``
        yields relative paths from ``iterdir()`` while the active generation
        from ``active_generation()`` is absolute, and an unresolved comparison
        would delete the active generation itself.
        """
        for obsolete in (self.settings.sanitized_root / GENERATIONS).iterdir():
            if obsolete.is_dir() and obsolete.resolve() != current.resolve():
                shutil.rmtree(obsolete)

    def published_failures(self) -> int:
        payload = validate_manifest(
            json.loads((self.settings.sanitized_root / MANIFEST_FILENAME).read_text()), SOURCE_NAME
        )
        return len(payload["errors"])

    @staticmethod
    def publish_with_heartbeat(
        work: Callable[[Callable[[], None]], tuple[int, int]],
        store: StateStore,
        lease_id: str,
        lease_seconds: int,
    ) -> tuple[int, int]:
        outcome: dict[str, Any] = {}
        finished = threading.Event()
        authority = threading.Event()
        authority.set()

        def guard() -> None:
            if not authority.is_set():
                raise StateError(f"Paperless ingest lease {lease_id} was lost")

        def target() -> None:
            try:
                outcome["result"] = work(guard)
            except BaseException as error:
                outcome["error"] = error
            finally:
                finished.set()

        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        while not finished.wait(max(lease_seconds // 3, 1)):
            try:
                store.heartbeat(lease_id, lease_seconds=lease_seconds)
            except (sqlite3.Error, StateError, ValueError):
                authority.clear()
                LOG.exception("Could not renew the Paperless ingest lease %s", lease_id)
                break
        worker.join()
        if "error" in outcome:
            raise outcome["error"]
        guard()
        result = outcome["result"]
        assert isinstance(result, tuple)
        return result

    def run_once(self) -> tuple[int, int]:
        root = self.settings.sanitized_root
        with (root / ".ingest.lock").open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if self.settings.state_path is None:
                return self._run_locked()
            store = StateStore.open(self.settings.state_path)
            try:
                try:
                    result = self._run_locked(store)
                except Exception:
                    with suppress(sqlite3.Error, StateError, ValueError):
                        write_health(self.settings.health_path, 1, store.metrics())
                    raise
                write_health(self.settings.health_path, result[1], store.metrics())
                return result
            finally:
                store.close()

    def _run_locked(self, store: StateStore | None = None) -> tuple[int, int]:
        root = self.settings.sanitized_root
        if (root / MANIFEST_FILENAME).exists() and not (root / "current").is_symlink():
            raise ValueError(
                "Paperless flat source layout must be cleared manually before upgrading"
            )
        if (root / "revoked.md").exists() or source_document_ids(root):
            raise ValueError(
                "Paperless flat source layout must be cleared manually before upgrading"
            )
        selected_ids: list[int] | None = None
        input_revisions: dict[str, str] | None = None
        lease_id: str | None = None
        if store is not None:
            selected_ids, input_revisions = self.snapshot()
            moment = int(time.time())
            store.expire_leases(now=moment)
            key = self.cycle_key(input_revisions)
            job, _ = store.enqueue(
                "ingest",
                {"provider": SOURCE_NAME, "source_count": len(selected_ids)},
                idempotency_key=key,
                now=moment,
            )
            for pending in store.pending_jobs("ingest"):
                if pending.id != job.id and pending.payload.get("provider") == SOURCE_NAME:
                    store.supersede(pending.id, now=moment)
            if job.state == JOB_SUCCEEDED and self.published_matches(input_revisions):
                current = active_generation(root)
                assert current is not None
                self.prune_generations(current)
                return 0, self.published_failures()
            if job.state == JOB_DEAD:
                LOG.error("Paperless job %s is dead; change the rejected input or rearm it", job.id)
                return 0, 1
            if job.state in (JOB_SUCCEEDED, JOB_SUPERSEDED):
                store.rearm(job.id, now=moment)
            lease = store.claim(
                job.id,
                holder=f"{socket.gethostname()}:{os.getpid()}",
                lease_seconds=max(self.settings.interval_seconds * 2, 300),
                now=moment,
            )
            if lease is None:
                LOG.info("Paperless job %s is leased or backing off", job.id)
                return 0, 1
            lease_id = lease.id
            if self.published_matches(input_revisions):
                self.record_generation(store)
                current = active_generation(root)
                assert current is not None
                failed = self.published_failures()
                store.complete(lease_id, result={"generation": current.name, "failed": failed})
                self.prune_generations(current)
                return 0, failed
        generations = root / GENERATIONS
        generations.mkdir(exist_ok=True)
        previous = active_generation(root)
        manifest_path = root / MANIFEST_FILENAME
        previous_manifest = manifest_path.read_text() if manifest_path.exists() else None
        # Staging directories are never published; discard interrupted builds.
        for abandoned in generations.glob(".staging-*"):
            if abandoned.is_dir():
                shutil.rmtree(abandoned)
        generation = generations / uuid.uuid4().hex
        staging = generations / f".staging-{generation.name}"
        staging.mkdir()
        try:
            if store is None or lease_id is None:
                return self._build_generation(staging, generation, previous)
            assert selected_ids is not None and input_revisions is not None

            def work(guard: Callable[[], None]) -> tuple[int, int]:
                return self._build_generation(
                    staging, generation, previous, selected_ids, input_revisions, guard
                )

            try:
                try:
                    changed, failed = self.publish_with_heartbeat(
                        work, store, lease_id, max(self.settings.interval_seconds * 2, 300)
                    )
                except StateError:
                    self.rollback_generation(generation, previous, previous_manifest)
                    raise
                if failed and not self.published_matches(input_revisions):
                    store.fail(lease_id, f"operational:{failed}")
                    return changed, failed
                self.record_generation(store)
                current = active_generation(root)
                assert current is not None
                store.complete(
                    lease_id,
                    result={"generation": current.name, "changed": changed, "failed": failed},
                )
                self.prune_generations(current)
                return changed, failed
            except StateError:
                raise
            except Exception as error:
                store.fail(lease_id, f"publication:{type(error).__name__}")
                raise
        finally:
            if staging.exists():
                shutil.rmtree(staging)

    def _build_generation(
        self,
        staging: Path,
        generation: Path,
        previous: Path | None,
        selected_ids: list[int] | None = None,
        input_revisions: dict[str, str] | None = None,
        guard: Callable[[], None] | None = None,
    ) -> tuple[int, int]:
        changed = 0
        failed = 0
        items: list[ManifestItem] = []
        errors: list[dict[str, str]] = []
        if selected_ids is None:
            selected_ids = self.client.selected_document_ids()
        known_ids = source_document_ids(previous) if previous is not None else set()
        revoked_ids = read_revoked_ids(previous / "revoked.md") if previous is not None else set()
        for document_id in selected_ids:
            source_path = f"{document_directory(document_id)}/document-{document_id}.md"
            try:
                document_changed, item = self.process(
                    document_id,
                    staging,
                    previous,
                    generation.name,
                    input_revisions[str(document_id)] if input_revisions is not None else None,
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
            "public_url": self.settings.public_url,
            "source_tag_id": self.settings.source_tag_id,
            "manifest_digest": self.manifest_digest(manifest),
            "source_revisions": {item.source_key: item.source_revision for item in items},
            "input_revisions": input_revisions,
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
                    and old_metadata.get("public_url") == self.settings.public_url
                    and old_metadata.get("source_tag_id") == self.settings.source_tag_id
                    and old_metadata.get("source_revisions") == metadata["source_revisions"]
                    and old_metadata.get("input_revisions") == metadata["input_revisions"]
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
        if guard is not None:
            guard()
        previous_manifest = self.settings.sanitized_root / MANIFEST_FILENAME
        old_manifest = previous_manifest.read_text() if previous_manifest.exists() else None
        staging.rename(generation)
        try:
            if guard is not None:
                guard()
            switch_generation(self.settings.sanitized_root, generation)
            if guard is not None:
                guard()
            write_manifest(self.settings.sanitized_root / MANIFEST_FILENAME, manifest)
            if guard is not None:
                guard()
        except BaseException:
            self.rollback_generation(generation, previous, old_manifest)
            raise
        if guard is None:
            self.prune_generations(generation)
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
        self,
        document_id: int,
        staging: Path,
        previous: Path | None,
        generation: str,
        expected_revision: str | None = None,
    ) -> tuple[bool, ManifestItem]:
        document, document_type, tag_names = self.prepare_document(document_id)
        issued_date = normalize_issued_date(document.get("created"))
        digest = source_hash(document, self.anonymizer.fingerprint, document_type, tag_names)
        if expected_revision is not None and digest != expected_revision:
            raise ValueError("Paperless document changed after job acceptance")
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
        except (urllib.error.URLError, OSError, ValueError, sqlite3.Error):
            LOG.exception("Paperless synchronization failed")
            metrics = None
            if settings.state_path is not None:
                with suppress(sqlite3.Error, StateError, ValueError, OSError):
                    store = StateStore.open(settings.state_path)
                    try:
                        metrics = store.metrics()
                    finally:
                        store.close()
            write_health(settings.health_path, 1, metrics)
        if stop_event.wait(settings.interval_seconds):
            return
