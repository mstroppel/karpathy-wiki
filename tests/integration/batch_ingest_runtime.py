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
SUMMARY_REQUESTS: list[str] = []
CONTEXT_FAILURES: list[str] = []
RESULT = (
    "Synthetic finding, source line 1. Contradictions: none. Limits: fully read line 1.\n\n"
    '```ingest-summary\n{"title":"Synthetic finding",'
    '"summary":["Added a supported finding.","No contradiction found in line 1."]}\n```'
)


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
    if MODE in ("relative-read", "add-stale"):
        create = f"*** Add File: {relative}\n+# Finding\n+Source line 1.\n"
        update = "*** Update File: overview.md\n@@\n-{}\n+# Finding\n"
        current = (root / "overview.md").read_text().splitlines()[0]
        if MODE == "relative-read":
            return [
                ("read", {"path": relative}),
                ("patch", {"patchText": f"*** Begin Patch\n{create}*** End Patch"}),
            ]
        return [
            (
                "patch",
                {
                    "patchText": f"*** Begin Patch\n{create}"
                    + update.format("Nonexistent stale context")
                    + "*** End Patch"
                },
            ),
            ("read", {"path": "overview.md"}),
            (
                "patch",
                {
                    "patchText": f"*** Begin Patch\n{create}"
                    + update.format(current)
                    + "*** End Patch"
                },
            ),
        ]
    if MODE == "denied":
        file = Path(TARGET) if Path(TARGET).is_absolute() else root / TARGET
        if MUTATION == "edit":
            return [
                (
                    "edit",
                    {
                        "path": TARGET,
                        "oldString": file.read_text().splitlines()[0]
                        if file.is_file()
                        else "# Protected synthetic page",
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
        calls: list[tuple[str, dict]] = []
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
        ("read", {"path": str(root)}),
        ("read", {"path": str(WIKI)}),
        ("read", {"path": source}),
        ("write", {"path": source, "content": "# Finding\nSource line 1.\n"}),
    ]
    if MODE == "normal":
        # Context-limit scenarios deliberately depend on the original call count.
        calls.insert(2, ("read", {"path": str(WIKI / "sources/webdav/other.md")}))
    if MODE == "pagination":
        calls.insert(
            0, ("read", {"path": batch["sources"][0]["source_path"], "offset": 2, "limit": 2})
        )
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
        summarizing = any("<template>" in str(message.get("content")) for message in messages)
        if summarizing:
            SUMMARY_REQUESTS.append(MODE)
        limited = batch and batch["sources"][0]["source_key"] == "notes.md"
        if MODE == "context-provider" and limited and len(history) >= 7:
            assert batch is not None
            CONTEXT_FAILURES.append(batch["wiki_root"])
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "error": {
                            "code": "context_length_exceeded",
                            "message": "maximum context length exceeded",
                        }
                    }
                ).encode()
            )
            return
        if not batch:
            last_user = max(
                (i for i, message in enumerate(messages) if message.get("role") == "user"),
                default=-1,
            )
            history = [
                message for message in messages[last_user + 1 :] if message.get("role") == "tool"
            ]
        call = None
        content = (
            "## Objective\n- Synthetic diagnosis\n\n## Important Details\n- No edits\n\n"
            "## Work State\n### Completed\n- Diagnosis\n### Active\n- (none)\n"
            "### Blocked\n- (none)\n\n## Next Move\n1. (none)\n\n## Relevant Files\n- (none)"
            if summarizing
            else RESULT
        )
        if MODE == "ordinary-compaction" or summarizing or not tools:
            pass
        elif MODE == "diagnose":
            assert not batch
            assert "wiki_ingest_batch" not in tools
            assert "wiki_ingest_diagnostics" in tools
            if not history:
                call = ("wiki_ingest_diagnostics", {})
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
            if MODE == "pagination":
                call[1]["limit"] = 1
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
                value = args.pop("path" if name == "read" else "id")
                args = {**args, key: value}
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
                "usage": {
                    "prompt_tokens": 127000
                    if MODE == "context-auto" and limited and len(history) >= 6
                    else 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
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


def run(sid=None):
    if sid is None:
        sid = request(
            "/api/session",
            {
                "agent": "wiki-ingest-orchestrator",
                "model": {
                    "providerID": "fake",
                    "id": "gpt-5-synthetic"
                    if MODE in ("patch", "mixed", "syntax", "relative-read", "add-stale")
                    or MUTATION == "patch"
                    else "synthetic",
                },
                "location": {"directory": str(WIKI)},
            },
        )["data"]["id"]
    request(f"/api/session/{sid}/prompt", {"text": "Run synthetic action"})
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
    manifest: dict = {
        "contract": "karpathy-wiki-provider-manifest",
        "version": 1,
        "source": "webdav",
        "wiki_root": "webdav",
        "generated_at": 1760000000,
        "items": [],
        "revoked": [],
        "errors": [],
    }

    def change_source(text=None, name="notes.md"):
        if text is None:
            text = f"Synthetic source line 1, generation {time.monotonic_ns()}.\n"
        (SOURCE / name).write_text(text)
        revision = hashlib.sha256(text.encode()).hexdigest()
        manifest["items"] = [
            {
                "source_key": name,
                "source_path": name,
                "wiki_path": f"webdav/{name}",
                "source_revision": revision,
                "frontmatter": {
                    "source": "webdav",
                    "source_path": name,
                    "source_revision": revision,
                },
                "claim": {"source": "webdav", "source_path": name},
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
            for mode in ("relative-read", "add-stale"):
                MODE = mode
                change_source(name=f"{mode}.md")
                ACTION = {"source_path": str(SOURCE / f"{mode}.md")}
                _, recovered = run()
                assert recovered["status"] == "done", (recovered, ERRORS)
                item = recovered["sources"][0]
                assert item["status"] == "ingested" and item["commit"], item
                subprocess.run(
                    ["git", "-C", str(WIKI), "cat-file", "-e", f"{item['commit']}^{{commit}}"],
                    check=True,
                )
                assert item["worker_errors"] == [], item
                assert item["attempts"][0]["recovered_errors"], item
            MODE = "normal"
            change_source()
            ACTION = {"source_path": str(SOURCE / "notes.md")}
            _, state = run()
            assert state["status"] == "done", state
            source = state["sources"][0]
            assert source["brief"] == {
                "title": "Synthetic finding",
                "summary": ["Added a supported finding.", "No contradiction found in line 1."],
            }, source
            overview = (JOURNAL / "batches" / state["batch_id"] / "report.md").read_text()
            assert source["source_label"] == "notes.md", source
            assert "## Kurzüberblick" in overview and "Quelle: notes&#46;md" in overview
            native = request(f"/api/experimental/session/{source['worker_session']}/export")["data"]
            assert "/ingest-journal/drafts/" in native["info"]["location"]["directory"], native[
                "info"
            ]
            assert source["source_written"] and source["worker_errors"] == [], source
            context_reads = [
                part
                for message in native["messages"]
                for part in message.get("content", [])
                if part.get("type") == "tool"
                and part.get("name") == "read"
                and part.get("state", {}).get("input", {}).get("path")
                == str(WIKI / "sources/webdav/other.md")
            ]
            assert context_reads and all(
                part["state"]["status"] == "completed" for part in context_reads
            ), context_reads
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
            # private. A new explicit request starts a fresh interpretation.
            change_source()
            MODE = "partial"
            sid, state = run()
            assert state["status"] == "partial", state
            before = (WIKI / "sources/webdav/notes.md").read_bytes()
            # A fresh build chat can inspect an existing batch, but cannot
            # resume it or mutate its state by loading an orchestrator skill.
            state_file = JOURNAL / "batches" / state["batch_id"] / "state.json"
            state_before = state_file.read_bytes()
            MODE = "diagnose"
            diagnostic_sid = request(
                "/api/session",
                {
                    "agent": "build",
                    "model": {"providerID": "fake", "id": "synthetic"},
                    "location": {"directory": str(WIKI)},
                },
            )["data"]["id"]
            run(diagnostic_sid)
            assert state_file.read_bytes() == state_before
            native = request(f"/api/experimental/session/{diagnostic_sid}/export")["data"]
            observations = [
                json.loads(part["state"]["content"][0]["text"])
                for msg in native["messages"]
                if msg["type"] == "assistant"
                for part in msg["content"]
                if part["type"] == "tool" and part["name"] == "wiki_ingest_diagnostics"
            ]
            assert len(observations) == 1, observations
            assert observations[0]["status"] == "idle", observations
            failed_worker = state["sources"][0]["worker_session"]
            MODE = "normal"
            _, state = run(sid)
            assert state["status"] == "done", state
            assert state["sources"][0]["worker_session"] != failed_worker
            assert (WIKI / "sources/webdav/notes.md").read_bytes() != before
            # The pinned native read tool shortens long lines, even at EOF.
            # Successful writes/model prose must not turn that into full coverage.
            change_source("<html>" + "x" * 5000 + "UNREAD_SUFFIX</html>\n")
            ACTION = {"source_path": str(SOURCE / "notes.md")}
            before = (WIKI / "sources/webdav/notes.md").read_bytes()
            sid, state = run()
            source = state["sources"][0]
            assert (
                state["status"] == "partial" and source["blocker"] == "worker_source_truncated"
            ), state
            assert len(source["attempts"]) == 1 and not source["commit"], source
            native = request(f"/api/experimental/session/{source['worker_session']}/export")["data"]
            reads = [
                part
                for msg in native["messages"]
                if msg["type"] == "assistant"
                for part in msg["content"]
                if part["type"] == "tool"
                and part["name"] == "read"
                and part["state"]["input"].get("path") == str(SOURCE / "notes.md")
            ]
            output = json.dumps(reads)
            assert "(line truncated to 2000 chars)" in output and "UNREAD_SUFFIX" not in output, (
                reads
            )
            assert (WIKI / "sources/webdav/notes.md").read_bytes() == before
            change_source("Synthetic line 1\nSynthetic line 2\nSynthetic line 3\n")
            MODE = "pagination"
            _, state = run()
            assert state["status"] == "done", state
            # Native automatic threshold and actual provider overflow both stop
            # after an allowed write, publish nothing, and admit independent work.
            for mode in ("context-auto", "context-provider"):
                change_source()
                before = (WIKI / "sources/webdav/notes.md").read_bytes()
                independent = "Synthetic independent source\n"
                (SOURCE / "independent.md").write_text(independent)
                revision = hashlib.sha256(independent.encode()).hexdigest()
                manifest["items"].append(
                    {
                        "source_key": "independent.md",
                        "source_path": "independent.md",
                        "wiki_path": "webdav/independent.md",
                        "source_revision": revision,
                        "frontmatter": {
                            "source": "webdav",
                            "source_path": "independent.md",
                            "source_revision": revision,
                        },
                        "claim": {"source": "webdav", "source_path": "independent.md"},
                    }
                )
                # Make the independent page outdated for the second round too.
                independent += mode + "\n"
                (SOURCE / "independent.md").write_text(independent)
                revision = hashlib.sha256(independent.encode()).hexdigest()
                manifest["items"][-1]["source_revision"] = revision
                manifest["items"][-1]["frontmatter"]["source_revision"] = revision
                (SOURCE / "manifest.json").write_text(json.dumps(manifest))
                MODE, ACTION = mode, {}
                _, state = run()
                limited = next(
                    source for source in state["sources"] if source["source_key"] == "notes.md"
                )
                assert state["status"] == "partial", state
                assert limited["blocker"] == "source_context_limit", state
                assert limited["source_written"] and not limited["commit"], limited
                assert len(limited["attempts"]) == 1
                if mode == "context-provider":
                    assert len(CONTEXT_FAILURES) == 1, CONTEXT_FAILURES
                assert next(
                    source
                    for source in state["sources"]
                    if source["source_key"] == "independent.md"
                )["commit"]
                assert (WIKI / "sources/webdav/notes.md").read_bytes() == before
                native = request(f"/api/experimental/session/{limited['worker_session']}/export")[
                    "data"
                ]
                assert any(
                    message["type"] == "idle"
                    and message.get("error", {}).get("message") == "ingest_context_limit"
                    for message in native["messages"]
                ), native
                assert not any(message["type"] == "compaction" for message in native["messages"])
                assert not SUMMARY_REQUESTS, SUMMARY_REQUESTS
            # The same hook must leave ordinary chat compaction functional.
            MODE = "ordinary-compaction"
            request(f"/api/session/{diagnostic_sid}/compact", {})
            request(f"/api/experimental/session/{diagnostic_sid}/wait", {})
            native = request(f"/api/experimental/session/{diagnostic_sid}/export")["data"]
            assert any(
                message["type"] == "compaction" and message["status"] == "completed"
                for message in native["messages"]
            ), native
            ACTION = {"source_path": str(SOURCE / "notes.md")}
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
                    assert state["status"] in ("partial", "blocked"), (mutation, target, state)
                    assert not state["sources"][0]["commit"], state
                    for name, original in protected.items():
                        assert (
                            Path(name) if Path(name).is_absolute() else WIKI / name
                        ).read_bytes() == original, (mutation, target)
            assert not subprocess.check_output(
                ["git", "-C", str(WIKI), "status", "--porcelain"], text=True
            ).strip()
            assert not ERRORS, ERRORS
            print(
                "batch-ingest-runtime: draft Location, native evidence, corrected patches, "
                "root directory reads, context stops, independent continuation and permissions OK"
            )
        finally:
            backend.terminate()
            backend.wait(timeout=30)
            model.shutdown()


if __name__ == "__main__":
    main()
