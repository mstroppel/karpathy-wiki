"""Bounded SQLite reads over a local Unix socket, in a networkless service."""

import json
import math
import os
import signal
import socketserver
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

ARCHIVE = Path("/archive/recorder.sqlite3")
SOCKET = Path("/timeseries-socket/query.sock")
SQL_BYTES = 12_000
RESULT_BYTES = 15_000
ROWS = 200

# Do not expose arbitrary extensions, table functions, PRAGMAs, or file I/O.
FUNCTIONS = frozenset(
    """
abs avg ceil ceiling coalesce count cume_dist date datetime dense_rank floor format
glob group_concat hex ifnull iif instr julianday lag last_value lead length like
likelihood likely lower ltrim max min nth_value ntile nullif percent_rank printf
quote rank replace round row_number rtrim sign strftime substr substring sum time
total trim typeof unixepoch unlikely upper first_value
""".split()
)


def authorize(action, first, second, database, _source):
    if action in {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE}:
        return sqlite3.SQLITE_OK
    if (
        action == sqlite3.SQLITE_READ
        and first in {"series", "samples"}
        # SQLite reports no schema or column for a table-only count(*).
        and (database == "main" or (database is None and second == ""))
    ):
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_FUNCTION and second in FUNCTIONS:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def encode(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def display_cell(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"blob_hex": value.hex()}
    if isinstance(value, float) and not math.isfinite(value):
        return {"non_finite": str(value)}
    # The exact timestamp_us fits safely as a JSON number for present-day dates,
    # but arbitrary SQLite aggregates can exceed the consumer's integer range.
    if isinstance(value, int) and abs(value) > 2**53 - 1:
        return {"integer": str(value)}
    return value


def query(archive: Path, sql: str) -> dict[str, Any]:
    if not 0 < len(sql.encode()) <= SQL_BYTES:
        raise ValueError("SQL size limit")
    deadline = time.monotonic() + 1.5
    with closing(sqlite3.connect(archive.as_uri() + "?mode=ro", uri=True, timeout=0.25)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("PRAGMA trusted_schema=OFF")
        db.execute("PRAGMA temp_store=MEMORY")
        db.execute("PRAGMA hard_heap_limit=33554432")
        db.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 64_000)
        db.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, SQL_BYTES)
        db.setlimit(sqlite3.SQLITE_LIMIT_COLUMN, 64)
        db.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        db.set_authorizer(authorize)
        result = db.execute(sql)
        if result.description is None:
            raise ValueError("expected a SELECT")
        columns = [column[0] for column in result.description]
        rows: list[list[Any]] = []
        response = {"columns": columns, "rows": rows, "truncated": False}
        remaining = RESULT_BYTES - len(encode(response))
        if remaining <= 0:
            raise ValueError("column names exceed output limit")
        for row in result:
            cells = [display_cell(cell) for cell in row]
            remaining -= len(encode(cells)) + 1
            if len(rows) == ROWS or remaining < 0:
                response["truncated"] = True
                break
            rows.append(cells)
        return response


class QueryRequest(socketserver.BaseRequestHandler):
    def handle(self):
        try:
            deadline = time.monotonic() + 2
            data = bytearray()
            while b"\n" not in data and len(data) <= SQL_BYTES * 6 + 2:
                self.request.settimeout(max(0.001, deadline - time.monotonic()))
                chunk = self.request.recv(4096)
                if not chunk or time.monotonic() >= deadline:
                    return
                data.extend(chunk)
            sql = json.loads(data)
            if not isinstance(sql, str):
                raise ValueError("expected SQL string")
            reply = query(ARCHIVE, sql)
        except sqlite3.Error as exc:
            code = getattr(exc, "sqlite_errorname", "SQLITE_ERROR")
            reply = {"error": f"SQL rejected or archive unavailable ({code})."}
        except (OSError, ValueError, OverflowError, RecursionError, MemoryError):
            reply = {"error": "Invalid SQL request or limit exceeded."}
        try:
            self.request.settimeout(1)
            self.request.sendall(encode(reply))
        except OSError:
            pass  # Client disconnected; never log SQL or archive contents.


def main():
    os.umask(0o077)
    if SOCKET.is_socket():
        SOCKET.unlink()

    def stop(_signal, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, stop)
    with socketserver.UnixStreamServer(str(SOCKET), QueryRequest) as server:
        try:
            server.serve_forever()
        finally:
            SOCKET.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
