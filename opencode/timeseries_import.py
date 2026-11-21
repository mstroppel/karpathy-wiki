"""Archive only explicitly selected Home Assistant Recorder history."""

import argparse
import json
import math
import os
import re
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from fcntl import LOCK_EX, flock
from http.client import HTTPException
from pathlib import Path
from typing import Any, NamedTuple
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from karpathy_wiki_ingest.shared import PrivacyValidationError, TargetedAnonymizer

ARCHIVE = Path("/archive/recorder.sqlite3")
CONFIG = Path("/etc/wiki-timeseries/entities.json")
TOKEN = Path("/run/secrets/home_assistant_token")
REDACTIONS = Path("/run/secrets/redactions")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
MICROSECOND = timedelta(microseconds=1)
OVERLAP = timedelta(hours=1)
PERIOD = 15 * 60
HISTORY_BYTES = 32 * 1024 * 1024
ENTITY_ID = re.compile(r"[a-z0-9_]+\.[a-z0-9_]+")
NUMBER = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")

SCHEMA = """
CREATE TABLE IF NOT EXISTS series (
    series_id TEXT PRIMARY KEY, source TEXT NOT NULL, entity_id TEXT NOT NULL,
    display_name TEXT NOT NULL, description TEXT NOT NULL, unit TEXT,
    kind TEXT NOT NULL, timestamp_meaning TEXT NOT NULL,
    imported_through_utc TEXT NOT NULL, imported_through_us INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS samples (
    series_id TEXT NOT NULL REFERENCES series ON DELETE CASCADE,
    timestamp_us INTEGER NOT NULL, timestamp_utc TEXT NOT NULL,
    value ANY, quality TEXT NOT NULL, raw_value TEXT, unit TEXT, record_key TEXT NOT NULL,
    PRIMARY KEY (series_id, timestamp_us, record_key)
) STRICT, WITHOUT ROWID;
"""


class ResponseTooLarge(ValueError):
    pass


class SeriesState(NamedTuple):
    imported_through: datetime
    display_name: str
    description: str
    unit: str | None
    kind: str


def parse_timestamp(value: str) -> datetime:
    stamp = datetime.fromisoformat(value)
    if stamp.utcoffset() is None:
        raise ValueError("Recorder timestamp has no offset")
    return stamp.astimezone(UTC)


def iso(stamp: datetime) -> str:
    return stamp.isoformat(timespec="microseconds").replace("+00:00", "Z")


def epoch_us(stamp: datetime) -> int:
    return (stamp - EPOCH) // MICROSECOND


