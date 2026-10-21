"""Pinned-runtime proof; disposable storage, synthetic HTTP model, no credentials."""

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
JOURNAL = Path("/knowledge/incoming/ingest-journal")
MODE = "normal"
TARGET = ""
MUTATION = "write"
ACTION: dict = {}
ERRORS: list[str] = []
RESULT = "Synthetic finding, source line 1. Contradictions: none. Limits: fully read line 1."


def patch(target, old, new):
    return (
        "patch",
        {
            "patchText": (
                f"*** Begin Patch\n*** Update File: {target}\n@@\n-{old}\n+{new}\n*** End Patch"
            )
        },
    )


def worker_calls(batch):
    root = Path(batch.get("wiki_root", str(WIKI)))
    source = batch["sources"][0]["wiki_path"]
    relative = str(Path(source).relative_to(root))
    if MODE == "denied":
        file = Path(TARGET) if Path(TARGET).is_absolute() else root / TARGET
        if MUTATION == "edit":
            return [
                (
                    "edit",
                    {
                        "path": TARGET,
                        "oldString": file.read_text().splitlines()[0],
                        "newString": "Forbidden",
                    },
                )
            ]
        if MUTATION == "patch":
            if file.is_file():
                return [patch(TARGET, file.read_text().splitlines()[0], "Forbidden")]
            return [
                (
                    "patch",
                    {
                        "patchText": (
                            f"*** Begin Patch\n*** Add File: {TARGET}\n+Forbidden\n*** End Patch"
                        )
                    },
                )
            ]
        return [("write", {"path": TARGET, "content": "Forbidden\n"})]
    if MODE in ("patch", "mixed", "syntax"):
        calls = []
        if MODE == "mixed":
            calls.append(
                (
                    "patch",
                    {
                        "patchText": (
                            f"*** Begin Patch\n*** Update File: {relative}\n@@\n"
                            "-# Finding\n+# Incomplete\n*** Update File: index.md\n@@\n"
                            "-# Synthetic index.md\n+# Forbidden\n*** End Patch"
                        )
                    },
                )
            )
        if MODE == "syntax":
            calls.append(patch(relative, "Nonexistent stale context", "# Broken"))
            calls.append(("read", {"path": relative}))
        for target in [
            relative,
            "overview.md",
            "entities/matrix.md",
            "concepts/matrix.md",
            "analyses/matrix.md",
        ]:
            old = (root / target).read_text().splitlines()[0]
            calls.append(patch(str(root / target), old, "# Patched finding"))
            calls.append(patch(target, "# Patched finding", "# Finding"))
        return calls
    calls = [
        ("read", {"path": source}),
        ("write", {"path": source, "content": "# Finding\nSource line 1.\n"}),
    ]
    if MODE == "partial":
        return calls + [
            ("write", {"path": "/knowledge/wiki/overview.md", "content": "Forbidden live write\n"})
        ]
    calls += [
        ("edit", {"path": relative, "oldString": "# Finding", "newString": "# Edited finding"})
    ]
    for target in ["overview.md", "entities/matrix.md", "concepts/matrix.md", "analyses/matrix.md"]:
        calls += [
            ("write", {"path": str(root / target), "content": "# Finding\n"}),
            ("edit", {"path": target, "oldString": "# Finding", "newString": "# Subject finding"}),
        ]
    return calls


