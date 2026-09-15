import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "config" / "init.sh"


class InitTests(unittest.TestCase):
    def run_init(self, root, **overrides):
        env = {
            **os.environ,
            "KNOWLEDGE_ROOT": str(root),
            "WIKI_NAME": "Example Wiki",
            "WIKI_PUBLIC_URL": "https://wiki.example.test/",
            "GIT_AUTHOR_NAME": "Wiki Agent",
            "GIT_AUTHOR_EMAIL": "wiki@example.test",
            "PAPERLESS_ENABLED": "false",
            "PUID": str(os.getuid()),
            "PGID": str(os.getgid()),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            **overrides,
        }
        return subprocess.run(
            ["sh", str(INIT)], env=env, text=True, capture_output=True, check=True
        )

    def test_webdav_only_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "knowledge"
            self.run_init(root)

            agents = (root / "wiki" / "AGENTS.md").read_text()
            self.assertIn("# Example Wiki", agents)
            self.assertIn("https://wiki.example.test/<pfad-ohne-.md>", agents)
            self.assertFalse((root / "sources" / "paperless").exists())
            self.assertTrue((root / "sources" / "webdav").is_dir())
            self.assertTrue((root / "wiki" / ".git").is_dir())
            self.assertTrue((root / "exports" / "sessions").is_dir())
            self.assertTrue((root / "opencode" / "config").is_dir())
            self.assertTrue((root / "opencode" / "data").is_dir())
            self.assertTrue((root / "opencode" / "state").is_dir())
            self.assertFalse((root / "quarantine").exists())
            author = subprocess.check_output(
                ["git", "-C", str(root / "wiki"), "log", "-1", "--format=%an <%ae>"],
                text=True,
            ).strip()
            self.assertEqual(author, "Wiki Agent <wiki@example.test>")

    def test_existing_content_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "knowledge"
            self.run_init(root)
            wiki = root / "wiki"
            protected = [wiki / name for name in ("AGENTS.md", "index.md", "overview.md", "log.md")]
            for path in protected:
                path.write_text(f"user content for {path.name}\n")

            self.run_init(
                root,
                WIKI_NAME="Changed Name",
                WIKI_PUBLIC_URL="https://changed.example.test",
                PAPERLESS_ENABLED="true",
            )

            for path in protected:
                self.assertEqual(path.read_text(), f"user content for {path.name}\n")
            self.assertTrue((root / "sources" / "paperless").is_dir())

    def test_existing_repository_with_different_owner_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "knowledge"
            wiki = root / "wiki"
            wiki.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", str(wiki)], check=True)

            self.run_init(root, GIT_TEST_ASSUME_DIFFERENT_OWNER="1")

            head = subprocess.check_output(
                ["git", "-C", str(wiki), "rev-parse", "--verify", "HEAD"],
                text=True,
            ).strip()
            self.assertTrue(head)

    def test_paperless_template_is_conditional(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "knowledge"
            self.run_init(root, PAPERLESS_ENABLED="true")
            agents = (root / "wiki" / "AGENTS.md").read_text()
            self.assertIn("/knowledge/sources/paperless/revoked.md", agents)
            self.assertTrue((root / "sources" / "paperless").is_dir())
            self.assertTrue((root / "quarantine" / "paperless").is_dir())

    def test_legacy_service_directories_are_migrated(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "knowledge"
            legacy_files = {
                "opencode-config/tool.js": "tool",
                "opencode-share/auth.json": "credentials",
                "opencode-state/.lock": "state",
                "session-exports/2026/session.pdf": "pdf",
                "sources/nextcloud/folder/source.pdf": "source",
                "quarantine/document-42.txt": "error",
                "quarantine/.marker": "marker",
            }
            for relative, content in legacy_files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)

            # Compose can create the new bind mount targets before init starts.
            for relative in (
                "opencode/config",
                "opencode/data",
                "opencode/state",
                "exports/sessions",
                "sources/webdav",
                "quarantine/paperless",
            ):
                (root / relative).mkdir(parents=True, exist_ok=True)

            self.run_init(root, PAPERLESS_ENABLED="true")

            expected = {
                "opencode/config/tool.js": "tool",
                "opencode/data/auth.json": "credentials",
                "opencode/state/.lock": "state",
                "exports/sessions/2026/session.pdf": "pdf",
                "sources/webdav/folder/source.pdf": "source",
                "quarantine/paperless/document-42.txt": "error",
                "quarantine/paperless/.marker": "marker",
            }
            for relative, content in expected.items():
                self.assertEqual((root / relative).read_text(), content)
            for legacy in (
                "opencode-config",
                "opencode-share",
                "opencode-state",
                "session-exports",
                "sources/nextcloud",
            ):
                self.assertFalse((root / legacy).exists())

    def test_webdav_migration_refuses_populated_old_and_new_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "knowledge"
            old = root / "sources" / "nextcloud" / "old.pdf"
            new = root / "sources" / "webdav" / "new.pdf"
            old.parent.mkdir(parents=True)
            new.parent.mkdir(parents=True)
            old.write_text("old")
            new.write_text("new")

            with self.assertRaises(subprocess.CalledProcessError):
                self.run_init(root)

            self.assertEqual(old.read_text(), "old")
            self.assertEqual(new.read_text(), "new")

    def test_migration_refuses_populated_legacy_and_current_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "knowledge"
            legacy = root / "opencode-share" / "auth.json"
            current = root / "opencode" / "data" / "auth.json"
            legacy.parent.mkdir(parents=True)
            current.parent.mkdir(parents=True)
            legacy.write_text("legacy")
            current.write_text("current")

            with self.assertRaises(subprocess.CalledProcessError):
                self.run_init(root)

            self.assertEqual(legacy.read_text(), "legacy")
            self.assertEqual(current.read_text(), "current")

    def test_invalid_boolean_is_rejected_before_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "knowledge"
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_init(root, PAPERLESS_ENABLED="yes")
            self.assertFalse(root.exists())


class ConfigTests(unittest.TestCase):
    def test_config_is_json_and_restricts_sources(self):
        config = json.loads((ROOT / "config" / "opencode.json").read_text())
        self.assertEqual(config["$schema"], "https://opencode.ai/config.json")
        self.assertEqual(config["permission"]["external_directory"]["*"], "deny")
        self.assertEqual(config["permission"]["edit"]["/knowledge/sources/**"], "deny")
        self.assertEqual(config["permission"]["edit"]["/knowledge/raw/**"], "deny")
        self.assertEqual(
            set(config["skills"]["paths"]), {"/etc/opencode/skills"}
        )
        ingest_new = config["command"]["ingest-new"]
        self.assertNotIn("WebDAV", ingest_new["description"] + ingest_new["template"])
        self.assertNotIn("Paperless", ingest_new["description"] + ingest_new["template"])
        self.assertIn("/knowledge/sources", ingest_new["template"])
        generic_status = "".join(
            (ROOT / "config" / "tools" / name).read_text().lower()
            for name in ("wiki_ingest_status.js", "wiki_ingest_status_core.mjs")
        )
        self.assertNotIn("webdav", generic_status)
        self.assertNotIn("paperless", generic_status)

    def test_compose_passes_paperless_state_to_opencode(self):
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn("PAPERLESS_ENABLED: ${PAPERLESS_ENABLED:-false}", compose)

if __name__ == "__main__":
    unittest.main()
