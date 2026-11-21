import json
import sqlite3
import threading
import unittest
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlsplit

from opencode.timeseries_import import delete_series, import_once
from opencode.timeseries_query import query

SLEEP = "sensor.sleep"
TEMP = "sensor.temperature"


def entry(entity, time, value, **attributes):
    return {"entity_id": entity, "last_updated": time, "state": value, "attributes": attributes}


class RecorderFixture:
    def __init__(self):
        self.requests: list[tuple[str, str] | tuple[str, str, datetime]] = []
        self.fail: set[str] = set()
        self.states = {
            SLEEP: entry(SLEEP, "2026-01-01T00:00:00Z", "awake", friendly_name="Synthetic sleep"),
            TEMP: entry(
                TEMP, "2026-01-01T00:00:00Z", "0", unit_of_measurement="°C", description="Fixture"
            ),
        }
        self.history = {
            SLEEP: [entry(SLEEP, "2026-01-01T00:00:00.123456Z", "deep")],
            TEMP: [
                entry(TEMP, "2026-01-01T00:00:00Z", "0", unit_of_measurement="°C"),
                entry(TEMP, "2026-01-01T00:01:00Z", "unknown"),
                entry(TEMP, "2026-01-01T00:02:00Z", "unavailable"),
                entry(TEMP, "2026-01-01T00:03:00Z", None),
            ],
        }

        class Handler(BaseHTTPRequestHandler):
            def do_GET(handler):
                path = urlsplit(handler.path)
                params = parse_qs(path.query, keep_blank_values=True)
                if handler.headers.get("Authorization") != "Bearer synthetic":
                    handler.send_error(401)
                    return
                if path.path.startswith("/api/states/"):
                    entity = path.path.split("/")[-1]
                    self.requests.append(("state", entity))
                    payload = self.states.get(entity)
                elif path.path.startswith("/api/history/period/"):
                    entity = params["filter_entity_id"][0]
                    start = datetime.fromisoformat(path.path.split("/")[-1])
                    end = datetime.fromisoformat(params["end_time"][0])
                    self.requests.append(("history", entity, start))
                    if entity in self.fail:
                        handler.send_error(503)
                        return
                    if (
                        params.get("significant_changes_only") != ["0"]
                        or "skip_initial_state" not in params
                    ):
                        handler.send_error(400)
                        return
                    payload = [
                        [
                            point
                            for point in self.history[entity]
                            if start < datetime.fromisoformat(point["last_updated"]) < end
                        ]
                    ]
                else:
                    payload = None
                if payload is None:
                    handler.send_error(404)
                    return
                data = json.dumps(payload).encode()
                handler.send_response(200)
                handler.send_header("Content-Length", str(len(data)))
                handler.end_headers()
                handler.wfile.write(data)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


class TimeseriesImportTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.archive = Path(directory.name) / "archive.sqlite3"
        self.config = Path(directory.name) / "entities.json"
        self.config.write_text(json.dumps([SLEEP, TEMP]))
        self.recorder = RecorderFixture()
        self.addCleanup(self.recorder.close)

    def run_import(self, hour):
        return import_once(
            self.archive,
            self.config,
            self.recorder.url,
            "synthetic",
            through=datetime(2026, 1, 1, hour, tzinfo=UTC),
        )

    def read(self, sql):
        return [tuple(row) for row in query(self.archive, sql)["rows"]]

    def test_imports_only_opted_in_history_with_types_and_quality(self):
        self.assertEqual(self.run_import(1), 0)
        self.assertEqual({request[1] for request in self.recorder.requests}, {SLEEP, TEMP})
        self.assertEqual(
            self.read(
                "SELECT series_id, source, display_name, description, unit, kind, "
                "timestamp_meaning FROM series ORDER BY series_id"
            ),
            [
                (
                    "ha:sensor.sleep",
                    "home_assistant",
                    "Synthetic sleep",
                    "",
                    None,
                    "state",
                    "last_updated",
                ),
                (
                    "ha:sensor.temperature",
                    "home_assistant",
                    TEMP,
                    "Fixture",
                    "°C",
                    "measurement",
                    "last_updated",
                ),
            ],
        )
        self.assertEqual(
            self.read(
                "SELECT value, quality, timestamp_utc FROM samples ORDER BY series_id, timestamp_us"
            ),
            [
                ("deep", "valid", "2026-01-01T00:00:00.123456Z"),
                (0, "valid", "2026-01-01T00:00:00.000000Z"),
                (None, "unknown", "2026-01-01T00:01:00.000000Z"),
                (None, "unavailable", "2026-01-01T00:02:00.000000Z"),
                (None, "missing", "2026-01-01T00:03:00.000000Z"),
            ],
        )

    def test_catches_up_after_pause_and_does_not_repeat_overlap_or_import_deselected(self):
        self.assertEqual(self.run_import(1), 0)
        self.recorder.fail.add(SLEEP)
        self.assertEqual(self.run_import(2), 1)
        self.recorder.fail.clear()
        self.recorder.history[SLEEP].append(entry(SLEEP, "2026-01-01T01:30:00Z", "awake"))
        self.recorder.history[TEMP].append(entry(TEMP, "2026-01-01T02:30:00Z", "10"))
        self.config.write_text(json.dumps([SLEEP]))
        self.recorder.requests.clear()
        self.assertEqual(self.run_import(4), 0)
        self.assertEqual(self.run_import(5), 0)
        first_history = next(
            request for request in self.recorder.requests if request[0] == "history"
        )
        self.assertEqual(first_history, ("history", SLEEP, datetime(2026, 1, 1, tzinfo=UTC)))
        self.assertEqual({request[1] for request in self.recorder.requests}, {SLEEP})
        self.assertEqual(
            self.read(
                "SELECT value FROM samples WHERE series_id = 'ha:sensor.sleep' "
                "ORDER BY timestamp_us"
            ),
            [("deep",), ("awake",)],
        )
        self.assertEqual(
            self.read("SELECT count(*) FROM samples WHERE series_id = 'ha:sensor.temperature'"),
            [(4,)],
        )

    def test_offline_query_accepts_ctes_and_windows_but_rejects_writes(self):
        self.assertEqual(self.run_import(1), 0)
        self.assertEqual(
            self.read("""
            WITH timeline AS (
                SELECT quality, lead(timestamp_us) OVER (ORDER BY timestamp_us) - timestamp_us AS us
                FROM samples WHERE series_id='ha:sensor.temperature'
            )
            SELECT sum(us) FROM timeline WHERE quality='valid'
        """),
            [(60_000_000,)],
        )
        for sql in (
            "DELETE FROM samples",
            "PRAGMA user_version",
            "SELECT * FROM sqlite_schema",
            "SELECT 1; SELECT 2",
        ):
            with self.subTest(sql=sql), self.assertRaises(sqlite3.Error):
                query(self.archive, sql)
        self.assertEqual(self.read("SELECT 1 AS answer LIMIT 0"), [])
        self.assertEqual(self.read("SELECT count(*) FROM samples"), [(5,)])

    def test_admin_deletes_only_deselected_series(self):
        self.assertEqual(self.run_import(1), 0)
        with self.assertRaises(ValueError):
            delete_series(self.archive, self.config, TEMP)
        self.config.write_text(json.dumps([SLEEP]))
        self.assertTrue(delete_series(self.archive, self.config, TEMP))
        self.assertFalse(delete_series(self.archive, self.config, TEMP))
        self.assertEqual(self.read("SELECT entity_id FROM series"), [(SLEEP,)])
        self.assertEqual(self.read("SELECT count(*) FROM samples"), [(1,)])
