"""Model-free negative probe in a disposable backend, never a release approval."""

import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def request(
    path: str,
    data: bytes | None = None,
    *,
    authenticated: bool = True,
    content_type: str = "application/octet-stream",
):
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


def main() -> None:
    for _ in range(60):
        try:
            status, _ = request("/api/info")
            if status == 200:
                break
        except urllib.error.URLError:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("backend startup timed out")

    status, _ = request("/api/info", authenticated=False)
    assert status == 401, "probe requires an authenticated backend"
    # Explicit location discovery, without a chat or model request.
    location = urllib.parse.urlencode({"location[directory]": "/tmp/guard-probe/wiki"})
    status, _ = request(f"/api/command?{location}")
    assert status == 200, f"location activation: HTTP {status}"
    status, _ = request(f"/api/location/reload?{location}", b"{}", content_type="application/json")
    assert status == 204, f"location reload: HTTP {status}"
    status, _ = request(
        "/api/session",
        json.dumps(
            {"title": "Synthetic guard probe", "location": {"directory": "/tmp/guard-probe/wiki"}}
        ).encode(),
        content_type="application/json",
    )
    assert status == 200, f"session creation: HTTP {status}"
    status, body = request(f"/api/plugin?{location}")
    assert status == 200, f"plugin discovery: HTTP {status}"
    for _ in range(60):
        if Path("/tmp/guard-probe/wiki/.guard-probe-ready").exists():
            break
        time.sleep(1)
    if not Path("/tmp/guard-probe/wiki/.guard-probe-ready").exists():
        _, inventory = request(f"/api/plugin?{location}")
        raise RuntimeError(f"guard activation failed: {inventory.decode()}")
    assert Path("/tmp/guard-probe/wiki/.guard-probe-ready").read_text() == "ready\n"

    # A rejected request alone is insufficient: prove the shell guard executed.
    status, _ = request(
        f"/api/shell?{location}",
        json.dumps({"command": "touch /tmp/guard-probe/shell-write"}).encode(),
        content_type="application/json",
    )
    assert status >= 400, f"shell guard did not reject request: HTTP {status}"
    assert Path("/tmp/guard-probe/shell-denied").read_text() == "denied\n"
    assert not Path("/tmp/guard-probe/shell-write").exists()

    paths = ["/tmp/guard-probe/wiki/.guard-probe-write", "/tmp/guard-probe/outside-location-write"]
    findings = []
    for path in paths:
        query = urllib.parse.urlencode(
            {"location[directory]": "/tmp/guard-probe/wiki", "path": path}
        )
        route = f"/api/experimental/fs/write?{query}"
        status, _ = request(route, b"synthetic probe\n", authenticated=False)
        assert status == 401
        status, _ = request(route, b"synthetic probe\n")
        assert status == 200, f"write probe changed: HTTP {status}; reassess runtime gate"
        assert Path(path).read_bytes() == b"synthetic probe\n"
        findings.append(
            {"operation": "experimental.fs.write", "outside_location": path == paths[1]}
        )

    print(
        json.dumps(
            {
                "probe_completed": True,
                "auto_ingest_admissible": False,
                "reason": "direct filesystem writes bypass permission/tool/shell hooks",
                "bypasses": findings,
                "untested_gates": [
                    "writer ownership and fencing",
                    "idempotent dispatch",
                    "boot warmup",
                    "provider limits",
                ],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
