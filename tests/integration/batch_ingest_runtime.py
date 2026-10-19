"""Disposable pinned-runtime proof using a local synthetic tool-calling model."""

import base64
import hashlib
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WIKI = Path("/knowledge/wiki")
SOURCE = Path("/knowledge/sources/webdav")
RESULT = [
    {
        "index": 0,
        "content": "Synthetic finding, source line 1.",
        "contradictions": "None observed.",
        "extraction_limits": "Fully read line 1.",
    }
]
CALLS: list[str] = []


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body.get("messages", [])
        worker = "Batchdaten:" in json.dumps(messages)
        tools = {tool["function"]["name"]: tool["function"] for tool in body.get("tools", [])}
        names = list(tools)
        content = "Synthetic batch completed."
        call = None
        history = [message for message in messages if message.get("role") == "tool"]
        if not worker and not history:
            name = next(name for name in names if name.endswith("wiki_ingest_batch"))
            call = (name, {})
        elif worker and not history:
            # Exercise real worker read permission, not a plugin-owned file read.
            name = next(name for name in names if name == "read")
            props = tools[name]["parameters"]["properties"]
            key = next(key for key in ("path", "filePath", "file_path") if key in props)
            call = (name, {key: str(SOURCE / "notes.md")})
        elif worker and len(history) == 1:
            name = next(name for name in names if name == "write")
            props = tools[name]["parameters"]["properties"]
            key = next(key for key in ("path", "filePath", "file_path") if key in props)
            call = (
                name,
                {
                    key: str(WIKI / "sources/webdav/notes.md"),
                    "content": "# Synthetic finding\nSource line 1.\n",
                },
            )
        elif worker:
            content = json.dumps(RESULT)
        if call:
            name, args = call
            CALLS.append(name)
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call_{len(CALLS)}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": content}
            finish = "stop"
        base = {
            "id": "chatcmpl-batch-fixture",
            "object": "chat.completion.chunk",
            "created": 1760000000,
            "model": "synthetic",
        }
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
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


def request(route, data=None):
    token = base64.b64encode(f"opencode:{os.environ['OPENCODE_PASSWORD']}".encode()).decode()
    req = urllib.request.Request(
        f"http://127.0.0.1:4096{route}",
        data=None if data is None else json.dumps(data).encode(),
        headers={"Authorization": f"Basic {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        raw = response.read()
        return json.loads(raw) if raw else None


def main():
    WIKI.joinpath("sources/webdav").mkdir(parents=True)
    SOURCE.mkdir(parents=True)
    for name in ("index.md", "overview.md", "log.md", "AGENTS.md"):
        WIKI.joinpath(name).write_text(f"# Synthetic {name}\n")
    text = "Synthetic source line 1.\n"
    SOURCE.joinpath("notes.md").write_text(text)
    revision = hashlib.sha256(text.encode()).hexdigest()
    SOURCE.joinpath("manifest.json").write_text(
        json.dumps(
            {
                "contract": "karpathy-wiki-provider-manifest",
                "version": 1,
                "source": "webdav",
                "wiki_root": "webdav",
                "generated_at": 1760000000,
                "items": [
                    {
                        "source_key": "notes.md",
                        "source_path": "notes.md",
                        "wiki_path": "webdav/notes.md",
                        "source_revision": revision,
                        "frontmatter": {
                            "source": "webdav",
                            "source_path": "notes.md",
                            "source_revision": revision,
                        },
                        "claim": {"source": "webdav", "source_path": "notes.md"},
                    }
                ],
                "revoked": [],
                "errors": [],
            }
        )
    )
    for args in (
        ("init", "-q"),
        ("config", "user.name", "Synthetic Fixture"),
        ("config", "user.email", "test@example.invalid"),
        ("add", "."),
        ("commit", "-qm", "baseline"),
    ):
        subprocess.run(["git", "-C", str(WIKI), *args], check=True)
    config = Path.home() / ".config/opencode/opencode.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        json.dumps(
            {
                "providers": {
                    "fake": {
                        "env": ["FAKE_API_KEY"],
                        "package": "@opencode/ai/providers/openai-compatible",
                        "settings": {"baseURL": "http://127.0.0.1:4081/v1"},
                        "models": {"synthetic": {"limit": {"context": 128000, "output": 4096}}},
                    }
                }
            }
        )
    )
    model = ThreadingHTTPServer(("127.0.0.1", 4081), Model)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    with subprocess.Popen(
        ["karpathy-wiki-entrypoint", "serve", "--hostname", "127.0.0.1", "--port", "4096"], cwd=WIKI
    ) as backend:
        try:
            for _ in range(60):
                try:
                    request("/api/info")
                    break
                except (urllib.error.URLError, TimeoutError):
                    time.sleep(1)
            else:
                raise AssertionError("Backend startup timed out")
            parent = request(
                "/api/session",
                {
                    "agent": "wiki-ingest-orchestrator",
                    "model": {"providerID": "fake", "id": "synthetic"},
                    "location": {"directory": str(WIKI)},
                    "title": "Synthetic batch test",
                },
            )["data"]
            sid = parent["id"]
            request(f"/api/session/{sid}/prompt", {"text": "Run synthetic batch"})
            request(f"/api/experimental/session/{sid}/wait", {})
            info = request(f"/api/session/{sid}")["data"]
            assert info["outcome"] == "succeeded", info
            reports = list(Path("/knowledge/incoming/ingest-journal/batches").glob("*/state.json"))
            assert len(reports) == 1, reports
            state = json.loads(reports[0].read_text())
            assert state["status"] == "ingested", state
            assert state["commit"], state
            assert CALLS == ["wiki_ingest_batch", "read", "write"], CALLS
            assert revision in WIKI.joinpath("sources/webdav/notes.md").read_text()
            assert SOURCE.joinpath("notes.md").read_text() == text
            count = subprocess.check_output(
                ["git", "-C", str(WIKI), "rev-list", "--count", "HEAD"], text=True
            )
            assert count.strip() == "2", count
            print("batch-ingest-runtime: real tool, child permissions, commit and report OK")
        finally:
            backend.terminate()
            backend.wait(timeout=30)
            model.shutdown()


if __name__ == "__main__":
    main()
