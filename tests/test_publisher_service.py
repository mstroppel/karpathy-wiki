"""Container-lifetime publisher fencing, without Docker or models."""

import runpy
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1] / "publisher/service.py"
serve = runpy.run_path(str(SERVICE))["serve"]


class PublisherServiceTests(unittest.TestCase):
    def test_rejects_unsafe_state_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "alias").symlink_to(root, target_is_directory=True)
            with self.assertRaises(ValueError):
                serve(root / "alias", root / "boot")

    def test_exclusive_live_instance_and_new_boot_after_exit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            boot_file = root / "boot"

            def start():
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        "import runpy, sys; from pathlib import Path; "
                        'runpy.run_path(sys.argv[1])["serve"]'
                        "(Path(sys.argv[2]), Path(sys.argv[3]))",
                        str(SERVICE),
                        str(root),
                        str(boot_file),
                    ]
                )

                def cleanup():
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)

                self.addCleanup(cleanup)
                for _ in range(500):
                    if boot_file.exists():
                        return process
                    if process.poll() is not None:
                        self.fail("publisher exited before boot identity")
                    time.sleep(0.01)
                self.fail("publisher startup timed out")

            first = start()
            identity = boot_file.read_text()
            self.assertRegex(identity, r"^[0-9a-f]{32}\n$")
            with self.assertRaises(BlockingIOError):
                serve(root, root / "second-boot")
            self.assertFalse((root / "second-boot").exists())
            first.terminate()
            self.assertEqual(first.wait(timeout=5), 0)
            boot_file.unlink()
            second = start()
            self.assertNotEqual(identity, boot_file.read_text())
            second.terminate()
            self.assertEqual(second.wait(timeout=5), 0)
