"""Reader API and executed shell remain fenced beside the trusted publisher."""

import errno
import hashlib
import json
import sys
import time
import urllib.error
import urllib.parse
from pathlib import Path

from runtime_write_isolation import api, request, wait_file

ROOTS = (
    Path("/knowledge/wiki"),
    Path("/knowledge/sources"),
    Path("/knowledge/incoming/ingest-journal"),
)


def snapshot():
    return {
        str(file): hashlib.sha256(file.read_bytes()).hexdigest()
        for root in ROOTS
        for file in root.rglob("*")
        if file.is_file()
    }


def child():
    for root in (*ROOTS, Path("/knowledge/publisher")):
        try:
            (root / "bypass.json").write_text("forbidden")
        except OSError as error:
            assert error.errno in (errno.EROFS, errno.ENOENT)
        else:
            raise AssertionError("reader gained protected write access")
    assert not Path("/knowledge/publisher/control.json").exists()
    assert not Path("/opt/karpathy-wiki/publisher/cli.mjs").exists()
    Path("/tmp/publisher-reader-result.json").write_text(json.dumps({"isolated": True}))


def main():
    for _ in range(60):
        try:
            if request("/api/info")[0] == 200:
                break
        except urllib.error.URLError:
            pass
        time.sleep(1)
    else:
        raise RuntimeError("reader startup timed out")
    before = snapshot()
    for root in (*ROOTS, Path("/knowledge/publisher")):
        query = urllib.parse.urlencode(
            {"location[directory]": "/knowledge/wiki", "path": f"{root}/bypass.json"}
        )
        assert (
            request(
                f"/api/experimental/fs/write?{query}",
                b"forbidden",
                content_type="application/octet-stream",
            )[0]
            >= 400
        )
    query = urllib.parse.urlencode({"location[directory]": "/knowledge/wiki"})
    Path("/tmp/publisher-reader-result.json").unlink(missing_ok=True)
    api(f"/api/shell?{query}", {"command": "python3 /tmp/publisher_reader_probe.py --child"})
    assert wait_file("/tmp/publisher-reader-result.json") == {"isolated": True}
    assert snapshot() == before
    print("trusted-publisher: pinned reader API/shell still isolated; protected bytes unchanged")


if __name__ == "__main__":
    child() if len(sys.argv) > 1 else main()
