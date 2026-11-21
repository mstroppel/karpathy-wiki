"""Synthetic HA Recorder and model; exercise two prompts in one real session."""

import base64
import json
import os
import sys
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import parse_qs, urlsplit
from urllib.request import Request, urlopen

SLEEP = "sensor.synthetic_owlet"
TEMP = "sensor.synthetic_room_temperature"
QUESTION = "Wie verteilt sich die Raumtemperatur während der von Owlet erkannten Tiefschlafphasen?"
FOLLOW_UP = "Zeig nur Nächte mit mindestens zwei Stunden gemeinsamer Datenabdeckung."
RECORDER_CAVEAT = (
    "Ein stummer Recorder-Ausfall ist ohne Einträge nicht vom stabilen Zustand unterscheidbar."
)
TITLES = ["Synthetischer Schlafzustand", "Synthetische Raumtemperatur"]
NIGHTS = ["2026-10-01", "2026-10-02"]
SLEEP_ROWS = [
    (0, "00:00", "deep"),
    (0, "02:30", "awake"),
    (1, "00:00", "deep"),
    (1, "02:45", "unknown"),
    (1, "03:30", "awake"),
]
TEMP_ROWS = [
    (0, "00:00", "18"),
    (0, "00:30", "18"),
    (0, "01:00", "20"),
    (0, "01:30", "20"),
    (0, "02:00", "20"),
    (0, "02:30", "21"),
    (1, "00:00", "19"),
    (1, "00:30", "unavailable"),
    (1, "02:15", "23"),
    (1, "03:00", "24"),
]
EXPECTED = [[NIGHTS[0], 18, 60], [NIGHTS[0], 20, 90], [NIGHTS[1], 19, 30], [NIGHTS[1], 23, 30]]
CATALOG = "SELECT display_name, kind, imported_through_utc FROM series ORDER BY series_id"

# Model fixture SQL, not an application feature. Integrate only the intersections
# of bounded measurement intervals and valid deep-sleep intervals.
DISTRIBUTION = """
WITH timeline AS (
    SELECT *, lead(timestamp_us) OVER (PARTITION BY series_id ORDER BY timestamp_us) AS next_us
    FROM samples
), overlap AS (
    SELECT substr(s.timestamp_utc, 1, 10) AS night, t.value,
        min(s.next_us, t.next_us, t.timestamp_us+1800000000)
        - max(s.timestamp_us, t.timestamp_us) AS duration
    FROM timeline s JOIN timeline t
        ON t.timestamp_us < s.next_us AND t.next_us > s.timestamp_us
    WHERE s.series_id='ha:sensor.synthetic_owlet' AND s.quality='valid' AND s.value='deep'
      AND t.series_id='ha:sensor.synthetic_room_temperature' AND t.quality='valid' AND t.unit='°C'
), bins AS (
    SELECT night, value, sum(duration)/60000000 AS minutes
    FROM overlap WHERE duration > 0 GROUP BY night, value
), coverage AS (
    SELECT *, sum(minutes) OVER (PARTITION BY night) AS covered FROM bins
)
SELECT night, value, minutes FROM coverage WHERE covered >= {minimum} ORDER BY night, value
"""


def entries(entity):
    measurement = entity == TEMP
    attrs = {"friendly_name": TITLES[measurement]}
    if measurement:
        attrs["unit_of_measurement"] = "°C"
    return [
        {
            "entity_id": entity,
            "state": value,
            "last_updated": f"{NIGHTS[night]}T{stamp}:00Z",
            "attributes": attrs,
        }
        for night, stamp, value in (TEMP_ROWS if measurement else SLEEP_ROWS)
    ]


