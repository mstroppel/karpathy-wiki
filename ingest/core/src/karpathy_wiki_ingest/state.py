"""Durable ingest state: jobs, leases, source generations, and publications.

A small embedded SQLite store records accepted ingest work so restarting a
service neither loses it nor executes the same publication twice. Job state
transitions, attempts, leases, source generations, and publications are
persistent; Git remains the human-readable audit history, but no longer the
concurrency-control mechanism. This is the durable-state boundary of the
transactional pipeline (#44): providers record their source generations here
and enqueue idempotent ingest jobs, and the serialized publisher work package
consumes the same tables.

The store is content-free: it records revisions, identifiers, counts, and
content-free error strings, never source or wiki content.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contract import REVISION_RE

GIT_REVISION_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")

STATE_VERSION = 1

JOB_TYPES = ("ingest", "publish")
JOB_PENDING = "pending"
JOB_LEASED = "leased"
JOB_SUCCEEDED = "succeeded"
JOB_DEAD = "dead"
JOB_STATES = (JOB_PENDING, JOB_LEASED, JOB_SUCCEEDED, JOB_DEAD)

PUBLICATION_STATUSES = ("published", "rolled_back", "rejected")

# A terminal job keeps its idempotency key: re-submitting the same key never
# re-executes the work; `rearm` is the explicit recovery transition.
REARMABLE_STATES = (JOB_SUCCEEDED, JOB_DEAD)

_IDEMPOTENCY_KEY_LIMIT = 512
_ERROR_LIMIT = 500
_HOLDER_LIMIT = 256

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    payload TEXT NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at INTEGER NOT NULL,
    last_error TEXT,
    idempotency_key TEXT UNIQUE,
    result TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS jobs_claimable ON jobs (job_type, state, next_attempt_at);
CREATE TABLE IF NOT EXISTS leases (
    id TEXT PRIMARY KEY,
    job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
    holder TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    acquired_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS job_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(id),
    at INTEGER NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS source_generations (
    provider TEXT NOT NULL,
    generation_id TEXT NOT NULL,
    manifest_revision TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    redaction_fingerprint TEXT,
    recorded_at INTEGER NOT NULL,
    PRIMARY KEY (provider, generation_id)
);
CREATE TABLE IF NOT EXISTS publications (
    idempotency_key TEXT PRIMARY KEY,
    job_id TEXT REFERENCES jobs(id),
    provider TEXT NOT NULL,
    source_generation_id TEXT NOT NULL,
    wiki_base_revision TEXT,
    commit_id TEXT,
    status TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    completed_at INTEGER
);
"""


class StateError(ValueError):
    """The state transition is not valid for the recorded job or lease."""


@dataclass(frozen=True)
class Job:
    id: str
    job_type: str
    payload: dict[str, Any]
    state: str
    attempts: int
    next_attempt_at: int
    last_error: str | None
    idempotency_key: str | None
    result: dict[str, Any] | None
    created_at: int
    updated_at: int


@dataclass(frozen=True)
class Lease:
    id: str
    job_id: str
    holder: str
    expires_at: int
    acquired_at: int


def _now() -> int:
    return int(time.time())


def _check_text(value: Any, label: str, limit: int) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"{label} must be a non-empty string of at most {limit} characters")
    return value


