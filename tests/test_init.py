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
            "COMPOSE_PROFILES": "",
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
            self.assertIn("wiki-analysis-save", agents)
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
                COMPOSE_PROFILES="paperless",
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
            self.run_init(root, COMPOSE_PROFILES="paperless")
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

            self.run_init(root, COMPOSE_PROFILES="paperless")

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

    def test_second_init_with_service_data_succeeds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "knowledge"
            self.run_init(root, COMPOSE_PROFILES="paperless")

            # Service data between two init runs: paperless error reports and
            # quarantined WebDAV files in the current layout.
            webdav_quarantine = root / "quarantine" / "webdav"
            webdav_quarantine.mkdir()
            (webdav_quarantine / "file.pdf.error").write_text("error")
            (root / "quarantine" / "paperless" / "document-42.txt").write_text("error")

            self.run_init(root, COMPOSE_PROFILES="paperless")

            self.assertEqual(
                (root / "quarantine" / "webdav" / "file.pdf.error").read_text(),
                "error",
            )
            self.assertEqual(
                (root / "quarantine" / "paperless" / "document-42.txt").read_text(),
                "error",
            )

    def test_quarantine_migration_ignores_webdav_quarantine(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "knowledge"
            # A legacy layout: flat quarantine files plus an already current
            # WebDAV quarantine directory.
            (root / "quarantine" / "webdav").mkdir(parents=True)
            (root / "quarantine" / "webdav" / "file.pdf.error").write_text("error")
            (root / "quarantine" / "document-42.txt").write_text("legacy")

            self.run_init(root, COMPOSE_PROFILES="paperless")

            self.assertEqual(
                (root / "quarantine" / "webdav" / "file.pdf.error").read_text(),
                "error",
            )
            self.assertEqual(
                (root / "quarantine" / "paperless" / "document-42.txt").read_text(),
                "legacy",
            )
            self.assertFalse((root / "quarantine" / "document-42.txt").exists())

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

    def test_paperless_profile_controls_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "knowledge"
            self.run_init(root, COMPOSE_PROFILES="webdav,paperless,session-export")
            self.assertTrue((root / "sources" / "paperless").is_dir())
            self.assertTrue((root / "quarantine" / "paperless").is_dir())


class ConfigTests(unittest.TestCase):
    def test_config_is_json_and_restricts_sources(self):
        config = json.loads((ROOT / "config" / "opencode.json").read_text())
        self.assertNotIn("$schema", config)
        self.assertEqual(config["update"], "disable")
        self.assertEqual(config["skills"], ["/etc/opencode/skills"])
        self.assertNotIn("permission", config)
        self.assertNotIn("agent", config)
        self.assertNotIn("command", config)
        rules = {
            (rule["action"], rule["resource"]): rule["effect"]
            for rule in config["permissions"]
        }
        self.assertEqual(rules[("*", "*")], "deny")
        self.assertEqual(rules[("edit", "/knowledge/sources/**")], "deny")
        self.assertEqual(rules[("edit", "/knowledge/raw/**")], "deny")
        self.assertEqual(rules[("external_directory", "/knowledge/sources/**")], "allow")
        ingest_new = config["commands"]["ingest-new"]
        self.assertNotIn("WebDAV", ingest_new["description"] + ingest_new["template"])
        self.assertNotIn("Paperless", ingest_new["description"] + ingest_new["template"])
        self.assertIn("/knowledge/sources", ingest_new["template"])
        analyse_save = config["commands"]["analyse-save"]
        self.assertEqual(analyse_save["agent"], "wiki-analysis-save")
        self.assertTrue(analyse_save["subagent"])
        save_agent = config["agents"]["wiki-analysis-save"]
        self.assertEqual(save_agent["mode"], "subagent")
        self.assertEqual(
            [rule["effect"] for rule in save_agent["permissions"] if rule["action"] == "subagent"],
            ["deny"],
        )
        self.assertEqual(rules[("skill", "wiki-analysis-save")], "allow")
        generic_status = "".join(
            path.read_text().lower()
            for path in (
                ROOT / "config" / "plugins" / "wiki-ingest-status.js",
                ROOT / "config" / "tools" / "wiki_ingest_status_core.mjs",
            )
        )
        self.assertNotIn("webdav", generic_status)
        self.assertNotIn("paperless", generic_status)

    def test_skills_declare_matching_frontmatter(self):
        skills = ROOT / "config" / "skills"
        directories = sorted(path.name for path in skills.iterdir() if path.is_dir())
        self.assertEqual(
            directories, ["wiki-analysis", "wiki-analysis-save", "wiki-ingest", "wiki-lint"]
        )
        for directory in directories:
            text = (skills / directory / "SKILL.md").read_text()
            self.assertTrue(text.startswith("---\n"), directory)
            self.assertIn(f"name: {directory}\n", text)
            self.assertIn("description: ", text)
        save_skill = (skills / "wiki-analysis-save" / "SKILL.md").read_text()
        self.assertIn("wiki/assets/analyses/", save_skill)
        self.assertIn("{{inhalt}}", save_skill)
        self.assertIn(".fs/assets/analyses/", save_skill)

    def test_compose_passes_profiles_to_init(self):
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn("COMPOSE_PROFILES: ${COMPOSE_PROFILES:-}", compose)
        self.assertNotIn("PAPERLESS_ENABLED", compose)

if __name__ == "__main__":
    unittest.main()
