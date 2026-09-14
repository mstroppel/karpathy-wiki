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

    def test_nextcloud_only_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "knowledge"
            self.run_init(root)

            agents = (root / "wiki" / "AGENTS.md").read_text()
            self.assertIn("# Example Wiki", agents)
            self.assertIn("https://wiki.example.test/<pfad-ohne-.md>", agents)
            self.assertFalse((root / "sources" / "paperless").exists())
            self.assertTrue((root / "sources" / "nextcloud").is_dir())
            self.assertTrue((root / "wiki" / ".git").is_dir())
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

    def test_paperless_template_is_conditional(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "knowledge"
            self.run_init(root, PAPERLESS_ENABLED="true")
            agents = (root / "wiki" / "AGENTS.md").read_text()
            self.assertIn("/knowledge/sources/paperless/revoked.md", agents)
            self.assertTrue((root / "sources" / "paperless").is_dir())

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
        self.assertEqual(
            set(config["skills"]["paths"]), {"/etc/opencode/skills"}
        )


if __name__ == "__main__":
    unittest.main()
