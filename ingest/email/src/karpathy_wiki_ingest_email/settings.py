"""Explicit IMAP account/folder configuration; no provider discovery or OAuth."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from email.utils import getaddresses
from pathlib import Path

from karpathy_wiki_ingest.shared import interval_seconds, required_env


def address(value: str) -> str:
    """An exact mailbox, preserving the local part and its plus tag."""
    if not re.fullmatch(r"[^\s<>,;@]+@[^\s<>,;@]+", value):
        raise ValueError("expected one exact mailbox address")
    local, domain = value.rsplit("@", 1)
    return f"{local}@{domain.lower()}"


def mailboxes(values: list[str]) -> list[str]:
    return [address(mailbox) for _, mailbox in getaddresses(values)]


def positive(name: str, default: str) -> int:
    value = int(os.getenv(name, default))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class Settings:
    host: str
    username: str
    password: str
    archive: Path
    sanitized: Path
    redactions: Path
    health: Path
    folder: str = "INBOX"
    port: int = 993
    interval: int = 60
    batch: int = 25
    max_bytes: int = 25 * 1024 * 1024
    timeout: int = 30
    allowlist: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, *, offline: bool = False) -> Settings:
        result = cls(
            host="" if offline else required_env("EMAIL_IMAP_HOST"),
            username="" if offline else required_env("EMAIL_IMAP_USERNAME"),
            password="" if offline else os.environ.get("EMAIL_IMAP_PASSWORD", ""),
            archive=Path(os.getenv("EMAIL_ARCHIVE_ROOT", "/data/incoming/email")),
            sanitized=Path(os.getenv("EMAIL_SANITIZED_ROOT", "/data/sanitized/email")),
            redactions=Path(required_env("REDACTIONS_FILE")),
            health=Path(os.getenv("HEALTH_PATH", "/tmp/health.json")),
            folder=os.getenv("EMAIL_IMAP_FOLDER", "INBOX"),
            port=positive("EMAIL_IMAP_PORT", "993"),
            interval=interval_seconds(os.getenv("EMAIL_POLL_INTERVAL", "60s")),
            batch=positive("EMAIL_BATCH_SIZE", "25"),
            max_bytes=positive("EMAIL_MAX_BYTES", "26214400"),
            timeout=positive("EMAIL_TIMEOUT_SECONDS", "30"),
            allowlist=tuple(
                address(item.strip())
                for item in os.getenv("EMAIL_FROM_ALLOWLIST", "").split(",")
                if item.strip()
            ),
        )
        if not offline and not result.password:
            raise ValueError("EMAIL_IMAP_PASSWORD is required")
        roots = (result.archive.resolve(), result.sanitized.resolve())
        if roots[0].is_relative_to(roots[1]) or roots[1].is_relative_to(roots[0]):
            raise ValueError("original archive and published sources must be separate")
        return result
