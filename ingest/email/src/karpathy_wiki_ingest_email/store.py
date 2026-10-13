"""Private restart-safe archive ledger; FULL SQLite commits precede IMAP deletion.

This is a new provider-local schema, not a migration of the shared job store.
One advisory process lock serializes receive, extraction, publication and replay.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import sqlite3
import stat
import time
import uuid
from pathlib import Path


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class Store:
    def __init__(self, root: Path):
        self.root = root
        missing = []
        parent = root
        while not parent.exists():
            missing.append(parent)
            parent = parent.parent
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        for directory in reversed(missing):
            fsync_directory(directory)
            fsync_directory(directory.parent)
        self.lock = (root / ".lock").open("a+b")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.db = sqlite3.connect(root / "mail.sqlite3")
            self.db.row_factory = sqlite3.Row
            self.db.execute("PRAGMA journal_mode=DELETE")
            self.db.execute("PRAGMA synchronous=FULL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL,
                    original TEXT NOT NULL, output TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt INTEGER NOT NULL DEFAULT 0, error TEXT
                );
                CREATE TABLE IF NOT EXISTS originals (
                    id TEXT PRIMARY KEY, sha256 TEXT UNIQUE NOT NULL,
                    source_id TEXT NOT NULL REFERENCES sources(id)
                );
                CREATE TABLE IF NOT EXISTS deliveries (
                    scope TEXT NOT NULL, epoch TEXT NOT NULL, uid TEXT NOT NULL,
                    original TEXT NOT NULL REFERENCES originals(id),
                    deleted INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(scope, epoch, uid)
                );
                CREATE TABLE IF NOT EXISTS cursors (
                    scope TEXT PRIMARY KEY, uid INTEGER NOT NULL
                );
            """)
            (root / "mail.sqlite3").chmod(0o600)
            fsync_directory(root)
        except BaseException:
            if hasattr(self, "db"):
                self.db.close()
            self.lock.close()
            raise

    def close(self) -> None:
        self.db.close()
        self.lock.close()

    def read(self, original: str) -> bytes:
        if len(original) != 32 or any(c not in "0123456789abcdef" for c in original):
            raise ValueError("invalid original ID")
        path = self.root / f"{original}.eml"
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("original is not a regular file")
            raw = handle.read()
        row = self.db.execute("SELECT sha256 FROM originals WHERE id=?", (original,)).fetchone()
        if row is None or digest(raw) != row["sha256"]:
            raise ValueError("original archive integrity failure")
        return raw

    def archive(self, raw: bytes, identity: str, scope: str, epoch: str, uid: str) -> str:
        existing = self.db.execute(
            "SELECT id, source_id FROM originals WHERE sha256=?", (digest(raw),)
        ).fetchone()
        if existing:
            original, source_id = existing["id"], existing["source_id"]
            self.read(original)
        else:
            original = uuid.uuid4().hex
            source = self.db.execute(
                "SELECT id FROM sources WHERE identity=?", (identity,)
            ).fetchone()
            source_id = source["id"] if source else uuid.uuid4().hex
            temporary = self.root / f".{original}.tmp"
            path = self.root / f"{original}.eml"
            try:
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(raw)
                    handle.flush()
                    os.fsync(handle.fileno())
                if temporary.read_bytes() != raw:
                    raise OSError("archive verification failed")
                os.link(temporary, path)  # never overwrite an original
                fsync_directory(self.root)
            finally:
                temporary.unlink(missing_ok=True)
            with self.db:
                if not source:
                    self.db.execute(
                        "INSERT INTO sources(id, identity, original) VALUES(?,?,?)",
                        (source_id, identity, original),
                    )
                self.db.execute(
                    "INSERT INTO originals VALUES(?,?,?)", (original, digest(raw), source_id)
                )
        # A crash before this commit leaves an orphan .eml, but no deletion.
        with self.db:
            self.db.execute(
                "INSERT INTO deliveries(scope,epoch,uid,original) VALUES(?,?,?,?) "
                "ON CONFLICT(scope,epoch,uid) DO NOTHING",
                (scope, epoch, uid, original),
            )
        return original

    def delivery(self, scope: str, epoch: str, uid: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM deliveries WHERE scope=? AND epoch=? AND uid=?",
            (scope, epoch, uid),
        ).fetchone()

    def deleted(self, scope: str, epoch: str, uid: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE deliveries SET deleted=1 WHERE scope=? AND epoch=? AND uid=?",
                (scope, epoch, uid),
            )

    def cursor(self, scope: str) -> int:
        row = self.db.execute("SELECT uid FROM cursors WHERE scope=?", (scope,)).fetchone()
        return row["uid"] if row else 0

    def advance(self, scope: str, uid: str) -> None:
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO cursors VALUES(?,?)", (scope, int(uid)))

    def sources(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM sources ORDER BY id"))

    def succeeded(self, source_id: str, output: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE sources SET output=?, error=NULL, attempts=0, next_attempt=0 WHERE id=?",
                (output, source_id),
            )

    def failed(self, source_id: str, error: str) -> None:
        with self.db:
            self.db.execute(
                "UPDATE sources SET output=NULL, error=?, attempts=attempts+1, "
                "next_attempt=? WHERE id=?",
                (error, int(time.time()) + 3600, source_id),
            )

    def rearm(self, source_id: str | None) -> None:
        if (
            source_id is not None
            and not self.db.execute("SELECT 1 FROM sources WHERE id=?", (source_id,)).fetchone()
        ):
            raise ValueError("unknown source ID")
        with self.db:
            query = "UPDATE sources SET output=NULL, attempts=0, next_attempt=0, error=NULL"
            self.db.execute(
                query if source_id is None else query + " WHERE id=?",
                () if source_id is None else (source_id,),
            )