class Model(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):
        try:
            self.respond()
        except Exception as error:
            ERRORS.append(repr(error))
            self.send_error(500, "Synthetic model assertion failed")

    def respond(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        messages = body.get("messages", [])
        batch = None
        for message in messages:
            value = message.get("content", "")
            texts = (
                [value]
                if isinstance(value, str)
                else [part.get("text", "") for part in value or []]
            )
            for text in texts:
                if "Batchdaten:\n" in text:
                    batch = json.loads(text.split("Batchdaten:\n", 1)[1])
        tools = {tool["function"]["name"]: tool["function"] for tool in body.get("tools", [])}
        history = [message for message in messages if message.get("role") == "tool"]
        if not batch:
            last_user = max(
                (i for i, message in enumerate(messages) if message.get("role") == "user"),
                default=-1,
            )
            history = [
                message for message in messages[last_user + 1 :] if message.get("role") == "tool"
            ]
        call = None
        content = RESULT
        if not tools:
            pass
        elif not batch and not history:
            call = ("wiki_ingest_batch", ACTION)
        elif not batch and len(history) == 1:
            reports = list(JOURNAL.glob("batches/*/report.md"))
            call = ("read", {"path": str(max(reports, key=lambda file: file.stat().st_mtime_ns))})
        elif not batch:
            pass
        elif not history:
            call = ("skill", {"id": "wiki-ingest"})
        elif len(history) == 1:
            call = ("read", {"path": "/knowledge/wiki/AGENTS.md"})
        elif len(history) == 2:
            call = ("read", {"path": batch["sources"][0]["source_path"]})
        else:
            calls = worker_calls(batch)
            step = len(history) - 3
            if step < len(calls):
                call = calls[step]
        if call:
            name, args = call
            assert name in tools, (name, list(tools))
            # Read input names are runtime-defined, not assumed from V1.
            if name in ("read", "skill"):
                props = tools[name]["parameters"]["properties"]
                candidates = ("path", "filePath", "file_path") if name == "read" else ("id", "name")
                key = next(key for key in candidates if key in props)
                args = {key: next(iter(args.values()))}
            delta = {
                "role": "assistant",
                "tool_calls": [
                    {
                        "index": 0,
                        "id": f"call_{len(history)}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            }
            finish = "tool_calls"
        else:
            delta, finish = {"role": "assistant", "content": content}, "stop"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        base = {
            "id": "chatcmpl-fixture",
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


def run(sid=None, approval=False):
    if sid is None:
        sid = request(
            "/api/session",
            {
                "agent": "wiki-ingest-orchestrator",
                "model": {
                    "providerID": "fake",
                    "id": "gpt-5-synthetic"
                    if MODE in ("patch", "mixed", "syntax") or MUTATION == "patch"
                    else "synthetic",
                },
                "location": {"directory": str(WIKI)},
            },
        )["data"]["id"]
    request(f"/api/session/{sid}/prompt", {"text": "Run synthetic action"})
    if approval:
        for _ in range(100):
            pending = request(f"/api/session/{sid}/permission")["data"]
            if pending:
                assert len(pending) == 1, pending
                item = pending[0]
                assert item["action"] == "ingest_decision", item
                request(f"/api/session/{sid}/permission/{item['id']}/reply", {"decision": "once"})
                break
            time.sleep(0.1)
        else:
            raise AssertionError("Bound decision did not ask for approval")
    request(f"/api/experimental/session/{sid}/wait", {})
    states = list(JOURNAL.glob("batches/*/state.json"))
    state = json.loads(max(states, key=lambda file: file.stat().st_mtime_ns).read_text())
    assert request(f"/api/session/{sid}")["data"]["outcome"] == "succeeded"
    return sid, state


def main():
    global MODE, TARGET, MUTATION, ACTION
    subprocess.run(
        ["sh", "/etc/opencode/init.sh"],
        env={
            **os.environ,
            "WIKI_NAME": "Synthetic Wiki",
            "WIKI_PUBLIC_URL": "https://wiki.example.invalid",
            "GIT_AUTHOR_NAME": "Synthetic Fixture",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "PUID": str(os.getuid()),
            "PGID": str(os.getgid()),
        },
        check=True,
    )
    for name in ("index.md", "overview.md", "log.md"):
        (WIKI / name).write_text(f"# Synthetic {name}\n")
    for name in (
        "entities/matrix.md",
        "concepts/matrix.md",
        "analyses/matrix.md",
        "sources/webdav/other.md",
        ".opencode/forbidden.md",
    ):
        file = WIKI / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("# Protected synthetic page\n")
    (WIKI / "sources/webdav/other.md").write_text(
        "---\nsource: webdav\nsource_path: other.md\nsource_revision: "
        + "a" * 64
        + "\n---\n# Protected synthetic page\n"
    )
    for args in [
        ("init", "-q"),
        ("config", "user.name", "Synthetic"),
        ("config", "user.email", "test@example.invalid"),
        ("add", "."),
        ("commit", "-qm", "baseline"),
    ]:
        subprocess.run(["git", "-C", str(WIKI), *args], check=True)
    manifest = {
        "contract": "karpathy-wiki-provider-manifest",
        "version": 1,
        "source": "webdav",
        "wiki_root": "webdav",
        "generated_at": 1760000000,
        "items": [],
        "revoked": [],
        "errors": [],
    }

    def change_source():
        text = f"Synthetic source line 1, generation {time.monotonic_ns()}.\n"
        (SOURCE / "notes.md").write_text(text)
        revision = hashlib.sha256(text.encode()).hexdigest()
        manifest["items"] = [
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
        ]
        (SOURCE / "manifest.json").write_text(json.dumps(manifest))

    change_source()
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
                        "models": {
                            name: {"limit": {"context": 128000, "output": 4096}}
                            for name in ("synthetic", "gpt-5-synthetic")
                        },
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
            ACTION = {"source_path": str(SOURCE / "notes.md")}
            _, state = run()
            assert state["status"] == "done", state
            source = state["sources"][0]
            native = request(f"/api/experimental/session/{source['worker_session']}/export")["data"]
            assert "/ingest-journal/drafts/" in native["info"]["location"]["directory"], native[
                "info"
            ]
            assert source["source_written"] and source["worker_errors"] == [], source
            assert source["attempts"][0]["recovered_errors"], source
            for mode in ("patch", "mixed", "syntax"):
                change_source()
                MODE = mode
                _, state = run()
                if state["status"] != "done":
                    worker = state["sources"][0].get("worker_session")
                    native = request(f"/api/experimental/session/{worker}/export")["data"]
                    failures = [
                        part
                        for msg in native["messages"]
                        if msg["type"] == "assistant"
                        for part in msg["content"]
                        if part["type"] == "tool" and part["state"]["status"] == "error"
                    ]
                    raise AssertionError((mode, state, failures))
            # An allowed source write followed by a forbidden live write stays
            # private. A bound answer in the same chat then completes it.
            change_source()
            MODE = "partial"
            sid, state = run()
            assert state["status"] == "paused", state
            before = (WIKI / "sources/webdav/notes.md").read_bytes()
            question = next(item for item in state["questions"] if item["source_index"] == 0)
            ACTION = {
                "batch_id": state["batch_id"],
                "decision_id": question["id"],
                "answer": "retry",
            }
            MODE = "normal"
            _, state = run(sid, approval=True)
            assert state["status"] == "done", state
            assert (WIKI / "sources/webdav/notes.md").read_bytes() != before
            forbidden = [
                "AGENTS.md",
                "log.md",
                "/knowledge/wiki/.git/config",
                "/knowledge/wiki/.opencode/forbidden.md",
                "sources/webdav/other.md",
                "/knowledge/wiki/overview.md",
                "/knowledge/sources/webdav/notes.md",
            ]
            for mutation in ("write", "edit", "patch"):
                for target in forbidden:
                    change_source()
                    MODE, MUTATION, TARGET = "denied", mutation, target
                    ACTION = {"source_path": str(SOURCE / "notes.md")}
                    protected = {
                        name: (Path(name) if Path(name).is_absolute() else WIKI / name).read_bytes()
                        for name in forbidden
                    }
                    sid, state = run()
                    assert state["status"] == "paused", (mutation, target, state)
                    assert not state["sources"][0]["commit"], state
                    for name, original in protected.items():
                        assert (
                            Path(name) if Path(name).is_absolute() else WIKI / name
                        ).read_bytes() == original, (mutation, target)
                    question = state["questions"][0]
                    ACTION = {
                        "batch_id": state["batch_id"],
                        "decision_id": question["id"],
                        "answer": "cancel",
                    }
                    _, state = run(sid, approval=True)
                    assert state["status"] == "cancelled", state
            assert not subprocess.check_output(
                ["git", "-C", str(WIKI), "status", "--porcelain"], text=True
            ).strip()
            assert not ERRORS, ERRORS
            print(
                "batch-ingest-runtime: draft Location, native evidence, corrected patches, "
                "read/write/edit permissions and bound approval/resumption OK"
            )
        finally:
            backend.terminate()
            backend.wait(timeout=30)
            model.shutdown()


if __name__ == "__main__":
    main()
