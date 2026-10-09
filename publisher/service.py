"""Hold a container-lifetime lock; no listener or worker execution."""

import fcntl
import os
import secrets
import signal
from pathlib import Path


def serve(root=Path("/knowledge/publisher"), boot_file=Path("/tmp/publisher-boot-id")):
    if root.resolve() != root or not root.is_dir():
        raise ValueError("unsafe state root")
    descriptor = os.open(root / "instance.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        # A different boot identity is proof of a restart only if a second live
        # publisher cannot claim the same backing state in another container.
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        boot_file.write_text(secrets.token_hex(16) + "\n", encoding="utf-8")
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        while True:
            signal.pause()
    finally:
        os.close(descriptor)


def stop(_signal, _frame):
    raise SystemExit(0)


if __name__ == "__main__":
    try:
        serve()
    except (OSError, ValueError):
        print("publisher: unsafe or occupied state; refusing startup", flush=True)
        raise SystemExit(1) from None
