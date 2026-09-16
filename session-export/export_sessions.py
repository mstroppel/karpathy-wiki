#!/usr/bin/env python3

import argparse
import base64
import html
import json
import os
import re
import signal
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo


API_URL = os.environ.get("OPENCODE_URL", "http://opencode:4096").rstrip("/")
DIRECTORY = os.environ.get("OPENCODE_DIRECTORY", "/knowledge/wiki")
PUBLIC_URL = os.environ.get("OPENCODE_PUBLIC_URL", "").strip()
EXPORT_ROOT = Path(os.environ.get("EXPORT_ROOT", "/exports"))
RCLONE_REMOTE = os.environ.get("RCLONE_REMOTE", "nextcloud").strip().rstrip(":")
REMOTE_PATH = os.environ.get("SESSION_EXPORT_REMOTE_PATH", "OpenCode Sessions").strip("/")
INTERVAL = os.environ.get("SESSION_EXPORT_INTERVAL", "15m")
TZ_NAME = os.environ.get("TZ", "UTC")
TZ = timezone.utc if TZ_NAME in ("UTC", "Etc/UTC") else ZoneInfo(TZ_NAME)
STATE_PATH = EXPORT_ROOT / ".export-state.json"
EXPORT_FORMAT_VERSION = 3
_markdown = None


class ShutdownRequested(BaseException):
    pass


def request_shutdown(_signum, _frame):
    raise ShutdownRequested


def run_command(command, *, capture_output=False, check=True):
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        text=capture_output,
    )
    try:
        stdout, stderr = process.communicate()
    except BaseException:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        raise

    result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, command, stdout, stderr)
    return result


def interval_seconds(value):
    match = re.fullmatch(r"\s*(\d+)\s*([smhd]?)\s*", value)
    if not match:
        raise ValueError(f"invalid SESSION_EXPORT_INTERVAL: {value}")
    units = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}
    return int(match.group(1)) * units[match.group(2)]


def validate_public_url(value):
    value = value.strip().rstrip("/")
    if not value:
        raise ValueError("OPENCODE_PUBLIC_URL is required")

    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "OPENCODE_PUBLIC_URL must be an HTTP(S) URL without credentials, query, or fragment"
        )
    return value


def api_get(path, query=None):
    url = f"{API_URL}{path}"
    if query:
        url += "?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"OpenCode API {url} returned HTTP {error.code}") from error


def safe_title(value):
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', " - ", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = (value or "Untitled Session")[:120].rstrip(" .")
    return value.encode("utf-8")[:180].decode("utf-8", errors="ignore").rstrip(" .")


def format_time(milliseconds):
    if not milliseconds:
        return ""
    return datetime.fromtimestamp(milliseconds / 1000, TZ).strftime("%Y-%m-%d %H:%M:%S %Z")


def session_url(session_id, public_url=None):
    public_url = validate_public_url(PUBLIC_URL if public_url is None else public_url)
    server = base64.urlsafe_b64encode(public_url.encode("utf-8")).decode("ascii").rstrip("=")
    quoted_id = urllib.parse.quote(session_id, safe="")
    return f"{public_url}/server/{server}/session/{quoted_id}"


def markdown_renderer():
    global _markdown
    if _markdown is None:
        import mistune  # type: ignore[import-not-found]

        _markdown = mistune.create_markdown(
            escape=True,
            plugins=["strikethrough", "table", "task_lists", "url"],
        )
    return _markdown


def render_message_parts(parts):
    text = "\n\n".join(part.get("text", "") for part in parts if part.get("type") == "text").strip()
    content = str(markdown_renderer()(text)) if text else ""
    attachments = []
    for part in parts:
        if part.get("type") != "file":
            continue
        label = part.get("filename") or part.get("mime") or "File"
        attachments.append(f'<div class="attachment">Attachment: {html.escape(str(label))}</div>')
    return content + "".join(attachments)


