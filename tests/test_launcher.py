import datetime as dt
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "karpathy-wiki.sh"

DOCKER_STUB = """#!/usr/bin/env python3
import io, json, os, sys, tarfile
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['DOCKER_STUB_LOG'], 'a') as log:
    log.write('---\\n' + '\\n'.join(args) + '\\n')
failure = os.environ.get('STUB_FAIL')
if failure and failure in args:
    print('synthetic-private-secret', file=sys.stderr)
    sys.exit(17)
if args[:2] == ['image', 'inspect']:
    print(json.dumps([{'Id': 'sha256:' + 'a' * 64}]))
elif args[:2] == ['image', 'save']:
    if os.environ.get('STUB_INCOMPLETE'):
        Path(args[args.index('--output') + 1]).write_bytes(b'incomplete archive')
        sys.exit(0)
    with tarfile.open(args[args.index('--output') + 1], 'w') as archive:
        data = b'synthetic saved image'
        member = tarfile.TarInfo('manifest.json')
        member.size = len(data)
        archive.addfile(member, io.BytesIO(data))
elif args[0] == 'inspect':
    print(json.dumps([{'State': {'Running': False}, 'Image': 'sha256:' + 'a' * 64,
                      'Config': {'Labels': {'com.docker.compose.service': 'writer'}}}]))
elif args[0] == 'ps':
    if os.environ.get('STUB_CONTAINERS'): print('synthetic-container')
elif args[0] == 'stop' and os.environ.get('STUB_SWAP_LINK'):
    entry = Path(os.environ['STUB_SWAP_LINK'])
    entry.unlink()
    entry.symlink_to(os.environ['STUB_LINK_TARGET'])
elif args[0] == 'compose' and 'config' in args:
    project = Path(args[args.index('--project-directory') + 1])
    values = dict(line.split('=', 1) for line in (project / '.env').read_text().splitlines()
                  if '=' in line and not line.startswith('#'))
    values = {key: value.strip(\"\\\"'\") for key, value in values.items()}
    if '--environment' in args:
        print('\\n'.join(key + '=' + value for key, value in values.items()))
    elif '--format' in args:
        source = str((project / values.get('DATA_ROOT', 'data')).resolve())
        version = os.environ.get('KARPATHY_WIKI_VERSION', '0.0.1')
        config = {'name': values.get('COMPOSE_PROJECT_NAME', 'test-wiki'),
                  'services': {'writer': {'image': 'synthetic:' + version,
                    'volumes': [{'type': 'bind', 'source': source, 'target': '/data'}]}}}
        if os.environ.get('STUB_CONFIG'):
            config.update(json.loads(os.environ['STUB_CONFIG']))
        print(json.dumps(config))
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
  *"/karpathy-wiki.sh"|*"/launcher_backup.py")
    name=${url##*/}
    [ "${STUB_DOWNLOAD_FAIL:-}" != "$name" ] || exit 7
    cp "$STUB_RELEASE_DIR/$name" "$out"
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
        (self.project / "data").mkdir()
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
        shutil.copy(ROOT / "launcher_backup.py", self.project / "launcher_backup.py")
        self.release = root / "release"
        self.release.mkdir()
        for name in ("karpathy-wiki.sh", "launcher_backup.py"):
            shutil.copy(ROOT / name, self.release / name)
        subprocess.run(
            [
                "python3",
                str(self.project / "launcher_backup.py"),
                "accept",
                str(self.project),
                "0.0.1",
            ],
            check=True,
            capture_output=True,
        )

    def run_launcher(self, *args, env=None, cwd=None, input=None):
        environment = {**os.environ}
        environment.pop("KARPATHY_WIKI_VERSION", None)
        environment.update(
            {
                "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
                "DOCKER_STUB_LOG": str(self.docker_log),
                "CURL_STUB_LOG": str(self.curl_log),
                "STUB_RELEASE_DIR": str(self.release),
                **(env or {}),
            }
        )
        return subprocess.run(
            ["sh", str(self.launcher), *args],
            env=environment,
            cwd=str(cwd or self.project),
            text=True,
            capture_output=True,
            input=input,
        )

    def write_env(self, content):
        env_file = self.project / ".env"
        env_file.write_text(content)
        env_file.chmod(0o600)
        if "KARPATHY_WIKI_VERSION=0.0.1\n" in content:
            self.pin_cache("0.0.1")
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
                "https://raw.githubusercontent.com/mstroppel/karpathy-wiki/v2.0.0-pre.1/karpathy-wiki.sh",
                "https://raw.githubusercontent.com/mstroppel/karpathy-wiki/v2.0.0-pre.1/launcher_backup.py",
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
        metadata = self.snapshot_metadata()[0]
        self.assertEqual(metadata["version"], "0.0.1")
        self.assertEqual(metadata["status"], "success")
        self.assertEqual(env_file.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.cache / "compose-v9.9.9.yaml").exists())
        calls = [call for call in self.docker_calls() if "pull" in call or "up" in call]
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][-1:], ["pull"])
        self.assertIn("--wait", calls[1])
        for call in calls:
            self.assertIn(str(self.cache / "compose-v9.9.9.yaml"), call)

    def test_update_to_explicit_version(self):
        env_file = self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")

        result = self.run_launcher("update", "1.2.3")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("KARPATHY_WIKI_VERSION=1.2.3", env_file.read_text())
        self.assertEqual(
            self.curl_urls(),
            [
                f"https://raw.githubusercontent.com/mstroppel/karpathy-wiki/v1.2.3/{name}"
                for name in ("compose.yaml", "karpathy-wiki.sh", "launcher_backup.py")
            ],
        )

    def test_update_without_env_file_fails(self):
        self.pin_cache("1.2.3")

        result = self.run_launcher("update", "1.2.3")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn(".env", result.stderr)
        self.assertEqual(self.docker_calls(), [])

    def test_no_backup_update_accepts_option_order_and_release_channels(self):
        for args, target in (
            (("--no-backup", "1.2.3"), "1.2.3"),
            (("1.2.3", "--no-backup"), "1.2.3"),
            (("--no-backup",), "2.0.0-pre.1"),
        ):
            with self.subTest(args=args):
                self.write_env("KARPATHY_WIKI_VERSION=0.0.1\nKARPATHY_WIKI_CHANNEL=pre\n")
                result = self.run_launcher("update", *args, env={"STUB_CONTAINERS": "1"})
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("WARNING: --no-backup", result.stderr)
                self.assertIn("no new rollback point", result.stderr)
                self.assertIn(
                    f"KARPATHY_WIKI_VERSION={target}", (self.project / ".env").read_text()
                )
                self.assertEqual(
                    json.loads((self.project / ".launcher-pair.json").read_text())["version"],
                    target,
                )
                self.assertFalse((self.project / "backups").exists())
                calls = self.docker_calls()
                self.assertFalse(any(c[:1] == ["image"] for c in calls))
                stop = next(i for i, c in enumerate(calls) if c[0] == "stop")
                pull = next(i for i, c in enumerate(calls) if "pull" in c)
                self.assertLess(stop, pull)
                self.assertTrue(any("--wait" in c for c in calls))
                self.docker_log.write_text("")

    def test_no_backup_does_not_access_storage_or_archive_data(self):
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        store = self.project / "not-a-directory"
        store.write_text("preserve existing storage")
        os.mkfifo(self.project / "data" / "not-archivable")
        result = self.run_launcher(
            "update",
            "--no-backup",
            "1.2.3",
            env={"KARPATHY_WIKI_BACKUP_DIR": str(store), "STUB_FAIL": "save"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(store.read_text(), "preserve existing storage")
        self.assertEqual(self.snapshot_metadata(), [])

    def test_no_backup_failure_has_no_fictitious_snapshot_or_rollback_command(self):
        for failure in ("pull", "up"):
            with self.subTest(failure=failure):
                self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
                result = self.run_launcher(
                    "update", "1.2.3", "--no-backup", env={"STUB_FAIL": failure}
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("No update backup was created", result.stderr)
                self.assertNotIn("./karpathy-wiki.sh rollback", result.stderr)
                self.assertNotIn("synthetic-private-secret", result.stderr)
                self.assertFalse((self.project / "backups").exists())
                self.assertIn(
                    "KARPATHY_WIKI_VERSION=" + ("0.0.1" if failure == "pull" else "1.2.3"),
                    (self.project / ".env").read_text(),
                )

    def test_update_options_are_validated_before_network_or_docker(self):
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        for args in (
            ("--skip-backup",),
            ("--no-backup", "--no-backup"),
            ("1.2.3", "2.0.0"),
            ("--no-backup", "../bad"),
            ("--no-backup=false",),
        ):
            result = self.run_launcher("update", *args)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.curl_urls(), [])
            self.assertEqual(self.docker_calls(), [])

    def test_no_backup_preserves_acceptance_lock_and_staging_guards(self):
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        (self.project / ".launcher-pair.json").unlink()
        result = self.run_launcher("update", "1.2.3", "--no-backup")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("launcher-accept", result.stderr)
        self.assertEqual(self.run_launcher("launcher-accept", "0.0.1").returncode, 0)
        root = self.project / ".launcher-stage" / "data"
        root.mkdir(parents=True)
        sentinel = root / "keep"
        sentinel.write_text("preserve data")
        self.write_env(f"KARPATHY_WIKI_VERSION=0.0.1\nDATA_ROOT={root}\n")
        result = self.run_launcher("update", "1.2.3", "--no-backup")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("staging overlaps", result.stderr)
        self.assertEqual(sentinel.read_text(), "preserve data")
        self.assertFalse(any("pull" in c or "up" in c for c in self.docker_calls()))

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

    def test_update_installs_matching_pair_and_rollback_restores_actual_old_pair(self):
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        old = {
            name: (self.project / name).read_bytes()
            for name in ("karpathy-wiki.sh", "launcher_backup.py")
        }
        for name in old:
            with (self.release / name).open("a") as stream:
                stream.write("\n# synthetic target release\n")
        result = self.run_launcher("update", "1.2.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in old:
            self.assertEqual((self.project / name).read_bytes(), (self.release / name).read_bytes())
        self.assertEqual(self.launcher.stat().st_mode & 0o777, 0o755)
        record = json.loads((self.project / ".launcher-pair.json").read_text())
        self.assertEqual(record["version"], "1.2.3")
        metadata = self.snapshot_metadata()[0]
        self.assertEqual(metadata["launcher"]["version"], "0.0.1")
        result = self.run_launcher("rollback", metadata["id"], input=f"rollback {metadata['id']}\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in old:
            self.assertEqual((self.project / name).read_bytes(), old[name])
        self.assertEqual(
            json.loads((self.project / ".launcher-pair.json").read_text())["version"], "0.0.1"
        )

    def test_download_or_runtime_validation_failure_leaves_pair_and_env_unchanged(self):
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        original = {
            name: (self.project / name).read_bytes()
            for name in ("karpathy-wiki.sh", "launcher_backup.py", ".env")
        }
        for name in ("karpathy-wiki.sh", "launcher_backup.py"):
            result = self.run_launcher("update", "1.2.3", env={"STUB_DOWNLOAD_FAIL": name})
            self.assertNotEqual(result.returncode, 0)
        (self.release / "launcher_backup.py").write_text(
            "raise RuntimeError('incompatible runtime')\n"
        )
        result = self.run_launcher("update", "1.2.3")
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("incompatible runtime", result.stderr)
        for name, content in original.items():
            self.assertEqual((self.project / name).read_bytes(), content)
        self.assertEqual(self.snapshot_metadata(), [])
        self.assertEqual(self.docker_calls().count(["stop"]), 0)
        self.assertFalse(any("save" in c or "pull" in c or "up" in c for c in self.docker_calls()))

    def test_unregistered_or_modified_pair_requires_explicit_acceptance(self):
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        record = self.project / ".launcher-pair.json"
        record.unlink()
        result = self.run_launcher("update", "1.2.3")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("launcher-accept", result.stderr)
        self.assertEqual(self.run_launcher("launcher-accept", "0.0.1").returncode, 0)
        with self.launcher.open("a") as stream:
            stream.write("\n# local customization\n")
        result = self.run_launcher("update", "1.2.3")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("locally modified", result.stderr)
        self.assertEqual(self.snapshot_metadata(), [])

    def test_accept_rejects_nonprotocol_edits_without_overwriting_record(self):
        record = self.project / ".launcher-pair.json"
        original = record.read_bytes()
        self.launcher.write_text(self.launcher.read_text().replace("# launcher-protocol: 1", ""))
        result = self.run_launcher("launcher-accept", "0.0.1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("protocol", result.stderr)
        self.assertEqual(record.read_bytes(), original)

    def test_update_rejects_staging_overlap_without_deleting_configured_roots(self):
        stage = self.project / ".launcher-stage"
        for setting in ("DATA_ROOT", "KARPATHY_WIKI_BACKUP_DIR", "TOKEN_FILE"):
            with self.subTest(setting=setting):
                root = stage / setting.lower()
                root.mkdir(mode=0o700, parents=True, exist_ok=True)
                sentinel = root / "keep"
                sentinel.write_text("synthetic preserved data")
                (stage / "keep-stage").write_text("preserve stage too")
                value = str(sentinel if setting == "TOKEN_FILE" else root)
                self.write_env(f"KARPATHY_WIKI_VERSION=0.0.1\n{setting}={value}\n")
                result = self.run_launcher("update", "1.2.3")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("staging overlaps", result.stderr)
                self.assertEqual(sentinel.read_text(), "synthetic preserved data")
                self.assertTrue((stage / "keep-stage").exists())
        self.assertFalse(any("save" in c or "pull" in c for c in self.docker_calls()))

    def test_rollback_rejects_saved_staging_overlap_before_loading_or_replacing(self):
        import hashlib
        import io
        import tarfile

        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        self.assertEqual(self.run_launcher("update", "1.2.3").returncode, 0)
        metadata = self.snapshot_metadata()[0]
        saved_root = self.project / ".launcher-stage" / "saved-data"
        metadata["paths"].append(str(saved_root))
        path = self.project / "backups" / metadata["id"] / "metadata.json"
        archive_path = path.parent / "snapshot.tar"
        # A valid saved destination, not merely a path missing from the archive.
        with tarfile.open(archive_path, "a") as archive:
            member = tarfile.TarInfo(str(saved_root).lstrip("/"))
            member.type = tarfile.DIRTYPE
            member.mode = 0o700
            member.uid, member.gid = os.getuid(), os.getgid()
            archive.addfile(member)
            member = tarfile.TarInfo(str(saved_root / "keep-data").lstrip("/"))
            member.size = 5
            member.uid, member.gid = os.getuid(), os.getgid()
            archive.addfile(member, io.BytesIO(b"saved"))
        metadata["sha256"]["snapshot.tar"] = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        path.write_text(json.dumps(metadata))
        marker = self.project / ".launcher-stage" / "preserve-staging"
        marker.write_text("retained evidence")
        current_env = (self.project / ".env").read_bytes()
        self.docker_log.write_text("")
        result = self.run_launcher("rollback", metadata["id"], input=f"rollback {metadata['id']}\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("staging overlaps", result.stderr)
        self.assertEqual(marker.read_text(), "retained evidence")
        self.assertEqual((self.project / ".env").read_bytes(), current_env)
        self.assertFalse(any("load" in c or "down" in c for c in self.docker_calls()))

    def test_rollback_validates_saved_pair_before_any_live_replacement(self):
        import hashlib
        import io
        import tarfile

        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        self.assertEqual(self.run_launcher("update", "1.2.3").returncode, 0)
        metadata = self.snapshot_metadata()[0]
        directory = self.project / "backups" / metadata["id"]
        archive_path = directory / "snapshot.tar"
        original_archive = archive_path.read_bytes()
        current_env = (self.project / ".env").read_bytes()
        current_pair = {
            name: (self.project / name).read_bytes()
            for name in ("karpathy-wiki.sh", "launcher_backup.py")
        }
        sentinel = self.project / "data" / "new-data"
        sentinel.write_text("preserve current data")
        for invalid in ("missing-metadata", "missing-root", "wrong-hash", "nonprotocol"):
            with self.subTest(invalid=invalid):
                archive_path.write_bytes(original_archive)
                changed = json.loads(json.dumps(metadata))
                if invalid == "missing-metadata":
                    del changed["launcher"]
                elif invalid == "missing-root":
                    changed["paths"].remove(str(self.launcher))
                elif invalid == "wrong-hash":
                    changed["launcher"]["sha256"]["karpathy-wiki.sh"] = "0" * 64
                else:
                    # A checksum-valid archive containing unsupported old launcher bytes.
                    content = self.launcher.read_bytes().replace(b"# launcher-protocol: 1", b"")
                    replacement = directory / "modified.tar"
                    with (
                        tarfile.open(archive_path) as source,
                        tarfile.open(replacement, "w") as target,
                    ):
                        for member in source:
                            body = source.extractfile(member) if member.isfile() else None
                            if member.name == str(self.launcher).lstrip("/"):
                                member.size = len(content)
                                body = io.BytesIO(content)
                            target.addfile(member, body)
                    replacement.replace(archive_path)
                    changed["launcher"]["sha256"]["karpathy-wiki.sh"] = hashlib.sha256(
                        content
                    ).hexdigest()
                    changed["sha256"]["snapshot.tar"] = hashlib.sha256(
                        archive_path.read_bytes()
                    ).hexdigest()
                (directory / "metadata.json").write_text(json.dumps(changed))
                self.docker_log.write_text("")
                result = self.run_launcher(
                    "rollback", metadata["id"], input=f"rollback {metadata['id']}\n"
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(sentinel.read_text(), "preserve current data")
                self.assertEqual((self.project / ".env").read_bytes(), current_env)
                for name, content in current_pair.items():
                    self.assertEqual((self.project / name).read_bytes(), content)
                self.assertFalse(
                    any("load" in c or "tag" in c or "down" in c for c in self.docker_calls())
                )

    def test_interrupted_activation_blocks_commands_and_recovers_only_complete_pair(self):
        spec = importlib.util.spec_from_file_location(
            "launcher_backup", ROOT / "launcher_backup.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        stage = self.project / ".launcher-stage"
        shutil.copytree(self.release, stage)
        with (stage / "karpathy-wiki.sh").open("a") as stream:
            stream.write("\n# new release\n")
        record = module.prepare_activation(stage, "1.2.3")
        real_sync = module.sync_directory
        calls = 0

        def crash_after_shell(directory):
            nonlocal calls
            real_sync(directory)
            calls += 1
            if calls == 2:  # durable marker, then first replacement
                raise SystemExit("synthetic power loss")

        with patch.object(module, "sync_directory", side_effect=crash_after_shell):
            with self.assertRaises(SystemExit):
                module.activate_pair(self.project, stage, record, self.project)
        for command in (("up", "-d"), ("update", "1.2.3"), ("launcher-accept", "1.2.3")):
            result = self.run_launcher(*command)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("interrupted launcher activation", result.stderr)
        self.assertEqual(self.docker_calls(), [])
        result = subprocess.run(
            ["python3", str(stage / "launcher_backup.py"), "recover", str(self.project)],
            text=True,
            capture_output=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.project / ".launcher-activation.json").exists())
        self.assertEqual(module.pair_hashes(self.project), record["sha256"])
        self.assertEqual((self.project / ".env").read_text(), "KARPATHY_WIKI_VERSION=0.0.1\n")
        self.assertEqual(self.run_launcher("version").stdout, "0.0.1\n")

    def snapshot_metadata(self):
        return [
            json.loads(p.read_text()) for p in (self.project / "backups").glob("*/metadata.json")
        ]

    def test_delayed_rollback_restores_complete_snapshot_and_rescues_current_state(self):
        import sqlite3

        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\nOPENCODE_PASSWORD=synthetic-secret\n")
        data = self.project / "data"
        wiki = data / "wiki"
        wiki.mkdir()
        subprocess.run(["git", "init", "-q", str(wiki)], check=True)
        (wiki / "index.md").write_text("old wiki")
        subprocess.run(["git", "-C", str(wiki), "add", "."], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(wiki),
                "-c",
                "user.name=Synthetic",
                "-c",
                "user.email=synthetic@example.com",
                "commit",
                "-qm",
                "snapshot",
            ],
            check=True,
        )
        database = sqlite3.connect(data / "state.sqlite3")
        database.execute("create table queue (item text)")
        database.execute("insert into queue values ('old queued item')")
        database.commit()
        database.close()
        (data / ".session").write_text("old session")
        (data / ".session").chmod(0o640)
        external = self.project.parent / "synthetic-secret"
        external.write_text("old secret")
        with (self.project / ".env").open("a") as stream:
            stream.write(f"TOKEN_FILE={external}\n")
        override = self.project / "compose.override.yaml"
        override.write_text("services: {}\n")
        self.assertEqual(self.run_launcher("update", "1.2.3").returncode, 0)
        identifier = self.snapshot_metadata()[0]["id"]
        (wiki / "index.md").write_text("new wiki")
        (data / "new-only").write_text("must not merge")
        external.write_text("new secret")
        override.write_text("services: {} # new\n")
        self.assertEqual(self.run_launcher("update", "2.0.0").returncode, 0)

        result = self.run_launcher("rollback", identifier, input=f"rollback {identifier}\n")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("All changes since", result.stdout)
        self.assertIn("Current version: 2.0.0", result.stdout)
        self.assertEqual((wiki / "index.md").read_text(), "old wiki")
        self.assertFalse((data / "new-only").exists())
        self.assertEqual((data / ".session").read_text(), "old session")
        self.assertEqual((data / ".session").stat().st_mode & 0o777, 0o640)
        self.assertEqual(external.read_text(), "old secret")
        self.assertEqual(override.read_text(), "services: {}\n")
        self.assertIn("KARPATHY_WIKI_VERSION=0.0.1", (self.project / ".env").read_text())
        subprocess.run(["git", "-C", str(wiki), "fsck", "--full"], check=True, capture_output=True)
        with sqlite3.connect(data / "state.sqlite3") as database:
            self.assertEqual(
                database.execute("select item from queue").fetchone(), ("old queued item",)
            )
        database.close()
        self.assertEqual(len([m for m in self.snapshot_metadata() if m["kind"] == "rescue"]), 1)
        self.assertNotIn("synthetic-secret", result.stdout + result.stderr)

    def test_update_failures_retain_snapshot_and_offer_explicit_rollback(self):
        for failure in ("pull", "up"):
            with self.subTest(failure=failure):
                self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
                result = self.run_launcher("update", "1.2.3", env={"STUB_FAIL": failure})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("rollback", result.stderr)
                self.assertEqual(self.snapshot_metadata()[-1]["status"], "failed")
                self.assertNotIn("synthetic-private-secret", result.stderr)

    def test_archive_failure_blocks_update_without_changing_env_or_data(self):
        original = "KARPATHY_WIKI_VERSION=0.0.1\n"
        self.write_env(original)
        result = self.run_launcher("update", "1.2.3", env={"STUB_FAIL": "save"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.project / ".env").read_text(), original)
        self.assertEqual(self.snapshot_metadata(), [])
        self.assertFalse(any("pull" in c or "up" in c for c in self.docker_calls()))

    def test_rollback_needs_confirmation_and_successful_rescue(self):
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        self.assertEqual(self.run_launcher("update", "1.2.3").returncode, 0)
        identifier = self.snapshot_metadata()[0]["id"]
        original = (self.project / ".env").read_text()
        result = self.run_launcher("rollback", identifier, input="no\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(len(self.snapshot_metadata()), 1)
        result = self.run_launcher(
            "rollback", identifier, input=f"rollback {identifier}\n", env={"STUB_FAIL": "save"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.project / ".env").read_text(), original)
        self.assertFalse(any("load" in c or "down" in c for c in self.docker_calls()))

    def test_stop_all_writers_precedes_image_archive_and_pull(self):
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        result = self.run_launcher("update", "1.2.3", env={"STUB_CONTAINERS": "1"})
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.docker_calls()
        stop = next(i for i, c in enumerate(calls) if c[0] == "stop")
        save = next(i for i, c in enumerate(calls) if c[:2] == ["image", "save"])
        pull = next(i for i, c in enumerate(calls) if "pull" in c)
        self.assertLess(stop, save)
        self.assertLess(save, pull)

    def test_unsupported_mounts_and_missing_paths_block_before_stopping(self):
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        for mount in (
            {"type": "volume", "source": "private-data"},
            {"type": "bind", "source": str(self.project.parent), "read_only": False},
        ):
            config = {"services": {"writer": {"image": "synthetic:0.0.1", "volumes": [mount]}}}
            result = self.run_launcher("update", "1.2.3", env={"STUB_CONFIG": json.dumps(config)})
            self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(c[0] == "stop" or "save" in c for c in self.docker_calls()))
        self.assertIn("0.0.1", (self.project / ".env").read_text())

    def test_private_storage_and_no_secret_values_in_list(self):
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\nOPENCODE_PASSWORD=synthetic-private-secret\n")
        self.assertEqual(self.run_launcher("update", "1.2.3").returncode, 0)
        store = self.project / "backups"
        self.assertEqual(store.stat().st_mode & 0o777, 0o700)
        for path in store.glob("*/*"):
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        result = self.run_launcher("backup", "list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("synthetic-private-secret", result.stdout + result.stderr)
        self.assertIn(self.snapshot_metadata()[0]["id"], result.stdout)

    def test_retention_boundary_latest_three_failed_and_rescue_protection(self):
        spec = importlib.util.spec_from_file_location(
            "launcher_backup", ROOT / "launcher_backup.py"
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        self.assertEqual(self.run_launcher("update", "1.2.3").returncode, 0)
        template = self.snapshot_metadata()[0]
        store = self.project / "backups"
        shutil.rmtree(store / template["id"])
        now = dt.datetime(2026, 10, 12, tzinfo=dt.UTC)
        for number, days, kind, status in [
            (1, 31, "update", "success"),
            (2, 31, "update", "failed"),
            (3, 30, "update", "success"),
            (4, 29.999, "update", "success"),
            (5, 28, "update", "success"),
            (6, 27, "update", "success"),
            (7, 26, "update", "success"),
            (8, 40, "rescue", "pending"),
        ]:
            metadata = {
                **template,
                "id": str(number),
                "kind": kind,
                "status": status,
                "created": (now - dt.timedelta(days=days)).isoformat(),
            }
            directory = store / str(number)
            directory.mkdir()
            (directory / "metadata.json").write_text(json.dumps(metadata))
        instance = type("Fixture", (), {"store": store, "identity": template["instance"]})()
        module.prune(instance, now=now)
        self.assertEqual({p.name for p in store.iterdir()}, {"2", "4", "5", "6", "7", "8"})
        with patch.object(
            module,
            "backups",
            return_value=[
                {**template, "id": str(i), "created": (now - dt.timedelta(days=90)).isoformat()}
                for i in (5, 6, 7)
            ],
        ):
            module.prune(instance, now=now)
        self.assertTrue(all((store / str(i)).exists() for i in (5, 6, 7)))

    def test_missing_secret_and_insecure_or_nested_storage_block_update(self):
        original = "KARPATHY_WIKI_VERSION=0.0.1\nTOKEN_FILE=./missing-secret\n"
        self.write_env(original)
        result = self.run_launcher("update", "1.2.3")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.project / ".env").read_text(), original)
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        for location, mode in (
            (self.project / "data" / "nested-backup", 0o700),
            (self.project / "public-backup", 0o755),
        ):
            location.mkdir(mode=mode)
            result = self.run_launcher(
                "update", "1.2.3", env={"KARPATHY_WIKI_BACKUP_DIR": str(location)}
            )
            self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any("save" in c or "pull" in c for c in self.docker_calls()))

    def test_corrupted_or_foreign_snapshot_is_not_restored(self):
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        self.assertEqual(self.run_launcher("update", "1.2.3").returncode, 0)
        metadata = self.snapshot_metadata()[0]
        identifier = metadata["id"]
        original = (self.project / ".env").read_text()
        metadata_path = self.project / "backups" / identifier / "metadata.json"
        metadata_path.write_text(json.dumps({**metadata, "instance": "another-instance"}))
        result = self.run_launcher("rollback", identifier, input=f"rollback {identifier}\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different instance", result.stderr)
        metadata_path.write_text(json.dumps(metadata))
        archive = metadata_path.parent / "snapshot.tar"
        with archive.open("ab") as stream:
            stream.write(b"synthetic corruption")
        result = self.run_launcher("rollback", identifier, input=f"rollback {identifier}\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("checksum", result.stderr)
        self.assertEqual((self.project / ".env").read_text(), original)
        self.assertEqual(len(self.snapshot_metadata()), 1)

    def test_restore_failure_keeps_target_and_rescue_snapshots(self):
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        self.assertEqual(self.run_launcher("update", "1.2.3").returncode, 0)
        identifier = self.snapshot_metadata()[0]["id"]
        result = self.run_launcher(
            "rollback", identifier, input=f"rollback {identifier}\n", env={"STUB_FAIL": "load"}
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Restore failed", result.stderr)
        self.assertEqual(len(self.snapshot_metadata()), 2)
        self.assertIn("KARPATHY_WIKI_VERSION=1.2.3", (self.project / ".env").read_text())

    def test_failed_update_allows_immediate_explicit_rollback(self):
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        result = self.run_launcher("update", "1.2.3", env={"STUB_FAIL": "up"})
        self.assertNotEqual(result.returncode, 0)
        identifier = self.snapshot_metadata()[0]["id"]
        result = self.run_launcher("rollback", identifier, input=f"rollback {identifier}\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("KARPATHY_WIKI_VERSION=0.0.1", (self.project / ".env").read_text())

    def test_external_read_only_mount_env_file_secret_and_proxy_are_included(self):
        import tarfile

        files = [
            self.project.parent / name for name in ("ro-config", "service.env", "secret", "proxy")
        ]
        for path in files:
            path.write_text("synthetic private content")
        self.write_env(
            "KARPATHY_WIKI_VERSION=0.0.1\n"
            f"KARPATHY_WIKI_BACKUP_EXTRA_PATHS='[{json.dumps(str(files[3]))}]'\n"
        )
        config = {
            "services": {
                "writer": {
                    "image": "synthetic:0.0.1",
                    "env_file": [{"path": str(files[1])}],
                    "volumes": [{"type": "bind", "source": str(files[0]), "read_only": True}],
                }
            },
            "secrets": {"private": {"file": str(files[2])}},
        }
        result = self.run_launcher("update", "1.2.3", env={"STUB_CONFIG": json.dumps(config)})
        self.assertEqual(result.returncode, 0, result.stderr)
        metadata = self.snapshot_metadata()[0]
        with tarfile.open(self.project / "backups" / metadata["id"] / "snapshot.tar") as archive:
            names = archive.getnames()
        self.assertTrue(all(str(path).lstrip("/") in names for path in files))
        self.assertNotIn("synthetic private content", result.stdout + result.stderr)

    def test_actual_other_installation_cannot_list_or_rollback_shared_backups(self):
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        self.assertEqual(self.run_launcher("update", "1.2.3").returncode, 0)
        identifier = self.snapshot_metadata()[0]["id"]
        other = self.project.parent / "other-wiki"
        shutil.copytree(self.project, other)
        environment = {
            "KARPATHY_WIKI_PROJECT_DIR": str(other),
            "KARPATHY_WIKI_BACKUP_DIR": str(self.project / "backups"),
        }
        result = self.run_launcher("backup", "list", env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(identifier, result.stdout)
        result = self.run_launcher(
            "rollback", identifier, env=environment, input=f"rollback {identifier}\n"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("different instance", result.stderr)

    def test_incomplete_archive_blocks_update_before_version_changes(self):
        original = "KARPATHY_WIKI_VERSION=0.0.1\n"
        self.write_env(original)
        result = self.run_launcher("update", "1.2.3", env={"STUB_INCOMPLETE": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.project / ".env").read_text(), original)
        self.assertEqual(self.snapshot_metadata(), [])
        self.assertFalse(any("pull" in call or "up" in call for call in self.docker_calls()))

    def test_external_symlink_blocks_snapshot_without_touching_data(self):
        original = "KARPATHY_WIKI_VERSION=0.0.1\n"
        self.write_env(original)
        external = self.project.parent / "external-session"
        external.write_text("synthetic external data")
        (self.project / "data" / ".session").symlink_to(external)
        result = self.run_launcher("update", "1.2.3")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("external symlink", result.stderr)
        self.assertEqual((self.project / ".env").read_text(), original)
        self.assertEqual(external.read_text(), "synthetic external data")

    def container_links(self):
        links = {"opencode/config/AGENTS.md": "/etc/opencode/routing.md"}
        for plugin in (ROOT / "config" / "plugins").glob("*.js"):
            links[f"opencode/config/plugins/{plugin.name}"] = f"/etc/opencode/plugins/{plugin.name}"
        for relative, target in links.items():
            path = self.project / "data" / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target)
        return links

    def test_container_links_survive_repeated_updates_and_rollback_rescue(self):
        import tarfile

        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        links = self.container_links()
        generation = self.project / "data" / "sources" / "webdav" / "generations" / "test"
        generation.mkdir(parents=True)
        (generation / "source.md").write_text("synthetic source")
        (generation.parent.parent / "current").symlink_to("generations/test")
        for version in ("1.2.3", "2.0.0"):
            result = self.run_launcher("update", version)
            self.assertEqual(result.returncode, 0, result.stderr)
        identifier = self.snapshot_metadata()[0]["id"]
        (self.project / "data" / "new-only").write_text("new state")
        result = self.run_launcher("rollback", identifier, input=f"rollback {identifier}\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.project / "data" / "new-only").exists())
        for metadata in self.snapshot_metadata():
            with tarfile.open(
                self.project / "backups" / metadata["id"] / "snapshot.tar"
            ) as archive:
                for relative, target in links.items():
                    path = self.project / "data" / relative
                    member = archive.getmember(str(path).lstrip("/"))
                    self.assertTrue(member.issym())
                    self.assertEqual(member.linkname, target)
                    self.assertEqual(os.readlink(path), target)
                self.assertFalse(any(m.name.startswith("etc/opencode/") for m in archive))
        self.assertEqual(os.readlink(generation.parent.parent / "current"), "generations/test")

    def test_container_link_exception_requires_exact_location_and_target(self):
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        links = self.container_links()
        for relative, target in links.items():
            path = self.project / "data" / relative
            path.unlink()
            path.symlink_to(target + ".unexpected")
            result = self.run_launcher("update", "1.2.3")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("external symlink", result.stderr)
            path.unlink()
            path.symlink_to(target)
        other = self.project / "data" / "elsewhere"
        other.symlink_to("/etc/opencode/routing.md")
        result = self.run_launcher("update", "1.2.3")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("external symlink", result.stderr)
        self.assertFalse(any("save" in c or "pull" in c for c in self.docker_calls()))

    def test_container_link_target_is_revalidated_after_stop(self):
        self.write_env("KARPATHY_WIKI_VERSION=0.0.1\n")
        self.container_links()
        result = self.run_launcher(
            "update",
            "1.2.3",
            env={
                "STUB_CONTAINERS": "1",
                "STUB_SWAP_LINK": str(self.project / "data" / "opencode/config/AGENTS.md"),
                "STUB_LINK_TARGET": "/etc/opencode/unexpected.md",
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("external symlink", result.stderr)
        self.assertTrue(any(c[0] == "stop" for c in self.docker_calls()))
        self.assertFalse(any("save" in c or "pull" in c for c in self.docker_calls()))

    def test_launcher_cannot_start_writers_while_maintenance_lock_is_held(self):
        import fcntl

        original = "KARPATHY_WIKI_VERSION=0.0.1\n"
        self.write_env(original)
        with (self.project / ".launcher.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            result = self.run_launcher("up", "-d")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("another launcher operation", result.stderr)
            self.assertEqual(self.docker_calls(), [])
            result = self.run_launcher("update", "1.2.3")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(any("save" in c or "pull" in c for c in self.docker_calls()))
        self.assertEqual((self.project / ".env").read_text(), original)

    def test_lock_precedes_version_resolution_and_compose_downloads(self):
        import fcntl

        self.write_env("KARPATHY_WIKI_VERSION=latest\n")
        with (self.project / ".launcher.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            for args in (
                ("version",),
                ("up", "-d"),
                ("update", "1.2.3"),
                ("update", "1.2.3", "--no-backup"),
                ("backup", "list"),
                ("rollback", "invalid"),
            ):
                with self.subTest(args=args):
                    result = self.run_launcher(*args)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("another launcher operation", result.stderr)
                    self.assertEqual(self.curl_urls(), [])
                    self.assertEqual(self.docker_calls(), [])
        self.assertFalse(self.cache.exists())

    def test_snapshot_revalidates_tree_after_writers_stop(self):
        original = "KARPATHY_WIKI_VERSION=0.0.1\n"
        self.write_env(original)
        entry = self.project / "data" / "session"
        entry.write_text("synthetic session")
        external = self.project.parent / "external"
        external.write_text("keep external")
        result = self.run_launcher(
            "update",
            "1.2.3",
            env={
                "STUB_CONTAINERS": "1",
                "STUB_SWAP_LINK": str(entry),
                "STUB_LINK_TARGET": str(external),
            },
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("external symlink", result.stderr)
        self.assertTrue(any(c[0] == "stop" for c in self.docker_calls()))
        self.assertFalse(any("save" in c or "pull" in c for c in self.docker_calls()))
        self.assertEqual(self.snapshot_metadata(), [])
        self.assertEqual((self.project / ".env").read_text(), original)
        self.assertEqual(external.read_text(), "keep external")

    def test_rollback_rejects_saved_destination_with_changed_symlink_parent(self):
        parent = self.project.parent / "proxy"
        parent.mkdir()
        config = parent / "config"
        config.write_text("original proxy")
        self.write_env(
            f'KARPATHY_WIKI_VERSION=0.0.1\nKARPATHY_WIKI_BACKUP_EXTRA_PATHS=["{config}"]\n'
        )
        result = self.run_launcher("update", "1.2.3")
        self.assertEqual(result.returncode, 0, result.stderr)
        identifier = self.snapshot_metadata()[0]["id"]
        # The current configuration no longer references the old external root.
        self.write_env("KARPATHY_WIKI_VERSION=1.2.3\n")
        parent.rename(parent.with_name("old-proxy"))
        external = self.project.parent / "unrelated"
        external.mkdir()
        (external / "config").write_text("do not overwrite")
        parent.symlink_to(external, target_is_directory=True)
        self.docker_log.write_text("")
        result = self.run_launcher("rollback", identifier, input=f"rollback {identifier}\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("must not be symlinks", result.stderr)
        self.assertEqual((external / "config").read_text(), "do not overwrite")
        self.assertFalse(any("load" in c or "down" in c for c in self.docker_calls()))
        self.assertEqual(len(self.snapshot_metadata()), 2)
        self.assertEqual((self.project / ".env").read_text(), "KARPATHY_WIKI_VERSION=1.2.3\n")


if __name__ == "__main__":
    unittest.main()
