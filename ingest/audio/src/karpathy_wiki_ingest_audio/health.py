"""Audio readiness checks both the provider and its live speech dependency."""

import os
from pathlib import Path

from karpathy_wiki_speech.liveness import read_healthy


def main() -> None:
    try:
        read_healthy(Path(os.getenv("HEALTH_PATH", "/tmp/health.json")))
        read_healthy(Path(os.getenv("SPEECH_ROOT", "/data/speech")) / "worker-health.json")
    except RuntimeError:
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
