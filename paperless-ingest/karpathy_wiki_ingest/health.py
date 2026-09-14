import json
import os
import time
from pathlib import Path


def main() -> None:
    health_path = Path(os.getenv("HEALTH_PATH", "/tmp/health.json"))
    payload = json.loads(health_path.read_text(encoding="utf-8"))
    interval = int(os.getenv("SYNC_INTERVAL_SECONDS", "900"))
    maximum_age = max(interval * 2, 120)
    if payload.get("failed") or time.time() - int(payload["checked_at"]) > maximum_age:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
