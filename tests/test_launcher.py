import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "karpathy-wiki.sh"

DOCKER_STUB = """#!/bin/sh
{
  printf -- '---\\n'
  for arg in "$@"; do printf '%s\\n' "$arg"; done
} >> "$DOCKER_STUB_LOG"
"""

CURL_STUB = """#!/bin/sh
if [ "${STUB_OFFLINE:-0}" = "1" ]; then
  exit 7
fi
out=""
url=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then out=$arg; fi
  case "$arg" in
    http*) url=$arg ;;
  esac
  prev=$arg
done
printf '%s\\n' "$url" >> "$CURL_STUB_LOG"
case "$url" in
  *"/releases/latest")
    printf '%s' "https://github.com/mstroppel/karpathy-wiki/releases/tag/v${STUB_LATEST_VERSION:-9.9.9}"
    exit 0
    ;;
  *"api.github.com/repos/"*"/tags"*)
    printf '[{"name": "v9.9.9"}, {"name": "v%s"}, {"name": "v2.0.0-pre.0"}]' \
      "${STUB_PRE_VERSION:-2.0.0-pre.1}"
    exit 0
    ;;
  *"/compose.yaml")
    [ -n "$out" ] || exit 1
    printf 'name: stub-compose\\n' > "$out"
    exit 0
    ;;
esac
exit 1
"""


class LauncherTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.project = root / "wiki"
        self.project.mkdir()
        self.cache = self.project / ".cache"
        self.bin = root / "bin"
        self.bin.mkdir()
        self.docker_log = root / "docker.log"
        self.curl_log = root / "curl.log"
        for name, source in (("docker", DOCKER_STUB), ("curl", CURL_STUB)):
            path = self.bin / name
            path.write_text(source)
            path.chmod(0o755)
        self.launcher = self.project / "karpathy-wiki.sh"
        shutil.copy(LAUNCHER, self.launcher)
        self.launcher.chmod(0o755)

    def run_launcher(self, *args, env=None, cwd=None):
        environment = {**os.environ}
        environment.pop("KARPATHY_WIKI_VERSION", None)
        environment.update(
            {
                "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                "DOCKER_STUB_LOG": str(self.docker_log),
                "CURL_STUB_LOG": str(self.curl_log),
                **(env or {}),
            }
        )
        return subprocess.run(
            ["sh", str(self.launcher), *args],
            env=environment,
            cwd=str(cwd or self.project),
            text=True,
            capture_output=True,
        )

    def write_env(self, content):
        env_file = self.project / ".env"
        env_file.write_text(content)
        env_file.chmod(0o600)
        return env_file

    def pin_cache(self, version):
        self.cache.mkdir(exist_ok=True)
        path = self.cache / f"compose-v{version}.yaml"
        path.write_text("name: cached-compose\n")
        return path

    def docker_calls(self):
        if not self.docker_log.exists():
            return []
        calls = []
        for block in self.docker_log.read_text().split("---\n"):
            lines = [line for line in block.splitlines() if line]
            if lines:
                calls.append(lines)
        return calls

    def curl_urls(self):
        if not self.curl_log.exists():
            return []
        return [line for line in self.curl_log.read_text().splitlines() if line]

    def test_pinned_version_uses_cache_without_network(self):
        self.write_env("KARPATHY_WIKI_VERSION=1.2.3\n")
        cached = self.pin_cache("1.2.3")

        result = self.run_launcher("up", "-d", env={"STUB_OFFLINE": "1"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.docker_calls(),
            [
                [
                    "compose",
                    "--project-directory",
                    str(self.project),
                    "-f",
                    str(cached),
                    "up",
                    "-d",
                ]
            ],
        )
        self.assertEqual(self.curl_urls(), [])

    def test_missing_compose_is_downloaded_and_cached(self):
        self.write_env("KARPATHY_WIKI_VERSION=1.2.3\n")

        result = self.run_launcher("config", "--quiet")

        self.assertEqual(result.returncode, 0, result.stderr)
        cached = self.cache / "compose-v1.2.3.yaml"
        self.assertEqual(cached.read_text(), "name: stub-compose\n")
        self.assertEqual(
            self.curl_urls(),
            ["https://raw.githubusercontent.com/mstroppel/karpathy-wiki/v1.2.3/compose.yaml"],
        )
        self.assertIn("-f", self.docker_calls()[0])
        self.assertIn(str(cached), self.docker_calls()[0])

    def test_override_file_is_applied_after_versioned_compose(self):
        self.write_env("KARPATHY_WIKI_VERSION=1.2.3\n")
        cached = self.pin_cache("1.2.3")
        override = self.project / "compose.override.yaml"
        override.write_text("services: {}\n")

        result = self.run_launcher("ps")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.docker_calls(),
            [
                [
                    "compose",
                    "--project-directory",
                    str(self.project),
                    "-f",
                    str(cached),
                    "-f",
                    str(override),
                    "ps",
                ]
            ],
        )

    def test_latest_resolves_release_and_remembers_pointer(self):
        self.write_env("KARPATHY_WIKI_VERSION=latest\n")

        result = self.run_launcher("version")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "9.9.9\n")
        self.assertEqual((self.cache / "latest").read_text(), "9.9.9\n")

    def test_latest_falls_back_to_pointer_when_offline(self):
        self.write_env("KARPATHY_WIKI_VERSION=latest\n")
        self.cache.mkdir()
        (self.cache / "latest").write_text("8.8.8\n")
        cached = self.pin_cache("8.8.8")

        result = self.run_launcher("up", "-d", env={"STUB_OFFLINE": "1"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(cached), self.docker_calls()[0])

    def test_pr_version_downloads_compose_from_main(self):
        self.write_env("KARPATHY_WIKI_VERSION=pr-42\n")

        result = self.run_launcher("config")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.curl_urls(),
            ["https://raw.githubusercontent.com/mstroppel/karpathy-wiki/main/compose.yaml"],
        )

    def test_pre_channel_resolves_newest_prerelease(self):
        self.write_env("KARPATHY_WIKI_VERSION=pre\n")

        result = self.run_launcher("version")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "2.0.0-pre.1\n")
        self.assertEqual((self.cache / "latest").read_text(), "2.0.0-pre.1\n")

    def test_pre_channel_env_resolves_when_version_is_latest(self):
        self.write_env("KARPATHY_WIKI_VERSION=latest\nKARPATHY_WIKI_CHANNEL=pre\n")

        result = self.run_launcher("version")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "2.0.0-pre.1\n")

    def test_pre_channel_update_pins_newest_prerelease(self):
        env_file = self.write_env("KARPATHY_WIKI_VERSION=0.0.1\nKARPATHY_WIKI_CHANNEL=pre\n")

        result = self.run_launcher("update")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("KARPATHY_WIKI_VERSION=2.0.0-pre.1", env_file.read_text())
        self.assertEqual(
            self.curl_urls(),
            [
                "https://api.github.com/repos/mstroppel/karpathy-wiki/tags?per_page=100",
                "https://raw.githubusercontent.com/mstroppel/karpathy-wiki"
                "/v2.0.0-pre.1/compose.yaml",
            ],
        )

    def test_update_pins_latest_pulls_and_restarts(self):
        env_file = self.write_env("KARPATHY_WIKI_VERSION=0.0.1\nOTHER=keep\n")

        result = self.run_launcher("update")

        self.assertEqual(result.returncode, 0, result.stderr)
        updated = env_file.read_text()
        self.assertIn("KARPATHY_WIKI_VERSION=9.9.9", updated)
        self.assertIn("OTHER=keep", updated)
        self.assertRegex(updated, r"OPENCODE_PASSWORD=[0-9a-f]{48}\n")
        self.assertEqual(
            (self.project / ".env.bak").read_text(),
            "KARPATHY_WIKI_VERSION=0.0.1\nOTHER=keep\n",
        )
        self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.cache / "compose-v9.9.9.yaml").exists())
        calls = self.docker_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][-1:], ["pull"])
        self.assertEqual(calls[1][-2:], ["up", "-d"])
        for call in calls:
            self.assertIn(str(self.cache / "compose-v9.9.9.yaml"), call)

    def test_update_to_explicit_version(self):
        env_file = self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")

        result = self.run_launcher("update", "1.2.3")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("KARPATHY_WIKI_VERSION=1.2.3", env_file.read_text())
        self.assertEqual(
            self.curl_urls(),
            ["https://raw.githubusercontent.com/mstroppel/karpathy-wiki/v1.2.3/compose.yaml"],
        )

    def test_update_without_env_file_fails(self):
        self.pin_cache("1.2.3")

        result = self.run_launcher("update", "1.2.3")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".env", result.stderr)
        self.assertEqual(self.docker_calls(), [])

    def test_invalid_version_is_rejected_before_network(self):
        self.write_env("KARPATHY_WIKI_VERSION=../evil\n")

        result = self.run_launcher("up", "-d")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid", result.stderr)
        self.assertEqual(self.curl_urls(), [])
        self.assertEqual(self.docker_calls(), [])

    def test_environment_version_overrides_env_file(self):
        self.write_env("KARPATHY_WIKI_VERSION=1.2.3\n")
        self.pin_cache("1.2.3")
        cached = self.pin_cache("4.5.6")

        result = self.run_launcher("up", "-d", env={"KARPATHY_WIKI_VERSION": "4.5.6"})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(str(cached), self.docker_calls()[0])

    def test_runs_from_any_working_directory(self):
        self.write_env("KARPATHY_WIKI_VERSION=1.2.3\n")
        cached = self.pin_cache("1.2.3")

        result = self.run_launcher("ps", cwd=self.project.parent)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.docker_calls(),
            [
                [
                    "compose",
                    "--project-directory",
                    str(self.project),
                    "-f",
                    str(cached),
                    "ps",
                ]
            ],
        )

    def test_help_is_shown_without_arguments(self):
        result = self.run_launcher()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Usage:", result.stdout)
        self.assertEqual(self.docker_calls(), [])

    def test_launcher_passes_shell_syntax_check(self):
        result = subprocess.run(["sh", "-n", str(LAUNCHER)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_launcher_is_executable_in_repository(self):
        mode = LAUNCHER.stat().st_mode
        self.assertTrue(mode & stat.S_IXUSR)


if __name__ == "__main__":
    unittest.main()
