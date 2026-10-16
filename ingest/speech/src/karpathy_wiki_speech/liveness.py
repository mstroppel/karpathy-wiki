"""Content-free health with fixed phase deadlines, not an unlimited timer heartbeat.

Standalone so the speech image does not depend on ingest core. The timer proves
process liveness only; actual work must advance phases before their deadlines.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

HEARTBEAT_SECONDS = 10
STALE_SECONDS = 120


def positive_seconds(name: str, default: int) -> int:
    value = int(os.getenv(name, str(default)))
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def read_live(path: Path, *, now: float | None = None) -> dict[str, Any]:
    """Require a ready, live process within its deadline, not successful work."""
    moment = time.time() if now is None else now
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("ready") is not True
            or not isinstance(payload.get("phase"), str)
        ):
            raise ValueError("unready or failed")
        checked = float(payload["checked_at"])
        deadline = float(payload["deadline_at"])
        if (
            not math.isfinite(checked)
            or not math.isfinite(deadline)
            or not 0 <= moment - checked <= STALE_SECONDS
            or moment >= deadline
        ):
            raise ValueError("stale or stalled")
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise RuntimeError("service is unready, failed, stale or stalled") from error
    return payload


def read_healthy(path: Path, *, now: float | None = None) -> dict[str, Any]:
    """Additionally reject recorded work failures (the Compose health policy)."""
    payload = read_live(path, now=now)
    if payload.get("failed") != 0:
        raise RuntimeError("service is unready, failed, stale or stalled")
    return payload


class Liveness:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.phase = "startup"
        self.ready = False
        self.failed = 0
        self.progress = 0
        self.last_success_at: float | None = None
        self.metrics: dict[str, Any] | None = None
        self.deadline = time.monotonic() + 120
        self.write()
        self.thread = threading.Thread(target=self._heartbeat, daemon=True)
        self.thread.start()

    def _heartbeat(self) -> None:
        while not self.stop.wait(HEARTBEAT_SECONDS):
            try:
                self.write()
            except OSError:
                # An unwritable record becomes stale. No fake health fallback.
                return

    def write(self) -> None:
        with self.lock:
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                self.failed = max(self.failed, 1)
                self.phase = "stalled"
            now = time.time()
            payload = {
                "checked_at": now,
                "deadline_at": now + remaining,
                "phase": self.phase,
                "ready": self.ready,
                "failed": self.failed,
                "progress": self.progress,
                "last_success_at": self.last_success_at,
                "metrics": self.metrics,
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as out:
                temporary = Path(out.name)
                try:
                    json.dump(payload, out)
                    out.flush()
                    os.replace(temporary, self.path)
                finally:
                    temporary.unlink(missing_ok=True)

    def enter(self, phase: str, seconds: int, *, ready: bool | None = None) -> None:
        if seconds <= 0:
            raise ValueError("phase deadline must be positive")
        with self.lock:
            if time.monotonic() >= self.deadline:
                self.failed = max(self.failed, 1)
            self.phase = phase
            self.deadline = time.monotonic() + seconds
            self.progress += 1
            if ready is not None:
                self.ready = ready
            self.write()

    def fail(self) -> None:
        with self.lock:
            self.failed = max(self.failed, 1)
            self.write()

    def finish(self, failed: int, idle_seconds: int) -> None:
        with self.lock:
            self.failed = failed
            if not failed:
                self.last_success_at = time.time()
            self.enter("failed" if failed else "idle", idle_seconds, ready=True)

    def close(self) -> None:
        self.stop.set()
        self.thread.join()