def selected_entities(config: Path) -> list[str]:
    data = json.loads(config.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not all(
        isinstance(item, str) and ENTITY_ID.fullmatch(item) for item in data
    ):
        raise ValueError("expected a JSON list of entity IDs")
    return list(dict.fromkeys(data))


@contextmanager
def archive_writer(path: Path) -> Iterator[sqlite3.Connection]:
    # Serialize import and admin deletion, but never hold a SQLite write
    # transaction open across a network request.
    with path.with_suffix(".lock").open("ab") as lock:
        flock(lock, LOCK_EX)
        with closing(sqlite3.connect(path, timeout=10)) as db:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA secure_delete=ON")
            db.executescript(SCHEMA)
            yield db


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Recorder:
    def __init__(self, base: str, token: str):
        url = urlsplit(base)
        if (
            url.scheme not in {"https", "http"}
            or not url.hostname
            or url.username is not None
            or url.query
            or url.fragment
            or not token
        ):
            raise ValueError("invalid Home Assistant URL or token")
        self.base = base.rstrip("/")
        self.token = token
        self.opener = build_opener(ProxyHandler({}), NoRedirects())

    def get(self, path: str, limit: int, *, not_found: bool = False) -> Any:
        request = Request(self.base + path, headers={"Authorization": f"Bearer {self.token}"})
        try:
            with self.opener.open(request, timeout=30) as response:
                data = response.read(limit + 1)
        except HTTPError as exc:
            exc.close()
            if not_found and exc.code == 404:
                return None
            raise
        if len(data) > limit:
            raise ResponseTooLarge()
        return json.loads(data)

    def metadata(self, entity: str) -> dict[str, Any] | None:
        state = self.get(f"/api/states/{entity}", 1024 * 1024, not_found=True)
        return None if state is None else attributes(state, entity)

    def windows(
        self, entity: str, start: datetime, end: datetime
    ) -> Iterator[tuple[datetime, list[dict[str, Any]]]]:
        params = urlencode(
            {
                "filter_entity_id": entity,
                "end_time": iso(end),
                "significant_changes_only": "0",
                "skip_initial_state": "1",
            }
        )
        try:
            result = self.get(f"/api/history/period/{iso(start)}?{params}", HISTORY_BYTES)
        except ResponseTooLarge:
            if end - start <= timedelta(minutes=1):
                raise
            middle = start + (end - start) // 2
            yield from self.windows(entity, start, middle)
            # The Recorder query can exclude both endpoints.
            yield from self.windows(entity, middle - MICROSECOND, end)
            return
        if not isinstance(result, list) or len(result) > 1:
            raise ValueError("unexpected Recorder history shape")
        rows = result[0] if result else []
        if not isinstance(rows, list):
            raise ValueError("unexpected Recorder row group")
        for row in rows:
            attributes(row, entity)
        yield end, rows


def attributes(row: Any, entity: str) -> dict[str, Any]:
    if not isinstance(row, dict) or row.get("entity_id") != entity:
        raise ValueError("unexpected Recorder entity")
    result = row.get("attributes", {})
    if not isinstance(result, dict):
        raise ValueError("unexpected Recorder attributes")
    return result


def text(attrs: dict[str, Any], key: str) -> str | None:
    value = attrs.get(key)
    return value if isinstance(value, str) and value.strip() else None


def redact_text(anonymizer: TargetedAnonymizer, value: str) -> str:
    try:
        return anonymizer.anonymize(value)[0]
    except PrivacyValidationError:
        # Omit a field that cannot be safely redacted, without losing samples.
        return ""


def redact_archived_metadata(db: sqlite3.Connection, anonymizer: TargetedAnonymizer) -> None:
    # Apply newly added rules even to deselected series or when Recorder is down.
    with db:
        for series_id, name, description in db.execute(
            "SELECT series_id, display_name, description FROM series"
        ).fetchall():
            safe_name = redact_text(anonymizer, name)
            safe_description = redact_text(anonymizer, description)
            if (safe_name, safe_description) != (name, description):
                db.execute(
                    "UPDATE series SET display_name=?, description=? WHERE series_id=?",
                    (safe_name, safe_description, series_id),
                )


def typed_value(raw: str | None, measurement: bool) -> tuple[str | int | float | None, str]:
    if raw is None:
        return None, "missing"
    if raw in {"unknown", "unavailable"}:
        return None, raw
    if not measurement:
        return raw, "valid"
    if NUMBER.fullmatch(raw):
        if re.fullmatch(r"[+-]?[0-9]{1,19}", raw) and -(2**63) <= (value := int(raw)) < 2**63:
            return value, "valid"
        number = float(raw)
        if math.isfinite(number):
            return number, "valid"
    return None, "invalid"


def store_window(
    db: sqlite3.Connection,
    entity: str,
    current: dict[str, Any] | None,
    old: SeriesState | None,
    end: datetime,
    points: list[dict[str, Any]],
    anonymizer: TargetedAnonymizer,
) -> SeriesState:
    series_id = f"ha:{entity}"
    # Use an actual archived row for removed entities, never a guessed unit.
    latest = max(points, key=lambda row: parse_timestamp(row["last_updated"]), default=None)
    meta = current if current is not None else latest.get("attributes", {}) if latest else None
    if meta is None:
        display = old.display_name if old else redact_text(anonymizer, entity)
        description = old.description if old else ""
        unit = old.unit if old else None
        kind = old.kind if old else "state"
    else:
        display = redact_text(anonymizer, text(meta, "friendly_name") or entity)
        description = redact_text(anonymizer, text(meta, "description") or "")
        unit = text(meta, "unit_of_measurement")
        # A later unavailable state without measurement metadata must not
        # turn a previously detected measurement into a categorical series.
        kind = (
            "measurement"
            if (
                unit
                or text(meta, "state_class")
                or (old and old.kind == "measurement")
                or any(
                    text(row.get("attributes", {}), "unit_of_measurement")
                    or text(row.get("attributes", {}), "state_class")
                    for row in points
                )
            )
            else "state"
        )

    def records():
        for row in points:
            stamp = parse_timestamp(row["last_updated"])
            if stamp < EPOCH or stamp > end:
                raise ValueError("Recorder timestamp outside requested window")
            raw = row.get("state")
            if raw is not None and not isinstance(raw, str):
                raise ValueError("unexpected Recorder value")
            sample_unit = text(row.get("attributes", {}), "unit_of_measurement")
            value, quality = typed_value(raw, kind == "measurement")
            key = json.dumps([raw, sample_unit], separators=(",", ":"))
            yield series_id, epoch_us(stamp), iso(stamp), value, quality, raw, sample_unit, key

    # During a split backfill, a chunk can cover only part of the overlap.
    cutoff = max(old.imported_through, end) if old else end
    with db:
        db.execute(
            """
            INSERT INTO series VALUES (?, 'home_assistant', ?, ?, ?, ?, ?, 'last_updated', ?, ?)
            ON CONFLICT(series_id) DO UPDATE SET display_name=excluded.display_name,
                description=excluded.description, unit=excluded.unit, kind=excluded.kind,
                imported_through_utc=excluded.imported_through_utc,
                imported_through_us=excluded.imported_through_us
        """,
            (series_id, entity, display, description, unit, kind, iso(cutoff), epoch_us(cutoff)),
        )
        db.executemany(
            """
            INSERT INTO samples VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT DO NOTHING
        """,
            records(),
        )
    return SeriesState(cutoff, display, description, unit, kind)


def import_once(
    archive: Path, config: Path, base: str, token: str, *, redactions: Path, through: datetime
) -> int:
    anonymizer = TargetedAnonymizer.from_file(redactions)
    failures = 0
    with archive_writer(archive) as db:
        redact_archived_metadata(db, anonymizer)
        ha = Recorder(base, token)
        for entity in selected_entities(config):
            try:
                start = EPOCH - MICROSECOND
                previous = db.execute(
                    """
                    SELECT imported_through_utc, display_name, description, unit, kind
                    FROM series WHERE series_id = ?
                """,
                    (f"ha:{entity}",),
                ).fetchone()
                old = SeriesState(parse_timestamp(previous[0]), *previous[1:]) if previous else None
                if old:
                    if through <= old.imported_through:
                        continue
                    start = max(start, old.imported_through - OVERLAP)
                current = ha.metadata(entity)
                for end, points in ha.windows(entity, start, through):
                    old = store_window(db, entity, current, old, end, points, anonymizer)
            except (OSError, HTTPException, ValueError, KeyError, TypeError, sqlite3.Error) as exc:
                failures += 1
                # Never emit history, hostnames, tokens or private entity IDs.
                print(
                    f"Recorder import failed for a selected entity ({type(exc).__name__})",
                    file=sys.stderr,
                )
    return failures


def delete_series(archive: Path, config: Path, entity: str) -> bool:
    if not ENTITY_ID.fullmatch(entity):
        raise ValueError("invalid entity ID")
    with archive_writer(archive) as db, db:
        if entity in selected_entities(config):
            raise ValueError("remove the entity from the import list before deleting its archive")
        return db.execute("DELETE FROM series WHERE series_id=?", (f"ha:{entity}",)).rowcount == 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="mode", required=True)
    commands.add_parser("poll")
    commands.add_parser("once")
    delete = commands.add_parser("delete")
    delete.add_argument("entity_id")
    delete.add_argument("--confirm", action="store_true", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    if args.mode == "delete":
        try:
            removed = delete_series(ARCHIVE, CONFIG, args.entity_id)
        except (OSError, ValueError, sqlite3.Error):
            print(
                "Cannot delete: check the archive and deselect the entity first.", file=sys.stderr
            )
            return 1
        print("Series deleted." if removed else "No archived series found.")
        return 0
    while True:
        started = time.monotonic()
        try:
            failures = import_once(
                ARCHIVE,
                CONFIG,
                os.environ.get("HOME_ASSISTANT_URL", ""),
                TOKEN.read_text(encoding="utf-8").strip(),
                redactions=Path(os.environ.get("REDACTIONS_FILE", str(REDACTIONS))),
                through=datetime.now(UTC),
            )
        except (OSError, ValueError, sqlite3.Error) as exc:
            print(f"Recorder import could not start ({type(exc).__name__})", file=sys.stderr)
            failures = 1
        if args.mode == "once":
            return int(bool(failures))
        time.sleep(max(0, PERIOD - (time.monotonic() - started)))


if __name__ == "__main__":
    raise SystemExit(main())
