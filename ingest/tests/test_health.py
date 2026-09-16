import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from karpathy_wiki_ingest.health import main


class HealthTests(unittest.TestCase):
    def test_recent_success_is_healthy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            path.write_text(json.dumps({"checked_at": 1000, "failed": 0}))
            with mock.patch.dict(
                "os.environ", {"HEALTH_PATH": str(path), "SYNC_INTERVAL_SECONDS": "60"}
            ), mock.patch("karpathy_wiki_ingest.health.time.time", return_value=1100):
                main()

    def test_failure_is_unhealthy(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "health.json"
            path.write_text(json.dumps({"checked_at": 1000, "failed": 1}))
            with mock.patch.dict("os.environ", {"HEALTH_PATH": str(path)}):
                with self.assertRaises(SystemExit):
                    main()


if __name__ == "__main__":
    unittest.main()
