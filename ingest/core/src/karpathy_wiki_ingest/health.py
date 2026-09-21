"""Healthcheck entry point for the ingest daemons.

Writes to the shared health record are performed by the ingest plugins; this
module only reads it. It fails when the last synchronization failed or when
the record is older than two synchronization intervals.
"""

import json
import os
import time
from pathlib import Path

from karpathy_wiki_ingest.shared import interval_seconds


def interval() -> int:
    seconds = os.getenv("SYNC_INTERVAL_SECONDS")
    if seconds is None:
        # The WebDAV plugin uses a duration string instead of plain seconds.
        return interval_seconds(os.getenv("WEBDAV_SYNC_INTERVAL", "15m"))
    return int(seconds)


def main() -> None:
    health_path = Path(os.getenv("HEALTH_PATH", "/tmp/health.json"))
    payload = json.loads(health_path.read_text(encoding="utf-8"))
    maximum_age = max(interval() * 2, 120)
    if payload.get("failed") or time.time() - int(payload["checked_at"]) > maximum_age:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
