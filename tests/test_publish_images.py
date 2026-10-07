"""Exercise publication retries without contacting a registry."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class PublishImagesTests(unittest.TestCase):
    def run_publish(self, failures=0, target="release", version="0.2.2-pre.51"):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            docker = directory / "docker"
            docker.write_text(
                "#!/bin/sh\n"
                'printf "%s|%s\\n" "$IMAGE_VERSION" "$*" >> "$TEST_DIR/calls"\n'
                'count=$(wc -l < "$TEST_DIR/calls")\n'
                'if [ "$count" -le "$FAILURES" ]; then exit 17; fi\n'
            )
            docker.chmod(0o755)
            sleep = directory / "sleep"
            sleep.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$TEST_DIR/delays"\n')
            sleep.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{directory}:{os.environ['PATH']}",
                "TEST_DIR": str(directory),
                "FAILURES": str(failures),
                "IMAGE_VERSION": version,
            }
            result = subprocess.run(
                ["sh", str(ROOT / "scripts/publish-images.sh"), target],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            calls = directory / "calls"
            delays = directory / "delays"
            return (
                result,
                calls.read_text().splitlines() if calls.exists() else [],
                delays.read_text().splitlines() if delays.exists() else [],
            )

    def test_success_does_not_retry(self):
        result, calls, delays = self.run_publish()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            calls,
            [
                "0.2.2-pre.51|buildx bake --file docker-bake.hcl --push "
                "--set *.platform=linux/amd64,linux/arm64 release"
            ],
        )
        self.assertEqual(delays, [])

    def test_transient_failure_retries_same_version_and_target(self):
        result, calls, delays = self.run_publish(failures=2, target="stable", version="0.2.2")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(set(calls)), 1)
        self.assertTrue(calls[0].startswith("0.2.2|"))
        self.assertTrue(calls[0].endswith(" stable"))
        self.assertEqual(delays, ["30", "60"])

    def test_persistent_failure_preserves_exit_status_and_stops(self):
        result, calls, delays = self.run_publish(failures=10)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(len(calls), 3)
        self.assertEqual(delays, ["30", "60"])
        self.assertIn("failed after 3 attempts", result.stderr)

    def test_invalid_target_does_not_publish(self):
        result, calls, delays = self.run_publish(target="default")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(calls, [])
        self.assertEqual(delays, [])

    def test_empty_version_does_not_publish(self):
        result, calls, delays = self.run_publish(version="")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(calls, [])
        self.assertEqual(delays, [])

    def test_release_workflows_use_retry_script(self):
        for name in ("pre-release.yml", "images.yml"):
            with self.subTest(workflow=name):
                workflow = (ROOT / ".github/workflows" / name).read_text()
                self.assertIn("run: sh scripts/publish-images.sh", workflow)
                self.assertIn("IMAGE_VERSION: ${{ steps.version.outputs.version }}", workflow)