def _check_positive(value: Any, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _encode_json(value: Any, label: str) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{label} is not JSON-serializable") from error


class StateStore:
    """SQLite-backed durable state for the ingest and publication pipeline."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    @classmethod
    def open(cls, path: str | Path, *, busy_timeout_ms: int = 5000) -> StateStore:
        """Open (or create) the store; the schema is versioned and additive."""
        if not isinstance(path, (str, Path)) or not str(path):
            raise ValueError("state store path must be a non-empty string")
        connection = sqlite3.connect(str(path), timeout=busy_timeout_ms / 1000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
        connection.execute("PRAGMA foreign_keys=ON")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > STATE_VERSION:
            connection.close()
            raise ValueError(f"state store version {version} is newer than {STATE_VERSION}")
        with connection:
            if version < STATE_VERSION:
                connection.executescript(_SCHEMA)
                connection.execute(f"PRAGMA user_version={STATE_VERSION}")
        return cls(connection)

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        # BEGIN IMMEDIATE serializes writers before any read, so a claim can
        # never observe and mutate state behind another writer's back.
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield self._connection
        except BaseException:
            self._connection.execute("ROLLBACK")
            raise
        self._connection.execute("COMMIT")

    def _record_event(
        self,
        job_id: str,
        to_state: str,
        *,
        at: int,
        from_state: str | None = None,
        detail: str | None = None,
    ) -> None:
        self._connection.execute(
            "INSERT INTO job_events (job_id, at, from_state, to_state, detail)"
            " VALUES (?, ?, ?, ?, ?)",
            (job_id, at, from_state, to_state, detail),
        )

    @staticmethod
    def _job(row: sqlite3.Row | None) -> Job | None:
        if row is None:
            return None
        payload = json.loads(row["payload"])
        result = json.loads(row["result"]) if row["result"] is not None else None
        return Job(
            id=row["id"],
            job_type=row["job_type"],
            payload=payload,
            state=row["state"],
            attempts=row["attempts"],
            next_attempt_at=row["next_attempt_at"],
            last_error=row["last_error"],
            idempotency_key=row["idempotency_key"],
            result=result,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _lease(row: sqlite3.Row | None) -> Lease | None:
        if row is None:
            return None
        return Lease(
            id=row["id"],
            job_id=row["job_id"],
            holder=row["holder"],
            expires_at=row["expires_at"],
            acquired_at=row["acquired_at"],
        )

    # -- jobs ---------------------------------------------------------------

    def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        now: int | None = None,
    ) -> tuple[Job, bool]:
        """Record accepted work; an existing job for the key is never re-run."""
        if job_type not in JOB_TYPES:
            raise ValueError(f"job_type must be one of {', '.join(JOB_TYPES)}")
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        moment = _now() if now is None else now
        encoded_payload = _encode_json(payload, "payload")
        with self._transaction() as connection:
            if idempotency_key is not None:
                _check_text(idempotency_key, "idempotency_key", _IDEMPOTENCY_KEY_LIMIT)
                existing = self._job(
                    connection.execute(
                        "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
                    ).fetchone()
                )
                if existing is not None:
                    return existing, False
            job_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO jobs (id, job_type, payload, state, attempts, next_attempt_at,"
                " idempotency_key, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)",
                (
                    job_id,
                    job_type,
                    encoded_payload,
                    JOB_PENDING,
                    moment,
                    idempotency_key,
                    moment,
                    moment,
                ),
            )
            self._record_event(job_id, JOB_PENDING, at=moment, detail="accepted")
        return self.job(job_id), True

    def job(self, job_id: str) -> Job:
        found = self._job(
            self._connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        )
        if found is None:
            raise ValueError(f"unknown job: {job_id}")
        return found

    def job_by_idempotency_key(self, idempotency_key: str) -> Job | None:
        return self._job(
            self._connection.execute(
                "SELECT * FROM jobs WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        )

    def job_events(self, job_id: str) -> list[dict[str, Any]]:
        """Durable state-transition history of a job, oldest first."""
        rows = self._connection.execute(
            "SELECT at, from_state, to_state, detail FROM job_events WHERE job_id = ? ORDER BY id",
            (job_id,),
        ).fetchall()
        return [
            {
                "at": row["at"],
                "from_state": row["from_state"],
                "to_state": row["to_state"],
                "detail": row["detail"],
            }
            for row in rows
        ]

    def _job_row_for_update(self, connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown job: {job_id}")
        return row

    def _set_job(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        *,
        state: str,
        next_attempt_at: int | None = None,
        last_error: str | None = None,
        result: str | None = None,
        attempts: int | None = None,
        at: int,
        from_state: str,
        detail: str | None = None,
    ) -> None:
        fields = ["state = ?", "updated_at = ?"]
        values: list[Any] = [state, at]
        if next_attempt_at is not None:
            fields.append("next_attempt_at = ?")
            values.append(next_attempt_at)
        if last_error is not None or state == JOB_PENDING:
            fields.append("last_error = ?")
            values.append(last_error)
        if result is not None or state == JOB_PENDING:
            fields.append("result = ?")
            values.append(result)
        if attempts is not None:
            fields.append("attempts = ?")
            values.append(attempts)
        values.append(job_id)
        connection.execute(f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?", values)
        self._record_event(job_id, state, at=at, from_state=from_state, detail=detail)

    # -- leases -------------------------------------------------------------

    def claim(
        self,
        job_id: str,
        holder: str,
        *,
        lease_seconds: int = 600,
        now: int | None = None,
    ) -> Lease | None:
        """Atomically move a retryable job to ``leased`` and hand out its lease.

        Returns ``None`` when the job is not claimable: unknown, terminal,
        leased by a live holder, or waiting out its retry backoff.
        """
        _check_text(holder, "holder", _HOLDER_LIMIT)
        _check_positive(lease_seconds, "lease_seconds")
        moment = _now() if now is None else now
        with self._transaction() as connection:
            row = self._job_row_for_update(connection, job_id)
            if row["state"] != JOB_PENDING or row["next_attempt_at"] > moment:
                return None
            lease_id = uuid.uuid4().hex
            self._set_job(
                connection,
                job_id,
                state=JOB_LEASED,
                attempts=row["attempts"] + 1,
                at=moment,
                from_state=JOB_PENDING,
                detail=f"lease={lease_id} holder={holder}",
            )
            connection.execute(
                "INSERT INTO leases (id, job_id, holder, expires_at, acquired_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (lease_id, job_id, holder, moment + lease_seconds, moment),
            )
        return self.lease(lease_id)

    def lease(self, lease_id: str) -> Lease:
        found = self._lease(
            self._connection.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
        )
        if found is None:
            raise ValueError(f"unknown lease: {lease_id}")
        return found

    def heartbeat(self, lease_id: str, *, lease_seconds: int, now: int | None = None) -> Lease:
        """Extend a held lease; long work must never rely on a stale lease.

        An expired lease cannot be renewed: the job must be recovered through
        ``expire_leases`` so restart recovery cannot be blocked indefinitely.
        """
        _check_positive(lease_seconds, "lease_seconds")
        moment = _now() if now is None else now
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM leases WHERE id = ?", (lease_id,)).fetchone()
            if row is None:
                raise ValueError(f"unknown lease: {lease_id}")
            if row["expires_at"] <= moment:
                raise StateError(
                    f"lease {lease_id} expired at {row['expires_at']} and cannot be renewed"
                )
            connection.execute(
                "UPDATE leases SET expires_at = ? WHERE id = ?", (moment + lease_seconds, lease_id)
            )
        return self.lease(lease_id)

    def complete(
        self, lease_id: str, *, result: dict[str, Any] | None = None, now: int | None = None
    ) -> Job:
        """Finish a leased job exactly once; the lease proves the holder."""
        moment = _now() if now is None else now
        with self._transaction() as connection:
            lease_row = connection.execute(
                "SELECT * FROM leases WHERE id = ?", (lease_id,)
            ).fetchone()
            if lease_row is None:
                raise StateError(f"unknown lease: {lease_id}")
            job_row = self._job_row_for_update(connection, lease_row["job_id"])
            if job_row["state"] != JOB_LEASED:
                raise StateError(f"job {job_row['id']} is not leased")
            encoded_result = _encode_json(result or {}, "result")
            self._set_job(
                connection,
                job_row["id"],
                state=JOB_SUCCEEDED,
                result=encoded_result,
                at=moment,
                from_state=JOB_LEASED,
                detail=f"lease={lease_id}",
            )
            connection.execute("DELETE FROM leases WHERE id = ?", (lease_id,))
        return self.job(job_row["id"])

    def fail(
        self,
        lease_id: str,
        error: str,
        *,
        base_backoff_seconds: int = 60,
        max_backoff_seconds: int = 3600,
        max_attempts: int = 5,
        now: int | None = None,
    ) -> Job:
        """Return a leased job to ``pending`` with exponential backoff.

        A job that reached its attempt limit becomes ``dead``; only ``rearm``
        can make it claimable again.
        """
        _check_text(error, "error", _ERROR_LIMIT)
        _check_positive(base_backoff_seconds, "base_backoff_seconds")
        _check_positive(max_backoff_seconds, "max_backoff_seconds")
        _check_positive(max_attempts, "max_attempts")
        moment = _now() if now is None else now
        with self._transaction() as connection:
            lease_row = connection.execute(
                "SELECT * FROM leases WHERE id = ?", (lease_id,)
            ).fetchone()
            if lease_row is None:
                raise StateError(f"unknown lease: {lease_id}")
            job_row = self._job_row_for_update(connection, lease_row["job_id"])
            if job_row["state"] != JOB_LEASED:
                raise StateError(f"job {job_row['id']} is not leased")
            attempts = job_row["attempts"]
            if attempts >= max_attempts:
                state = JOB_DEAD
                next_attempt_at = moment
            else:
                state = JOB_PENDING
                next_attempt_at = moment + min(
                    base_backoff_seconds * 2 ** (attempts - 1), max_backoff_seconds
                )
            self._set_job(
                connection,
                job_row["id"],
                state=state,
                next_attempt_at=next_attempt_at,
                last_error=error,
                at=moment,
                from_state=JOB_LEASED,
                detail=f"lease={lease_id} attempts={attempts}",
            )
            connection.execute("DELETE FROM leases WHERE id = ?", (lease_id,))
        return self.job(job_row["id"])

    def expire_leases(self, now: int | None = None) -> int:
        """Return jobs of expired leases to ``pending``; restart recovery."""
        moment = _now() if now is None else now
        with self._transaction() as connection:
            expired = connection.execute(
                "SELECT * FROM leases WHERE expires_at <= ?", (moment,)
            ).fetchall()
            for row in expired:
                job_row = self._job_row_for_update(connection, row["job_id"])
                if job_row["state"] == JOB_LEASED:
                    self._set_job(
                        connection,
                        job_row["id"],
                        state=JOB_PENDING,
                        next_attempt_at=moment,
                        at=moment,
                        from_state=JOB_LEASED,
                        detail=f"lease={row['id']} expired",
                    )
                connection.execute("DELETE FROM leases WHERE id = ?", (row["id"],))
        return len(expired)

    def rearm(self, job_id: str, *, now: int | None = None) -> Job:
        """Recovery transition: make a terminal job claimable again.

        Only used when the recorded outcome is gone (for example after a
        restore) or when a permanently failed input becomes worth retrying.
        """
        moment = _now() if now is None else now
        with self._transaction() as connection:
            row = self._job_row_for_update(connection, job_id)
            if row["state"] not in REARMABLE_STATES:
                raise StateError(f"job {job_id} in state {row['state']} cannot be rearmed")
            self._set_job(
                connection,
                job_id,
                state=JOB_PENDING,
                next_attempt_at=moment,
                attempts=0,
                at=moment,
                from_state=row["state"],
                detail="rearmed",
            )
        return self.job(job_id)

    # -- source generations and publications --------------------------------

    def record_source_generation(
        self,
        provider: str,
        generation_id: str,
        *,
        manifest_revision: str,
        item_count: int,
        redaction_fingerprint: str | None = None,
        now: int | None = None,
    ) -> bool:
        """Record an immutable source generation; ``False`` if already known.

        ``manifest_revision`` is the SHA-256 of the generation's manifest, so
        a recorded generation can always be verified against the filesystem.
        """
        _check_text(provider, "provider", _IDEMPOTENCY_KEY_LIMIT)
        _check_text(generation_id, "generation_id", _IDEMPOTENCY_KEY_LIMIT)
        if not isinstance(manifest_revision, str) or not REVISION_RE.fullmatch(manifest_revision):
            raise ValueError("manifest_revision must be a lowercase SHA-256 digest")
        if not isinstance(item_count, int) or isinstance(item_count, bool) or item_count < 0:
            raise ValueError("item_count must be a non-negative integer")
        moment = _now() if now is None else now
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO source_generations"
                " (provider, generation_id, manifest_revision, item_count,"
                "  redaction_fingerprint, recorded_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    provider,
                    generation_id,
                    manifest_revision,
                    item_count,
                    redaction_fingerprint,
                    moment,
                ),
            )
        return cursor.rowcount > 0

    def record_publication(
        self,
        idempotency_key: str,
        provider: str,
        source_generation_id: str,
        *,
        job_id: str | None = None,
        wiki_base_revision: str | None = None,
        commit: str | None = None,
        status: str = "published",
        now: int | None = None,
    ) -> bool:
        """Record a wiki publication exactly once for its idempotency key.

        The primary key rejects a duplicate record: the same accepted work
        can never be executed as two publications.
        """
        _check_text(idempotency_key, "idempotency_key", _IDEMPOTENCY_KEY_LIMIT)
        _check_text(provider, "provider", _IDEMPOTENCY_KEY_LIMIT)
        _check_text(source_generation_id, "source_generation_id", _IDEMPOTENCY_KEY_LIMIT)
        if status not in PUBLICATION_STATUSES:
            raise ValueError(f"status must be one of {', '.join(PUBLICATION_STATUSES)}")
        for name, value in (("wiki_base_revision", wiki_base_revision), ("commit", commit)):
            if value is not None and (
                not isinstance(value, str) or not GIT_REVISION_RE.fullmatch(value)
            ):
                raise ValueError(f"{name} must be a lowercase Git/SHA-256 digest or None")
        if job_id is not None:
            self.job(job_id)
        moment = _now() if now is None else now
        with self._transaction() as connection:
            cursor = connection.execute(
                "INSERT OR IGNORE INTO publications"
                " (idempotency_key, job_id, provider, source_generation_id, wiki_base_revision,"
                "  commit_id, status, created_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    job_id,
                    provider,
                    source_generation_id,
                    wiki_base_revision,
                    commit,
                    status,
                    moment,
                    moment if status != "published" or commit else None,
                ),
            )
        return cursor.rowcount > 0

    # -- observation --------------------------------------------------------

    def metrics(self, now: int | None = None) -> dict[str, Any]:
        """Queue depth, oldest pending age, and durable entity counts."""
        moment = _now() if now is None else now
        jobs = {
            state: self._connection.execute(
                "SELECT COUNT(*) FROM jobs WHERE state = ?", (state,)
            ).fetchone()[0]
            for state in JOB_STATES
        }
        row = self._connection.execute(
            "SELECT MIN(created_at) FROM jobs WHERE state = ?", (JOB_PENDING,)
        ).fetchone()
        oldest = row[0]
        return {
            "jobs": jobs,
            # The pending age is measured from acceptance, not from the
            # backoff-scheduled `next_attempt_at`, so an old retrying job
            # cannot appear freshly accepted.
            "oldest_pending_age": max(moment - oldest, 0) if oldest is not None else 0,
            "source_generations": self._connection.execute(
                "SELECT COUNT(*) FROM source_generations"
            ).fetchone()[0],
            "publications": self._connection.execute(
                "SELECT COUNT(*) FROM publications"
            ).fetchone()[0],
        }
