import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "config" / "init.sh"
ENTRYPOINT = ROOT / "opencode" / "entrypoint.sh"


class EntrypointTests(unittest.TestCase):
    def test_global_routing_is_installed_without_overwriting_custom_instructions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            opencode = bin_dir / "opencode"
            opencode.write_text("#!/bin/sh\nexit 0\n")
            opencode.chmod(0o755)
            env = {**os.environ, "HOME": str(root), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
            instructions = root / ".config" / "opencode" / "AGENTS.md"

            subprocess.run(["sh", str(ENTRYPOINT), "--version"], env=env, check=True)
            self.assertTrue(instructions.is_symlink())
            self.assertEqual(os.readlink(instructions), "/etc/opencode/routing.md")

            instructions.unlink()
            instructions.write_text("custom instructions\n")
            subprocess.run(["sh", str(ENTRYPOINT), "--version"], env=env, check=True)
            self.assertEqual(instructions.read_text(), "custom instructions\n")


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
            self.assertFalse((root / "sources" / "answers").exists())
            self.assertTrue((root / "incoming" / "answers").is_dir())
            self.assertTrue((root / "sources" / "webdav").is_dir())
            self.assertTrue((root / "wiki" / ".git").is_dir())
            self.assertFalse((root / "exports").exists())
            self.assertTrue((root / "opencode" / "config").is_dir())
            self.assertTrue((root / "opencode" / "data").is_dir())
            self.assertTrue((root / "opencode" / "state").is_dir())
            self.assertFalse((root / "quarantine").exists())
            author = subprocess.check_output(
                ["git", "-C", str(root / "wiki"), "log", "-1", "--format=%an <%ae>"],
                text=True,
            ).strip()
            self.assertEqual(author, "Wiki Agent <wiki@example.test>")

    def test_answers_profile_creates_only_the_provider_source_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "knowledge"
            self.run_init(root, COMPOSE_PROFILES="webdav, answers")
            self.assertTrue((root / "sources" / "answers").is_dir())
            self.assertTrue((root / "wiki" / "sources" / "answers").is_dir())
            self.assertIn("/knowledge/incoming/answers", (root / "wiki" / "AGENTS.md").read_text())
            inbox = root / "incoming" / "answers"
            self.assertEqual(stat.S_IMODE(inbox.stat().st_mode), 0o700)
            self.assertEqual(inbox.stat().st_uid, os.getuid())
            inbox.chmod(0o755)
            (inbox / "existing.md").write_text("retained draft")
            self.run_init(root, COMPOSE_PROFILES="answers")
            self.assertEqual(stat.S_IMODE(inbox.stat().st_mode), 0o700)
            self.assertEqual((inbox / "existing.md").read_text(), "retained draft")

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
            self.assertIn("wiki_ingest_status", agents)
            self.assertIn("status_state: revoked", agents)
            self.assertNotIn("/knowledge/sources/paperless/revoked.md", agents)
            self.assertTrue((root / "sources" / "paperless").is_dir())
            self.assertTrue((root / "quarantine" / "paperless").is_dir())

    def test_paperless_profile_controls_initialization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "knowledge"
            self.run_init(root, COMPOSE_PROFILES="webdav,paperless")
            self.assertTrue((root / "sources" / "paperless").is_dir())
            self.assertTrue((root / "quarantine" / "paperless").is_dir())

    def test_paperless_profile_matching_ignores_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "knowledge"
            self.run_init(root, COMPOSE_PROFILES="webdav, paperless, raw-files")
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
            (rule["action"], rule["resource"]): rule["effect"] for rule in config["permissions"]
        }
        self.assertEqual(rules[("*", "*")], "deny")
        self.assertEqual(rules[("read", "*.env")], "deny")
        self.assertEqual(rules[("read", "*.env.*")], "deny")
        self.assertEqual(rules[("read", "*.env.example")], "allow")
        self.assertEqual(rules[("edit", "/knowledge/sources/**")], "deny")
        self.assertEqual(rules[("external_directory", "/knowledge/sources/**")], "allow")
        self.assertNotIn(("external_directory", "/knowledge/raw/**"), rules)
        tool_output = "/home/opencode/.local/share/opencode/tool-output/**"
        self.assertEqual(rules[("external_directory", tool_output)], "allow")
        self.assertEqual(rules[("edit", tool_output)], "deny")
        template_dir = "/etc/opencode/skills/wiki-analysis-save/**"
        self.assertEqual(rules[("external_directory", template_dir)], "allow")
        self.assertEqual(rules[("edit", "/etc/opencode/skills/**")], "deny")
        self.assertFalse(any(rule["action"] == "shell" for rule in config["permissions"]))
        for agent in ("wiki-ingest", "wiki-lint", "wiki-analysis-save"):
            shell_rules = {
                rule["resource"]: rule["effect"]
                for rule in config["agents"][agent]["permissions"]
                if rule["action"] == "shell"
            }
            self.assertEqual(shell_rules, {"*": "allow"}, agent)
        analysis_rules = config["agents"]["wiki-analysis"]["permissions"]
        self.assertIn({"action": "shell", "resource": "*", "effect": "deny"}, analysis_rules)
        self.assertIn({"action": "edit", "resource": "*", "effect": "deny"}, analysis_rules)
        ingest_new = config["commands"]["ingest-new"]
        self.assertNotIn("WebDAV", ingest_new["description"] + ingest_new["template"])
        self.assertNotIn("Paperless", ingest_new["description"] + ingest_new["template"])
        self.assertIn("/knowledge/sources", ingest_new["template"])
        analysis_save = config["commands"]["analysis-save"]
        self.assertEqual(config["commands"]["analysis"]["agent"], "wiki-analysis")
        self.assertEqual(analysis_save["agent"], "build")
        self.assertFalse(analysis_save["subagent"])
        self.assertIn("letzte vollständige Analyse", analysis_save["template"])
        self.assertIn("vollständigen zu speichernden Text", analysis_save["template"])
        self.assertIn("Index, Log und Druckansicht", analysis_save["template"])
        self.assertIn("wiki-analysis-save", analysis_save["template"])
        self.assertIn("full output saved to", analysis_save["template"])
        self.assertIn("vollständig wiederherstellbar", analysis_save["template"])
        save_agent = config["agents"]["wiki-analysis-save"]
        self.assertEqual(save_agent["mode"], "subagent")
        self.assertEqual(
            [rule["effect"] for rule in save_agent["permissions"] if rule["action"] == "subagent"],
            ["deny"],
        )
        self.assertEqual(rules[("skill", "wiki-analysis-save")], "allow")
        self.assertEqual(rules[("skill", "wiki-gap-review")], "allow")
        self.assertEqual(rules[("edit", "/knowledge/sources/**")], "deny")
        self.assertEqual(rules[("external_directory", "/knowledge/incoming/answers/**")], "allow")
        self.assertEqual(config["commands"]["gap-review"]["agent"], "build")
        self.assertFalse(config["commands"]["gap-review"]["subagent"])
        generic_status = "".join(
            path.read_text().lower()
            for path in (
                ROOT / "config" / "plugins" / "wiki-ingest-status.js",
                ROOT / "config" / "tools" / "wiki_ingest_status_core.mjs",
            )
        )
        self.assertNotIn("webdav", generic_status)
        self.assertNotIn("paperless", generic_status)

    def test_ingest_reports_per_source_details(self):
        config = json.loads((ROOT / "config" / "opencode.json").read_text())
        template = config["commands"]["ingest-new"]["template"]
        skill = (ROOT / "config" / "skills" / "wiki-ingest" / "SKILL.md").read_text()
        orchestrator = (
            ROOT / "config" / "skills" / "wiki-ingest-orchestrator" / "SKILL.md"
        ).read_text()
        renderer = (ROOT / "config" / "tools" / "wiki_ingest_journal_core.mjs").read_text()
        # Bulk runs carry the complete per-source details in the report file and
        # answer with status, unfinished sources, and the report path (agreed
        # contract change for issue #152). Details are never replaced by an
        # aggregate summary and nothing is invented.
        self.assertIn("Pfad zum vollständigen Bericht", template)
        self.assertIn("stehen vollständig im Bericht", template)
        self.assertIn("reine Sammelzusammenfassung", template)
        self.assertIn("stehen vollständig im Bericht", orchestrator)
        self.assertIn("Wurde keine Quelle bearbeitet", orchestrator)
        self.assertIn("erfinde keine Details", orchestrator)
        for field in (
            "Quelle",
            "Commit",
            "Geänderte Seiten",
            "Inhalt",
            "Widersprüche/offene Fragen",
            "Extraktionsgrenzen",
        ):
            self.assertIn(f"**{field}:**", skill)
        # The report renderer keeps every detail block; the source path itself
        # is the block heading.
        for field in (
            "Commit",
            "Geänderte Seiten",
            "Inhalt",
            "Widersprüche/offene Fragen",
            "Extraktionsgrenzen",
        ):
            self.assertIn(f"**{field}:**", renderer)
        self.assertIn("record.source_path", renderer)
        self.assertIn("Mit Git verifizierter", skill)
        self.assertIn("Wiki-Pfade aus dem tatsächlichen Quellen-Commit", skill)
        self.assertIn("Kein Commit", skill)
        self.assertIn("Nicht ermittelt", skill)
        self.assertIn("nur, wenn der Benutzer ausdrücklich genau diese Quelle", skill)
        self.assertIn("ein normaler Auftrag für alle neuen/geänderten Quellen", skill)
        self.assertIn("include_current: true", skill)
        self.assertIn("suche nach dem exakten `source_path`", skill)
        self.assertIn("Quellpfad und Quellschlüssel sind nicht", skill)
        self.assertIn("statt eine Duplikatseite für", skill)
        # Evidence is durable per source, written only after the verified
        # commit, and unverified results are recorded as blocked.
        self.assertIn("wiki_ingest_journal", skill)
        self.assertIn("status: blocked", skill)
        self.assertIn("entsteht erst nach dem Commit und nie davor", skill)

    def test_skills_declare_matching_frontmatter(self):
        skills = ROOT / "config" / "skills"
        directories = sorted(path.name for path in skills.iterdir() if path.is_dir())
        self.assertEqual(
            directories,
            [
                "wiki-analysis",
                "wiki-analysis-save",
                "wiki-gap-review",
                "wiki-ingest",
                "wiki-ingest-orchestrator",
                "wiki-lint",
            ],
        )
        for directory in directories:
            text = (skills / directory / "SKILL.md").read_text()
            self.assertTrue(text.startswith("---\n"), directory)
            self.assertIn(f"name: {directory}\n", text)
            self.assertIn("description: ", text)
        save_skill = (skills / "wiki-analysis-save" / "SKILL.md").read_text()
        self.assertIn("assets/analyses/", save_skill)
        self.assertIn(".fs/assets/analyses/", save_skill)
        self.assertIn("render-analysis", save_skill)
        template = (skills / "wiki-analysis-save" / "print-template.html").read_text()
        self.assertIn("{{inhalt}}", template)
        self.assertIn('lang="de"', template)
        dockerfile = (ROOT / "opencode" / "Dockerfile").read_text()
        self.assertIn("COPY config/skills /etc/opencode/skills", dockerfile)

    def test_compose_passes_profiles_to_init(self):
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn("COMPOSE_PROFILES: ${COMPOSE_PROFILES:-}", compose)
        self.assertNotIn("PAPERLESS_ENABLED", compose)


if __name__ == "__main__":
    unittest.main()
