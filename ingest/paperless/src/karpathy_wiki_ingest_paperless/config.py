"""Configuration and secret handling for the Paperless ingest plugin."""

from __future__ import annotations

import os
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from karpathy_wiki_ingest.shared import required_env


def read_secret(value_name: str, file_name: str) -> str:
    direct = os.getenv(value_name, "").strip()
    if direct:
        return direct
    path = required_env(file_name)
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"secret file for {value_name} is empty")
    return value


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
    state_path: Path | None = None

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
        parts = urllib.parse.urlsplit(public_url)
        if parts.scheme != "https":
            raise ValueError("PAPERLESS_PUBLIC_URL must use HTTPS")
        if not parts.netloc:
            raise ValueError("PAPERLESS_PUBLIC_URL must include an HTTPS host")
        if parts.query or parts.fragment:
            raise ValueError("PAPERLESS_PUBLIC_URL must not include a query or fragment")
        return cls(
            public_url=public_url,
            source_tag_id=source_tag_id,
            token=token,
            redactions_path=Path(required_env("REDACTIONS_FILE")),
            interval_seconds=interval_seconds,
            sanitized_root=Path(os.getenv("SANITIZED_ROOT", "/data/sanitized/paperless")),
            quarantine_root=Path(os.getenv("QUARANTINE_ROOT", "/data/quarantine/paperless")),
            health_path=Path(os.getenv("HEALTH_PATH", "/tmp/health.json")),
            state_path=Path(os.environ["INGEST_STATE_PATH"])
            if os.getenv("INGEST_STATE_PATH")
            else None,
        )