def render_html(session, messages):
    title = session.get("title") or "Untitled Session"
    created = session.get("time", {}).get("created")
    updated = session.get("time", {}).get("updated")
    public_session_url = session_url(session.get("id", ""))
    blocks = []
    for message in messages:
        info = message.get("info", {})
        role = info.get("role", "unknown")
        if role not in ("user", "assistant"):
            continue
        content = render_message_parts(message.get("parts", []))
        if not content:
            continue
        label = "User" if role == "user" else "OpenCode"
        blocks.append(
            f'<section class="message {html.escape(role)}"><header>{html.escape(label)}</header>'
            f'<div class="content">{content}</div></section>'
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src data:; style-src 'unsafe-inline'">
<title>{html.escape(title)}</title>
<style>
@page {{ size: A4; margin: 18mm 16mm 20mm; @bottom-right {{ content: counter(page) " / " counter(pages); color: #667085; font-size: 8pt; }} }}
* {{ box-sizing: border-box; }}
body {{ color: #17202a; font: 10pt/1.5 "DejaVu Sans", sans-serif; margin: 0; }}
h1 {{ color: #102a43; font-size: 22pt; line-height: 1.2; margin: 0 0 4mm; }}
.meta {{ border-bottom: 1px solid #ccd5df; color: #52606d; font-size: 8.5pt; margin-bottom: 8mm; padding-bottom: 4mm; }}
.session-link {{ font-size: 9pt; margin: 0 0 3mm; overflow-wrap: anywhere; }}
.session-link a {{ color: #126e82; text-decoration: none; }}
.message {{ break-inside: avoid; border: 1px solid #d9e2ec; border-radius: 3mm; margin: 0 0 5mm; overflow: hidden; }}
.message header {{ background: #eef2f6; color: #243b53; font-weight: bold; padding: 2mm 3mm; }}
.message.user {{ border-color: #b8d8d8; }}
.message.user header {{ background: #e5f3f3; color: #165c5c; }}
.content {{ overflow-wrap: anywhere; padding: 3mm; }}
.content > :first-child {{ margin-top: 0; }}
.content > :last-child {{ margin-bottom: 0; }}
.content h1 {{ font-size: 16pt; margin: 5mm 0 2mm; }}
.content h2 {{ font-size: 14pt; margin: 4mm 0 2mm; }}
.content h3 {{ font-size: 12pt; margin: 3mm 0 1mm; }}
.content p {{ margin: 0 0 2.5mm; }}
.content ul, .content ol {{ margin: 0 0 3mm; padding-left: 7mm; }}
.content li {{ margin-bottom: 1mm; }}
.content blockquote {{ border-left: 1mm solid #9fb3c8; color: #486581; margin: 3mm 0; padding: 1mm 0 1mm 4mm; }}
.content code {{ background: #eef2f6; border-radius: 1mm; font: 8.5pt monospace; padding: .3mm 1mm; }}
.content pre {{ background: #1f2933; border-radius: 2mm; color: #f5f7fa; font: 7.5pt/1.45 monospace; overflow-wrap: anywhere; padding: 3mm; white-space: pre-wrap; }}
.content pre code {{ background: transparent; color: inherit; padding: 0; }}
.content a {{ color: #126e82; text-decoration: none; }}
.content table {{ border-collapse: collapse; margin: 3mm 0; width: 100%; }}
.content th, .content td {{ border: 1px solid #ccd5df; padding: 1.5mm 2mm; text-align: left; vertical-align: top; }}
.content th {{ background: #eef2f6; }}
.content hr {{ border: 0; border-top: 1px solid #ccd5df; margin: 4mm 0; }}
.content img {{ height: auto; max-width: 100%; }}
.attachment {{ background: #f8fafc; border-left: 1mm solid #9fb3c8; color: #52606d; margin-top: 3mm; padding: 2mm 3mm; }}
</style>
</head>
<body>
<h1>{html.escape(title)}</h1>
<div class="session-link"><a href="{html.escape(public_session_url, quote=True)}">Open session in OpenCode</a></div>
<div class="meta">Created: {html.escape(format_time(created))} &middot; Updated: {html.escape(format_time(updated))}</div>
{"".join(blocks)}
</body>
</html>
"""


def create_pdf(session, messages, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=EXPORT_ROOT) as tmp_dir:
        html_path = Path(tmp_dir) / "session.html"
        pdf_path = Path(tmp_dir) / "session.pdf"
        html_path.write_text(render_html(session, messages), encoding="utf-8")
        result = run_command(
            [
                "chromium",
                "--headless",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--no-pdf-header-footer",
                f"--print-to-pdf={pdf_path}",
                html_path.as_uri(),
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            details = result.stderr.strip() or result.stdout.strip() or "no diagnostic output"
            raise RuntimeError(f"Chromium PDF export failed: {details}")
        os.replace(pdf_path, destination)


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    temporary = STATE_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, STATE_PATH)


def select_sessions(sessions, root_id=None):
    sessions_by_id = {session["id"]: session for session in sessions}
    if root_id is None:
        selected = [session for session in sessions if not session.get("parentID")]
    else:
        if root_id not in sessions_by_id:
            raise ValueError(f"Session not found: {root_id}")

        children = {}
        for session in sessions:
            parent_id = session.get("parentID")
            if parent_id:
                children.setdefault(parent_id, []).append(session)

        selected = []
        visited = set()

        def add_tree(session):
            session_id = session["id"]
            if session_id in visited:
                return
            visited.add(session_id)
            selected.append(session)
            for child in children.get(session_id, []):
                add_tree(child)

        add_tree(sessions_by_id[root_id])

    selected.sort(key=lambda item: (item.get("time", {}).get("created", 0), item.get("id", "")))
    return selected


def export_once(root_id=None):
    all_sessions = api_get("/session", {"directory": DIRECTORY})
    sessions = select_sessions(all_sessions, root_id)
    statuses = api_get("/session/status", {"directory": DIRECTORY})
    explicit = root_id is not None

    state = load_state()
    selected_ids = {session["id"] for session in sessions}
    next_state = dict(state) if explicit else {}
    session_paths = {}
    retained_ids = {
        session_id
        for session_id in state
        if session_id not in selected_ids
        or statuses.get(session_id, {}).get("type") not in (None, "idle")
    }
    used_paths = {
        item["path"]
        for session_id, item in state.items()
        if session_id in retained_ids
        if item.get("path")
    }
    for session in sessions:
        if session["id"] in state and (
            explicit or statuses.get(session["id"], {}).get("type") not in (None, "idle")
        ):
            session_paths[session["id"]] = Path(state[session["id"]]["path"])
            continue
        created = datetime.fromtimestamp(session["time"]["created"] / 1000, TZ)
        stem = f'{created:%Y-%m-%d} {safe_title(session.get("title", ""))}'
        relative = Path(f"{created:%Y}") / f"{created:%m}" / f"{created:%d}" / f"{stem}.pdf"
        counter = 2
        while str(relative) in used_paths:
            relative = relative.with_name(f"{stem} ({counter}).pdf")
            counter += 1
        used_paths.add(str(relative))
        session_paths[session["id"]] = relative

    for session in sessions:
        session_id = session["id"]
        status = statuses.get(session_id, {})
        if not explicit and status.get("type") not in (None, "idle"):
            print(f"Skipping active session {session_id}", flush=True)
            if session_id in state:
                next_state[session_id] = state[session_id]
            continue

        relative = session_paths[session_id]
        marker = {
            "format": EXPORT_FORMAT_VERSION,
            "updated": session["time"].get("updated"),
            "path": str(relative),
        }
        destination = EXPORT_ROOT / relative
        if state.get(session_id) != marker or not destination.exists():
            messages = api_get(f"/session/{urllib.parse.quote(session_id, safe='')}/message", {"directory": DIRECTORY})
            create_pdf(session, messages, destination)
            print(f"Exported {session_id} to {relative}", flush=True)
        next_state[session_id] = marker

    if not explicit:
        expected = {EXPORT_ROOT / item["path"] for item in next_state.values()}
        for existing in EXPORT_ROOT.glob("**/*.pdf"):
            if existing not in expected:
                existing.unlink()
        for directory in sorted(EXPORT_ROOT.glob("**/*"), reverse=True):
            if directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()

    run_command(
        [
            "rclone",
            "sync",
            str(EXPORT_ROOT),
            f"{RCLONE_REMOTE}:{REMOTE_PATH}",
            "--exclude",
            ".export-state.json",
            "--create-empty-src-dirs",
            "--log-level",
            "INFO",
        ],
    )
    save_state(next_state)


def main():
    parser = argparse.ArgumentParser(description="Export OpenCode sessions as PDF")
    parser.add_argument(
        "session_id",
        nargs="?",
        help="Export this session and all descendants recursively, then exit",
    )
    args = parser.parse_args()
    try:
        validate_public_url(PUBLIC_URL)
    except ValueError as error:
        raise SystemExit(str(error)) from error
    if not RCLONE_REMOTE:
        raise SystemExit("RCLONE_REMOTE must not be empty")
    if not REMOTE_PATH:
        raise SystemExit("SESSION_EXPORT_REMOTE_PATH must not be empty")
    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    delay = interval_seconds(INTERVAL)
    try:
        if args.session_id:
            export_once(args.session_id)
            return
        while True:
            try:
                export_once()
            except Exception as error:
                print(f"Session export failed: {error}", flush=True)
                time.sleep(min(delay, 30))
                continue
            time.sleep(delay)
    except ShutdownRequested:
        print("Session exporter stopped", flush=True)


if __name__ == "__main__":
    main()
