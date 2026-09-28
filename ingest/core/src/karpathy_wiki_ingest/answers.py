"""Publish confirmed, local Q&A drafts as tracked, redacted source entries."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import tempfile
import time
from pathlib import Path

from .manifest import ManifestItem, build_manifest, write_manifest
from .shared import TargetedAnonymizer, interval_seconds, write_health

END_MARKER = "<!-- END CONFIRMED ANSWERS -->"


def publish(inbox: Path, source_root: Path, redactions: Path) -> int:
    """Publish a complete inventory; leave the last manifest intact on failure."""
    anonymizer = TargetedAnonymizer.from_file(redactions)
    source_root.mkdir(parents=True, exist_ok=True)
    revisions = source_root / "revisions"
    revisions.mkdir(exist_ok=True)
    items: list[ManifestItem] = []
    for entry in sorted(inbox.iterdir()):
        if entry.is_symlink():
            raise ValueError("answer inbox contains a symlink")
        if not entry.is_file() or entry.suffix != ".md":
            raise ValueError("answer inbox must contain only Markdown files")
        if not entry.stem or not all(
            c.isascii() and (c.islower() or c.isdigit() or c == "-") for c in entry.stem
        ):
            raise ValueError("answer draft filename must be lowercase kebab-case")
        raw = entry.read_bytes().decode("utf-8")
        if not raw.rstrip().endswith(END_MARKER):
            raise ValueError("answer draft is incomplete")
        sanitized, _ = anonymizer.anonymize(raw)
        data = sanitized.encode("utf-8")
        revision = hashlib.sha256(data).hexdigest()
        # Never mutate a published revision. A redaction change yields new bytes
        # and therefore a new immutable source path.
        relative = f"revisions/{entry.stem}-{revision}.md"
        destination = revisions / f"{entry.stem}-{revision}.md"
        if destination.exists():
            if destination.is_symlink() or destination.read_bytes() != data:
                raise ValueError("published answer revision differs from its hash")
        else:
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(dir=revisions, delete=False) as output:
                    temporary = Path(output.name)
                    output.write(data)
                os.link(temporary, destination)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        items.append(
            ManifestItem(
                source_key=entry.name,
                source_path=relative,
                wiki_path=f"answers/{entry.stem}/index.md",
                source_revision=revision,
                frontmatter={
                    "source_adapter": "answers",
                    "source_path": entry.name,
                    "source_revision": revision,
                },
                claim={"source_path": entry.name},
            )
        )
    # Dropping an answer from the inbox must never silently remove its provenance.
    # The inbox is an archive; an unexpected removal blocks publication.
    manifest_path = source_root / "manifest.json"
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        previous_keys = {item["source_key"] for item in previous["items"]}
        if previous_keys - {item.source_key for item in items}:
            raise ValueError("published answer is missing from the inbox")
    write_manifest(manifest_path, build_manifest("answers", items, wiki_root="answers"))
    return len(items)


def main() -> None:
    inbox = Path(os.environ.get("ANSWERS_INBOX", "/data/incoming/answers"))
    source_root = Path(os.environ.get("ANSWERS_SOURCE_ROOT", "/data/sanitized/answers"))
    redactions = Path(os.environ.get("REDACTIONS_FILE", "/run/secrets/redactions"))
    health = Path(os.environ.get("HEALTH_PATH", "/tmp/health.json"))
    interval = interval_seconds(os.environ.get("ANSWERS_SYNC_INTERVAL", "15"))
    stop = False

    def shutdown(_signal: int, _frame: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    while not stop:
        try:
            publish(inbox, source_root, redactions)
            write_health(health, 0)
        except (OSError, UnicodeError, ValueError, KeyError, TypeError) as error:
            # Never log source contents, including exception strings from codecs.
            print(f"answer publication failed: {type(error).__name__}", flush=True)
            write_health(health, 1)
        deadline = time.monotonic() + interval
        while not stop:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(1, remaining))


if __name__ == "__main__":
    main()
