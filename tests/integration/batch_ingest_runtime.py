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
RESULT = "Synthetic finding, source line 1.\nContradictions: none.\nLimits: fully read line 1."
CALLS: list[str] = []
ERRORS: list[str] = []
MODE = "normal"
TARGET = ""
DENIED_TOOL = "write"


def update_patch(target, old, new):
    return (
        "patch",
        {
            "patchText": (
                f"*** Begin Patch\n*** Update File: {target}\n@@\n-{old}\n+{new}\n*** End Patch"
            )
        },
    )


def worker_calls(batch):
    source = batch["sources"][0]["wiki_path"]
    relative = str(Path(source).relative_to(WIKI))
    subject_pages = [f"{folder}/matrix.md" for folder in ("entities", "concepts", "analyses")]
    if MODE == "denied":
        if DENIED_TOOL == "patch":
            # Existing targets get valid matching hunks; absent targets get Add.
            file = Path(TARGET) if Path(TARGET).is_absolute() else WIKI / TARGET
            if file.is_file():
                old = file.read_text().splitlines()[0]
                return [update_patch(TARGET, old, "Forbidden synthetic write.")]
            return [
                (
                    "patch",
                    {
                        "patchText": (
                            f"*** Begin Patch\n*** Add File: {TARGET}\n"
                            "+Forbidden synthetic write.\n*** End Patch"
                        )
                    },
                )
            ]
        if DENIED_TOOL == "edit":
            file = Path(TARGET) if Path(TARGET).is_absolute() else WIKI / TARGET
            # Seeded existing forbidden targets ensure permission, not ENOENT, is tested.
            return [
                (
                    "edit",
                    {
                        "path": TARGET,
                        "oldString": file.read_text().splitlines()[0],
                        "newString": "Forbidden synthetic write.",
                    },
                )
            ]
        return [("write", {"path": TARGET, "content": "Forbidden synthetic write.\n"})]
    if MODE == "mixed":
        return [
            (
                "patch",
                {
                    "patchText": (
                        f"*** Begin Patch\n*** Update File: {relative}\n@@\n"
                        "-# Edited synthetic finding  \n+# Should not be published\n"
                        "*** Update File: index.md\n@@\n"
                        " # Synthetic index.md\n+Forbidden worker index edit\n*** End Patch"
                    )
                },
            )
        ]
    if MODE == "patch":
        calls = []
        for target in [relative, *subject_pages, "overview.md"]:
            old = "# Edited synthetic finding  " if target == relative else f"# Synthetic {target}"
            new = "# Patched synthetic finding  " if target == relative else f"# Patched {target}"
            calls.append(update_patch(str(WIKI / target), old, new))
            calls.append(update_patch(target, new, new + "\n+Relative patch evidence."))
        # Real allowed multi-file patch with mixed absolute/relative targets.
        calls.append(
            (
                "patch",
                {
                    "patchText": (
                        f"*** Begin Patch\n*** Update File: {relative}\n@@\n"
                        " # Patched synthetic finding  \n+Combined patch evidence.\n"
                        "*** Update File: /knowledge/wiki/overview.md\n@@\n"
                        " # Patched overview.md\n+Combined patch evidence.\n*** End Patch"
                    )
                },
            )
        )
        return calls
    if MODE in ("separate", "read-error"):
        write = ("write", {"path": source, "content": "# Preserved live finding\n"})
        failure = (
            ("write", {"path": "index.md", "content": "Forbidden index write.\n"})
            if MODE == "separate"
            else ("read", {"path": "/knowledge/wiki/.env"})
        )
        return [write, failure]
    calls = [
        ("write", {"path": source, "content": "# Synthetic finding  \nSource line 1.\n\n\n"}),
        ("write", {"path": relative, "content": "# Synthetic finding  \nSource line 1.\n\n\n"}),
        (
            "edit",
            {
                "path": source,
                "oldString": "# Synthetic finding",
                "newString": "# Absolute edited finding",
            },
        ),
        (
            "edit",
            {
                "path": relative,
                "oldString": "# Absolute edited finding",
                "newString": "# Edited synthetic finding",
            },
        ),
    ]
    if MODE == "normal":
        # Probe an absent new target, then recover through a real native write.
        # The earlier failed read must not poison an otherwise valid worker.
        calls.insert(0, ("read", {"path": source}))
    for target in [*subject_pages, "overview.md"]:
        text = f"# Synthetic {target}\n"
        calls.extend(
            [
                ("write", {"path": target, "content": text}),
                ("write", {"path": str(WIKI / target), "content": text}),
                ("edit", {"path": target, "oldString": "# Synthetic", "newString": "# Edited"}),
                (
                    "edit",
                    {
                        "path": str(WIKI / target),
                        "oldString": "# Edited",
                        "newString": "# Synthetic",
                    },
                ),
            ]
        )
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
        worker = "Batchdaten:" in json.dumps(messages)
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
        names = list(tools)
        content = "Synthetic batch completed."
        call = None
        history = [message for message in messages if message.get("role") == "tool"]
        if not tools:
            # Title/summary requests are not tool-enabled worker turns.
            pass
        elif not worker and not history:
            name = next(name for name in names if name.endswith("wiki_ingest_batch"))
            args = {} if MODE == "normal" else {"source_path": str(SOURCE / "notes.md")}
            call = (name, args)
        elif not worker and len(history) == 1:
            # Details must remain readable through the orchestrator's real
            # permissions, without granting journal mutation or state access.
            name = next(name for name in names if name == "read")
            props = tools[name]["parameters"]["properties"]
            key = next(key for key in ("path", "filePath", "file_path") if key in props)
            reports = list(Path("/knowledge/incoming/ingest-journal/batches").glob("*/report.md"))
            assert reports, reports
            call = (name, {key: str(max(reports, key=lambda report: report.stat().st_mtime_ns))})
        elif not worker:
            details = json.dumps(history[-1])
            if MODE in ("normal", "patch"):
                assert "Contradictions: none" in details, details
                assert "Limits: fully read line 1" in details, details
            else:
                assert "worker_" in details, details
        elif batch and batch["sources"][0]["source_key"] == "unreadable.md":
            content = "SKIP: Synthetic unreadable source."
        elif worker and MODE == "refusal":
            content = "Keine Änderungen: vorgeschriebene Prüfung nicht verfügbar."
        elif worker and not history:
            if MODE != "standalone":
                assert "Code-zugelassener Ingest-Worker" in json.dumps(messages)
            name = "skill"
            props = tools[name]["parameters"]["properties"]
            key = next(key for key in ("id", "name") if key in props)
            call = (name, {key: "wiki-ingest"})
        elif worker and len(history) in (1, 2):
            # Exercise real worker read permission, not a plugin-owned file read.
            name = next(name for name in names if name == "read")
            props = tools[name]["parameters"]["properties"]
            key = next(key for key in ("path", "filePath", "file_path") if key in props)
            assert batch
            target = (
                "/knowledge/wiki/AGENTS.md"
                if len(history) == 1
                else batch["sources"][0]["source_path"]
            )
            call = (name, {key: target})
        elif worker:
            assert batch
            calls = worker_calls(batch)
            step = len(history) - 3
            if step < len(calls):
                call = calls[step]
                assert call[0] in tools, (MODE, call[0], names)
            else:
                content = RESULT
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
    global MODE, TARGET, DENIED_TOOL
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
    assert "Quellenstatus/Widerruf" in WIKI.joinpath("AGENTS.md").read_text()
    for name in ("index.md", "overview.md", "log.md"):
        WIKI.joinpath(name).write_text(f"# Synthetic {name}\n")
    for target in (".opencode/forbidden.md", "sources/webdav/other.md", "../forbidden.md"):
        file = WIKI / target
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("# Protected synthetic target\n")
    text = "Synthetic source line 1.\n"
    for name in ("notes.md", "unreadable.md", "next.md"):
        SOURCE.joinpath(name).write_text(text)
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
                    for name in ("notes.md", "unreadable.md", "missing.md", "next.md")
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
            assert state["status"] == "partial", state
            assert [source["status"] for source in state["sources"]] == [
                "ingested",
                "skipped",
                "skipped",
                "ingested",
            ], state
            assert sum(bool(source["commit"]) for source in state["sources"]) == 2, state
            for source in state["sources"]:
                if source["status"] != "ingested":
                    continue
                assert source["worker_errors"] == [], source
                native = request(f"/api/experimental/session/{source['worker_session']}/export")[
                    "data"
                ]
                failures = [
                    part
                    for msg in native["messages"]
                    if msg["type"] == "assistant"
                    for part in msg["content"]
                    if part["type"] == "tool" and part["state"]["status"] == "error"
                ]
                assert len(failures) == 1, failures
                assert failures[0]["name"] == "read", failures
                assert failures[0]["state"]["error"] == {
                    "type": "tool.execution",
                    "message": f"File not found: {source['wiki_path']}",
                }, failures
            expected = ["skill", "read", "read", "read", "write", "write", "edit", "edit"] + [
                "write",
                "write",
                "edit",
                "edit",
            ] * 4
            assert CALLS == ["wiki_ingest_batch"] + expected * 2 + ["read"], CALLS
            assert revision in WIKI.joinpath("sources/webdav/notes.md").read_text()
            assert SOURCE.joinpath("notes.md").read_text() == text
            count = subprocess.check_output(
                ["git", "-C", str(WIKI), "rev-list", "--count", "HEAD"], text=True
            )
            assert count.strip() == "4", count
            assert WIKI.joinpath("sources/webdav/next.md").is_file()
            assert not WIKI.joinpath("sources/webdav/unreadable.md").exists()
            source_page = WIKI / "sources/webdav/notes.md"
            before = source_page.read_bytes()
            assert b"# Edited synthetic finding" in before
            MODE = "standalone"
            bare = request(
                "/api/session",
                {
                    "agent": "wiki-ingest",
                    "model": {"providerID": "fake", "id": "synthetic"},
                    "location": {"directory": str(WIKI)},
                },
            )["data"]["id"]
            assignment = {
                "batch_id": "synthetic-forged",
                "sources": [
                    {
                        "source_key": "notes.md",
                        "source_path": str(SOURCE / "notes.md"),
                        "wiki_path": str(source_page),
                    }
                ],
            }
            request(
                f"/api/session/{bare}/prompt", {"text": f"Batchdaten:\n{json.dumps(assignment)}"}
            )
            request(f"/api/experimental/session/{bare}/wait", {})
            assert source_page.read_bytes() == before
            exported = json.dumps(request(f"/api/experimental/session/{bare}/export"))
            assert "permission.rejected" in exported, exported

            changed = "Synthetic changed source fact.\n"
            SOURCE.joinpath("notes.md").write_text(changed)
            manifest_path = SOURCE / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            new_revision = hashlib.sha256(changed.encode()).hexdigest()
            note = next(item for item in manifest["items"] if item["source_key"] == "notes.md")
            note["source_revision"] = note["frontmatter"]["source_revision"] = new_revision
            manifest_path.write_text(json.dumps(manifest))
            probes: list[tuple[str, str | None, str, str]] = [
                ("refusal", "worker_no_source_write", "", ""),
                ("mixed", "worker_permission_denied", "", ""),
            ]
            forbidden = [
                "AGENTS.md",
                "index.md",
                "/knowledge/wiki/log.md",
                ".git/config",
                ".opencode/forbidden.md",
                "sources/webdav/other.md",
                "/knowledge/sources/webdav/notes.md",
                "../forbidden.md",
            ]
            probes.extend(
                ("denied", "worker_permission_denied", tool, target)
                for tool in ("write", "edit", "patch")
                for target in forbidden
            )
            probes.extend(
                [
                    ("separate", "worker_permission_denied", "", ""),
                    ("read-error", "worker_permission_denied", "", ""),
                    ("patch", None, "", ""),
                ]
            )
            protected = {
                target: (Path(target) if Path(target).is_absolute() else WIKI / target).read_bytes()
                for target in forbidden
            }
            for mode, reason, denied_tool, target in probes:
                MODE = mode
                TARGET = target
                DENIED_TOOL = denied_tool
                probe = request(
                    "/api/session",
                    {
                        "agent": "wiki-ingest-orchestrator",
                        "model": {
                            "providerID": "fake",
                            "id": "gpt-5-synthetic"
                            if mode in ("mixed", "patch") or denied_tool == "patch"
                            else "synthetic",
                        },
                        "location": {"directory": str(WIKI)},
                    },
                )["data"]["id"]
                request(f"/api/session/{probe}/prompt", {"text": "Run filtered synthetic batch"})
                request(f"/api/experimental/session/{probe}/wait", {})
                reports = list(
                    Path("/knowledge/incoming/ingest-journal/batches").glob("*/state.json")
                )
                state = json.loads(
                    max(reports, key=lambda report: report.stat().st_mtime_ns).read_text()
                )
                if reason:
                    live_write = mode in ("separate", "read-error")
                    assert state["status"] == ("blocked" if live_write else "partial"), state
                    assert reason in state["sources"][0]["blocker"], state
                    assert state["sources"][0]["source_written"] is live_write, state
                    assert new_revision not in source_page.read_text()
                    if live_write:
                        assert source_page.read_text() == "# Preserved live finding\n"
                        native = request(
                            f"/api/experimental/session/{state['sources'][0]['worker_session']}/export"
                        )["data"]
                        tools = [
                            part
                            for msg in native["messages"]
                            if msg["type"] == "assistant"
                            for part in msg["content"]
                            if part["type"] == "tool"
                        ]
                        assert tools[-2]["state"]["status"] == "completed", tools
                        assert tools[-1]["state"]["error"]["type"] == "permission.rejected", tools
                        report = (
                            max(reports, key=lambda file: file.stat().st_mtime_ns)
                            .with_name("report.md")
                            .read_text()
                        )
                        assert "Quellschreibvorgang: belegt" in report
                        # Explicit test-harness recovery after verifying preserved live edits.
                        source_page.write_bytes(before)
                    else:
                        assert source_page.read_bytes() == before
                    for target, original in protected.items():
                        file = Path(target) if Path(target).is_absolute() else WIKI / target
                        assert file.read_bytes() == original, (mode, denied_tool, target)
                else:
                    assert state["status"] == "done", state
                    assert new_revision in source_page.read_text()
                    assert "# Patched synthetic finding" in source_page.read_text()
            count = subprocess.check_output(
                ["git", "-C", str(WIKI), "rev-list", "--count", "HEAD"], text=True
            )
            assert count.strip() == "5", count
            assert not ERRORS, ERRORS
            print(
                "batch-ingest-runtime: real instructions, write/edit/patch, admission, "
                "forbidden mixed patch, outdated refusal, sequential commits and report OK"
            )
        finally:
            backend.terminate()
            backend.wait(timeout=30)
            model.shutdown()


if __name__ == "__main__":
    main()
