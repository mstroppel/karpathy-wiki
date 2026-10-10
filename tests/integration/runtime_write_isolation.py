"""Exercise the shipped OpenCode API and actual kernel write failures, without models."""

import base64
import errno
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from functools import partial
from pathlib import Path

PROTECTED = (
    "/knowledge/wiki",
    "/knowledge/sources",
    "/knowledge/incoming/ingest-journal",
    "/knowledge/incoming/answers",
)
SENTINEL = "synthetic unchanged bytes\n"
LOCATION = urllib.parse.urlencode({"location[directory]": "/knowledge/wiki"})


def request(path, data=None, authenticated=True, content_type="application/json"):
    headers = {"Content-Type": content_type}
    if authenticated:
        token = base64.b64encode(f"opencode:{os.environ['OPENCODE_PASSWORD']}".encode())
        headers["Authorization"] = f"Basic {token.decode()}"
    req = urllib.request.Request(f"http://127.0.0.1:4096{path}", data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def api(path, data=None):
    status, body = request(path, None if data is None else json.dumps(data).encode())
    assert status in (200, 204), f"API probe request failed: HTTP {status}: {body.decode()}"
    if status == 204:
        return None
    result = json.loads(body)
    return result.get("data", result)


def wait_file(path):
    for _ in range(60):
        if Path(path).exists():
            return json.loads(Path(path).read_text())
        time.sleep(1)
    raise RuntimeError("probe completion timed out")


def check_unchanged():
    for directory in PROTECTED:
        assert Path(directory, "isolation-sentinel").read_text() == SENTINEL
        assert not Path(directory, "new-file").exists()


def snapshot():
    files = {}
    for directory in PROTECTED:
        for path in Path(directory).rglob("*"):
            metadata = path.lstat()
            content = (
                os.readlink(path)
                if path.is_symlink()
                else (
                    hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "directory"
                )
            )
            files[str(path)] = (metadata.st_mode, metadata.st_uid, metadata.st_gid, content)
    return files


def child_probe(result_file):
    for directory in PROTECTED:
        target = Path(directory, "isolation-sentinel")
        actions = (
            partial(target.write_text, "overwrite"),
            partial(Path(directory, "new-file").write_text, "create"),
            target.unlink,
            partial(target.rename, Path(directory, "new-file")),
            partial(target.chmod, 0o777),
            Path(directory, "new-file").mkdir,
            partial(Path(directory, "new-file").symlink_to, "/tmp"),
        )
        for action in actions:
            try:
                action()
            except OSError as error:
                assert error.errno == errno.EROFS, "not a kernel read-only denial"
            else:
                raise AssertionError("protected write succeeded")
    # Alternate path spelling and links still resolve to the protected mount.
    alias = Path("/tmp/wiki-link")
    alias.unlink(missing_ok=True)
    alias.symlink_to("/knowledge/wiki", target_is_directory=True)
    for target in (
        alias / "isolation-sentinel",
        Path("/proc/self/root/knowledge/wiki/isolation-sentinel"),
    ):
        try:
            target.write_text("bypass")
        except OSError as error:
            assert error.errno == errno.EROFS
        else:
            raise AssertionError("alias write succeeded")
    try:
        os.link("/knowledge/wiki/isolation-sentinel", "/tmp/hardlink-bypass")
    except OSError as error:
        assert error.errno in (errno.EXDEV, errno.EROFS)
    else:
        raise AssertionError("writable hardlink escape succeeded")
    remount = subprocess.run(["mount", "-o", "remount,rw", "/knowledge/wiki"], capture_output=True)
    assert remount.returncode != 0
    check_unchanged()
    Path(result_file).write_text(
        json.dumps({"kernel_denial": "EROFS", "protected_roots": len(PROTECTED)})
    )


def main(mode):
    for _ in range(60):
        try:
            if request("/api/info")[0] == 200:
                break
        except urllib.error.URLError:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("backend startup timed out")
    assert request("/api/info", authenticated=False)[0] == 401

    def write(path, data, location="/knowledge/wiki"):
        query = urllib.parse.urlencode({"location[directory]": location, "path": path})
        return request(
            f"/api/experimental/fs/write?{query}", data, content_type="application/octet-stream"
        )[0]

    control = "/tmp/api-writable-control"
    assert write(control, b"control") == 200
    assert Path(control).read_bytes() == b"control"
    if mode == "manual":
        assert write("/knowledge/wiki/isolation-sentinel", b"manual control") == 200
        assert Path("/knowledge/wiki/isolation-sentinel").read_bytes() == b"manual control"
        assert write("/knowledge/wiki/isolation-sentinel", SENTINEL.encode()) == 200
        print("runtime-write-isolation: manual writable control OK")
        return

    baseline = snapshot()
    for directory in PROTECTED:
        for filename in ("isolation-sentinel", "new-file"):
            for location in ("/knowledge/wiki", "/tmp"):
                assert write(f"{directory}/{filename}", b"forbidden", location) >= 400
    check_unchanged()
    # Session state and read access remain usable.
    session = api(
        "/api/session",
        {"title": "Synthetic write isolation", "location": {"directory": "/knowledge/wiki"}},
    )
    api(f"/api/session/{session['id']}")

    command = "python3 /tmp/runtime_write_isolation.py --child"
    # The command must actually run and finish, not just fail admission.
    for kind in ("location-shell", "session-shell", "pty"):
        result_path = f"/tmp/{kind}-result.json"
        execution = f"{command} {result_path}"
        if kind == "location-shell":
            api(f"/api/shell?{LOCATION}", {"command": execution})
        elif kind == "session-shell":
            api(f"/api/session/{session['id']}/shell", {"command": execution})
        else:
            api(
                f"/api/pty?{LOCATION}",
                {"command": "/bin/sh", "args": ["-c", execution], "cwd": "/knowledge/wiki"},
            )
        assert wait_file(result_path)["kernel_denial"] == "EROFS"

    for _ in range(60):
        if Path("/tmp/core-probe-ready").exists():
            break
        time.sleep(1)
    assert Path("/tmp/core-probe-ready").read_text() == "ready\n"
    api(f"/api/session/{session['id']}/command", {"name": "runtime-write-probe", "text": ""})
    assert wait_file("/tmp/core-probe-result.json") == {
        "journal_core": "EROFS",
        "publication_lock": "EROFS",
        "publisher_core": "rejected_before_publication",
    }
    check_unchanged()
    # Configuration and plugin changes cannot grant kernel write authority.
    assert write("/etc/opencode/opencode.json", b"{}") >= 400
    assert write("/home/opencode/.config/opencode/opencode.json", b"{}") == 200
    assert write("/knowledge/wiki/isolation-sentinel", b"configuration bypass") >= 400
    assert snapshot() == baseline, "protected tree bytes, metadata or entries changed"
    print(
        "runtime-write-isolation: API, location/session shells, PTY, publisher/journal cores, "
        "aliases and remount denied; bytes unchanged"
    )


if __name__ == "__main__":
    if sys.argv[1] == "--child":
        child_probe(sys.argv[2])
    else:
        main(sys.argv[1])