def complete(body):
    messages = body["messages"]
    tool_names = {item["function"]["name"] for item in body.get("tools", [])}
    if not tool_names:  # Session title generation is not a question.
        return {"role": "assistant", "content": "Synthetische Zeitreihen"}, "stop"
    assert "wiki_timeseries_sql" in tool_names, sorted(tool_names)
    system_text = " ".join(
        json.dumps([item for item in messages if item["role"] == "system"], ensure_ascii=False)
        .replace("\\n", " ")
        .split()
    )
    assert "Aus Zeit ohne Recorder-Einträge lässt sich ein stummer Ausfall nicht" in system_text
    index, question = [
        (index, question)
        for index, item in enumerate(messages)
        if item["role"] == "user"
        for question in (QUESTION, FOLLOW_UP)
        if question in json.dumps(item, ensure_ascii=False)
    ][-1]
    results = [item["content"] for item in messages[index + 1 :] if item["role"] == "tool"]
    follow_up = question == FOLLOW_UP
    if not results:
        sql = CATALOG
    else:
        catalog = json.loads(results[0])
        assert [row[:2] for row in catalog["rows"]] == [
            [TITLES[0], "state"],
            [TITLES[1], "measurement"],
        ], catalog
        assert all(row[2] > "2026-10-02T03:30" for row in catalog["rows"]), catalog
        sql = DISTRIBUTION.format(minimum=120 if follow_up else 0)
    if len(results) == 2:
        result = json.loads(results[-1])
        assert not result["truncated"], result
        assert result["rows"] == (EXPECTED[:2] if follow_up else EXPECTED), result
        rows = "; ".join(
            f"{night}: {degrees} °C für {minutes} min" for night, degrees, minutes in result["rows"]
        )
        suffix = (
            "Mindestabdeckung 120 min; 02.10. ausgeschlossen (nur 60 min)."
            if follow_up
            else "Abdeckung: 150 min und 60 min."
        )
        data_as_of = min(row[2] for row in catalog["rows"])
        report = (
            f"{rows}. {suffix} Zeitraum: 01.–02.10. UTC; Methode: 30 min Haltedauer, Schnittmenge."
            f" Datenstand: {data_as_of}. {RECORDER_CAVEAT}"
        )
        return {"role": "assistant", "content": report}, "stop"
    return {
        "role": "assistant",
        "tool_calls": [
            {
                "index": 0,
                "id": f"call_{int(follow_up)}_{len(results)}",
                "type": "function",
                "function": {"name": "wiki_timeseries_sql", "arguments": json.dumps({"sql": sql})},
            }
        ],
    }, "tool_calls"


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/ready":
            self.send_response(200)
            self.end_headers()
            return
        assert self.headers["Authorization"] == "Bearer synthetic-recorder"
        url = urlsplit(self.path)
        if url.path.startswith("/api/states/"):
            entity = url.path.split("/")[-1]
            assert entity in {SLEEP, TEMP}
            payload = entries(entity)[-1]
        else:
            assert url.path.startswith("/api/history/period/")
            params = parse_qs(url.query)
            (entity,) = params["filter_entity_id"]
            assert entity in {SLEEP, TEMP}
            assert params["significant_changes_only"] == ["0"] and "skip_initial_state" in params
            start = datetime.fromisoformat(url.path.split("/")[-1])
            end = datetime.fromisoformat(params["end_time"][0])
            payload = [
                [
                    row
                    for row in entries(entity)
                    if start < datetime.fromisoformat(row["last_updated"]) < end
                ]
            ]
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def do_POST(self):
        assert self.path == "/v1/chat/completions"
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        delta, finish = complete(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        base = {
            "id": "chatcmpl-timeseries-fixture",
            "object": "chat.completion.chunk",
            "created": 1760000000,
            "model": "synthetic",
        }
        for chunk in (
            {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {
                **base,
                "choices": [{"index": 0, "delta": {}, "finish_reason": finish}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        ):
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
        self.wfile.write(b"data: [DONE]\n\n")


def api(path, body=None):
    token = base64.b64encode(f"opencode:{os.environ['OPENCODE_PASSWORD']}".encode()).decode()
    request = Request(
        f"http://127.0.0.1:4096{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
    )
    with urlopen(request, timeout=45) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def chat():
    for _ in range(45):
        try:
            api("/api/info")
            break
        except URLError:
            time.sleep(1)
    else:
        raise AssertionError("Backend startup timed out")
    sid = api(
        "/api/session",
        {
            "agent": "build",
            "model": {"providerID": "fake", "id": "synthetic"},
            "location": {"directory": "/knowledge/wiki"},
        },
    )["data"]["id"]
    for question, expected in (
        (QUESTION, "Abdeckung: 150 min und 60 min"),
        (FOLLOW_UP, "02.10. ausgeschlossen (nur 60 min)"),
    ):
        api(f"/api/session/{sid}/prompt", {"text": question})
        api(f"/api/experimental/session/{sid}/wait", {})
        transcript = api(f"/api/experimental/session/{sid}/export")
        assert api(f"/api/session/{sid}")["data"]["outcome"] == "succeeded", transcript
        texts = (
            part.get("text", "")
            for message in transcript["data"]["messages"]
            for part in message.get("content", [])
            if part.get("type") == "text"
        )
        assert any(expected in text and RECORDER_CAVEAT in text for text in texts), transcript


if __name__ == "__main__":
    if sys.argv[1] == "fixture":
        ThreadingHTTPServer(("0.0.0.0", 4081), Fixture).serve_forever()
    else:
        chat()
