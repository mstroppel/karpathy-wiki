import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"

INSTALL_CURL_STUB = """#!/bin/sh
out=""
previous=""
for argument in "$@"; do
  if [ "$previous" = "-o" ]; then out=$argument; fi
  previous=$argument
done
case "$out" in
  *karpathy-wiki.sh) printf '%s\n' '#!/bin/sh' > "$out" ;;
  *.env) printf '%s\n' \
    'COMPOSE_PROJECT_NAME=karpathy-wiki' \
    'STACK_ID=karpathy-wiki' \
    'KARPATHY_WIKI_VERSION=latest' > "$out" ;;
  *api.github.com/repos/*/tags*)
    printf '%s\n' \
      '[' \
      '  {"name": "v1.2.3"},' \
      '  {"name": "v2.0.0-pre.1"},' \
      '  {"name": "v2.0.0-pre.0"}' \
      ']' ;;
  *) exit 1 ;;
esac
"""

INSTALL_PRERELEASE_CURL_STUB = """#!/bin/sh
out=""
url=""
previous=""
for argument in "$@"; do
  if [ "$previous" = "-o" ]; then out=$argument; fi
  case "$argument" in
    http*) url=$argument ;;
  esac
  previous=$argument
done
case "$out" in
  *karpathy-wiki.sh) printf '%s\\n' '#!/bin/sh' > "$out" ;;
  *.env) printf '%s\\n' \
    'COMPOSE_PROJECT_NAME=karpathy-wiki' \
    'STACK_ID=karpathy-wiki' \
    'KARPATHY_WIKI_VERSION=latest' > "$out" ;;
esac
case "$url" in
  *api.github.com/repos/*/tags*)
    printf '%s\\n' \
      '[' \
      '  {"name": "v1.2.3"},' \
      '  {"name": "v2.0.0-pre.1"},' \
      '  {"name": "v2.0.0-pre.0"}' \
      ']' ;;
  *) [ -n "$out" ] || exit 1 ;;
esac
"""

INSTALL_FALLBACK_CURL_STUB = """#!/bin/sh
out=""
previous=""
url=""
for argument in "$@"; do
  if [ "$previous" = "-o" ]; then out=$argument; fi
  case "$argument" in
    https://raw.githubusercontent.com/*) url=$argument ;;
  esac
  previous=$argument
done
case "$url" in
  */v1.2.3/karpathy-wiki.sh)
    printf '%s\n' 'curl: (22) The requested URL returned error: 404' >&2
    exit 22
    ;;
  */main/karpathy-wiki.sh) printf '%s\n' '#!/bin/sh' > "$out" ;;
  */v1.2.3/.env.example) printf '%s\n' \
    'COMPOSE_PROJECT_NAME=karpathy-wiki' \
    'STACK_ID=karpathy-wiki' \
    'KARPATHY_WIKI_VERSION=latest' > "$out" ;;
  *) exit 1 ;;
esac
"""


class InstallTests(unittest.TestCase):
    def run_installer(self, directory, **overrides):
        bin_dir = Path(directory).parent / "bin"
        bin_dir.mkdir(exist_ok=True)
        curl = bin_dir / "curl"
        curl.write_text(overrides.pop("CURL_STUB", INSTALL_CURL_STUB))
        curl.chmod(0o755)
        environment = {
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "KARPATHY_WIKI_VERSION": "1.2.3",
            **overrides,
        }
        return subprocess.run(
            ["sh", str(INSTALLER)],
            cwd=directory,
            env=environment,
            text=True,
            capture_output=True,
        )

    def test_install_uses_directory_name_for_stack_identity(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "my-wiki"
            directory.mkdir()

            result = self.run_installer(directory)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("COMPOSE_PROJECT_NAME and STACK_ID as my-wiki", result.stdout)
            self.assertEqual(
                (directory / ".env").read_text(),
                "COMPOSE_PROJECT_NAME=my-wiki\n"
                "STACK_ID=my-wiki\n"
                "KARPATHY_WIKI_VERSION=1.2.3\n",
            )
            self.assertTrue((directory / "karpathy-wiki.sh").exists())
            self.assertEqual((directory / ".gitignore").read_text(), ".cache/\n")

    def test_install_refuses_non_empty_directory_before_download(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "my-wiki"
            directory.mkdir()
            (directory / "keep.txt").write_text("keep")

            result = self.run_installer(directory)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must be empty", result.stderr)
            self.assertFalse((directory / ".env").exists())

    def test_install_suppresses_expected_missing_release_launcher(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "my-wiki"
            directory.mkdir()

            result = self.run_installer(directory, CURL_STUB=INSTALL_FALLBACK_CURL_STUB)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertTrue((directory / "karpathy-wiki.sh").exists())

    def test_install_rejects_unsafe_directory_name(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "My Wiki"
            directory.mkdir()

            result = self.run_installer(directory)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must start with", result.stderr)

    def test_install_pre_channel_pins_newest_prerelease(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "my-wiki"
            directory.mkdir()

            result = self.run_installer(
                directory,
                CURL_STUB=INSTALL_PRERELEASE_CURL_STUB,
                KARPATHY_WIKI_VERSION="",
                KARPATHY_WIKI_CHANNEL="pre",
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("Installed Karpathy Wiki 2.0.0-pre.1", result.stdout)
            self.assertIn(
                "KARPATHY_WIKI_VERSION=2.0.0-pre.1", (directory / ".env").read_text()
            )

    def test_install_rejects_unknown_channel(self):
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "my-wiki"
            directory.mkdir()

            result = self.run_installer(directory, KARPATHY_WIKI_CHANNEL="beta")

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsupported channel", result.stderr)


if __name__ == "__main__":
    unittest.main()
