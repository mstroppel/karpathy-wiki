"""Email provider: receive originals before deletion, then publish redacted text."""

from __future__ import annotations

import argparse
import imaplib
import json
import logging
import shutil
import signal
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from karpathy_wiki_ingest.manifest import ManifestItem, build_manifest, write_manifest
from karpathy_wiki_ingest.shared import TargetedAnonymizer, atomic_write, write_health

from .extract import EXTRACTION_VERSION, render
from .mailbox import connection, receive
from .settings import Settings
from .store import Store, digest

LOG = logging.getLogger("karpathy-wiki-email")


def extract(store: Store, anonymizer: TargetedAnonymizer, limit: int) -> int:
    failed = handled = 0
    for source in store.sources():
        if not source["pending"]:
            continue
        if source["attempts"] >= 5:
            failed += 1
            continue
        if source["next_attempt"] > int(time.time()):
            failed += 1
            continue
        if handled >= limit:
            break
        handled += 1
        try:
            output = render(store.read(source["original"]), source["id"], anonymizer)
            store.succeeded(source["id"], output)
        except (OSError, ValueError, UnicodeError, LookupError, RecursionError) as error:
            store.failed(source["id"], type(error).__name__)
            failed += 1
    return failed


def publish(store: Store, root: Path, anonymizer: TargetedAnonymizer) -> int:
    """Immutable generations + atomic manifest. All visible data crosses the gate.

    Recheck stored projections under CURRENT redactions without re-extracting
    originals. A more permissive policy needs explicit replay. Retire old
    generations only after the atomic manifest switch, as other providers do.
    """
    documents: dict[str, str] = {}
    paths: dict[str, Path] = {}
    revoked = []
    errors = []
    for source in store.sources():
        if source["error"]:
            errors.append({"source_key": source["id"], "error": source["error"]})
        if source["output"] is None:
            revoked.append({"source_key": source["id"], "claim": {"email_id": source["id"]}})
            continue
        try:
            relative = store.relative_path(source["original"]).with_suffix(".md")
            documents[source["id"]] = anonymizer.anonymize(source["output"])[0]
            paths[source["id"]] = relative
        except ValueError:
            revoked.append({"source_key": source["id"], "claim": {"email_id": source["id"]}})
            errors.append({"source_key": source["id"], "error": "PrivacyValidationError"})
    fingerprint = digest(
        json.dumps([EXTRACTION_VERSION, anonymizer.fingerprint, documents], sort_keys=True).encode()
    )
    # Deterministic content identity makes publication restart-idempotent.
    generation = root / "generations" / fingerprint
    root.mkdir(parents=True, exist_ok=True)
    items = []
    for source_id, text in documents.items():
        revision = digest(text.encode("utf-8"))
        items.append(
            ManifestItem(
                source_key=source_id,
                source_path=f"generations/{fingerprint}/{paths[source_id].as_posix()}",
                wiki_path=f"email/{source_id}/index.md",
                source_revision=revision,
                source_sha256=revision,
                frontmatter={
                    "source_adapter": "email",
                    "email_id": source_id,
                    "source_revision": revision,
                },
                claim={"email_id": source_id},
            )
        )
    manifest = build_manifest("email", items, revoked=revoked, errors=errors, wiki_root="email")
    if anonymizer.locate(json.dumps(manifest, ensure_ascii=False)):
        raise ValueError("manifest privacy validation failed")
    if not generation.exists():
        staging = root / f".staging-{uuid.uuid4().hex}"
        staging.mkdir()
        for source_id, text in documents.items():
            destination = staging / paths[source_id]
            destination.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(destination, text)
        generation.parent.mkdir(exist_ok=True)
        staging.rename(generation)
    else:
        for source_id, text in documents.items():
            if (generation / paths[source_id]).read_text(encoding="utf-8") != text:
                raise ValueError("published generation integrity failure")
    path = root / "manifest.json"
    unchanged = False
    if path.exists():
        previous = json.loads(path.read_text(encoding="utf-8"))
        comparable = dict(previous, generated_at=manifest["generated_at"])
        if comparable == manifest:
            unchanged = True
    if not unchanged:
        write_manifest(path, manifest)
    # Retire old projections after the manifest switch, especially after a
    # stricter redaction policy. Raw originals are never affected by this.
    for old in generation.parent.iterdir():
        if old != generation and old.is_dir() and not old.is_symlink():
            shutil.rmtree(old)
    for abandoned in root.glob(".staging-*"):
        if abandoned.is_dir() and not abandoned.is_symlink():
            shutil.rmtree(abandoned)
    return len(errors)


def cycle(settings: Settings, *, replay: bool = False, source_id: str | None = None) -> int:
    anonymizer = TargetedAnonymizer.from_file(settings.redactions)
    store = Store(settings.archive)
    failed = 0
    try:
        if replay:
            store.rearm(source_id)
        else:
            try:
                with connection(settings) as client:
                    _, failed = receive(client, settings, store)
            except (OSError, ValueError, sqlite3.Error, imaplib.IMAP4.error) as error:
                # An unavailable server must not prevent processing safe local
                # originals. State failure never permits an IMAP deletion.
                LOG.error("Email reception failed (%s)", type(error).__name__)
                failed = 1
        failed += extract(store, anonymizer, len(store.sources()) if replay else settings.batch)
        failed += publish(store, settings.sanitized, anonymizer)
        return failed
    finally:
        store.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Archive IMAP mail and publish redacted text")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--replay", metavar="ID_OR_ALL", help="Offline explicit re-extraction")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env(offline=args.replay is not None)
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    while not stop.is_set():
        try:
            failed = cycle(
                settings,
                replay=args.replay is not None,
                source_id=None if args.replay == "all" else args.replay,
            )
        except Exception as error:
            # Content-free diagnostics, including IMAP auth errors and bad MIME.
            LOG.error("Email cycle failed (%s)", type(error).__name__)
            failed = 1
        write_health(settings.health, failed)
        if args.once or args.replay is not None:
            if failed:
                raise SystemExit(1)
            return
        stop.wait(settings.interval)
