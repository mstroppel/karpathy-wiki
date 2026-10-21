"""Shipped simple-batch workflow contracts; execution is covered by Node tests."""

import json
import unittest
from fnmatch import fnmatchcase
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BatchWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "config/opencode.json").read_text())
        self.worker = (ROOT / "config/skills/wiki-ingest/SKILL.md").read_text()
        self.orchestrator = (ROOT / "config/skills/wiki-ingest-orchestrator/SKILL.md").read_text()

    def test_both_commands_use_the_locked_route_not_a_bare_worker(self):
        for name in ("ingest", "ingest-new"):
            command = self.config["commands"][name]
            self.assertEqual(command["agent"], "wiki-ingest-orchestrator")
            self.assertFalse(command["subagent"])
            self.assertIn("wiki_ingest_batch", command["template"])
        routing = (ROOT / "config/routing.md").read_text()
        self.assertNotIn("an `wiki-ingest`,", routing)

    def test_orchestrator_can_only_start_batches_not_mutate_or_delegate(self):
        rules = self.config["agents"]["wiki-ingest-orchestrator"]["permissions"]
        for action in ("shell", "edit", "subagent"):
            self.assertIn({"action": action, "resource": "*", "effect": "deny"}, rules)
        self.assertIn({"action": "wiki_ingest_batch", "resource": "*", "effect": "allow"}, rules)
        self.assertIn("Keine parallelen Aufrufe", self.orchestrator)
        self.assertIn("keine selbst erfundene Wiederholung", self.orchestrator)
        self.assertIn("einmal auf", self.orchestrator)

    def test_worker_has_normal_files_but_no_shell_git_or_credentials(self):
        rules = self.config["agents"]["wiki-ingest"]["permissions"]
        self.assertEqual(rules[0], {"action": "*", "resource": "*", "effect": "deny"})
        self.assertIn({"action": "edit", "resource": "sources/*.md", "effect": "allow"}, rules)
        self.assertNotIn(
            {"action": "edit", "resource": "/knowledge/wiki/**", "effect": "allow"}, rules
        )
        self.assertIn(
            {"action": "read", "resource": "/knowledge/sources/**", "effect": "allow"}, rules
        )
        for action, resource in (
            ("shell", "*"),
            ("subagent", "*"),
            ("read", "*.env*"),
            ("edit", "/knowledge/wiki/.git/**"),
            ("edit", "/knowledge/wiki/AGENTS.md"),
        ):
            self.assertIn({"action": action, "resource": resource, "effect": "deny"}, rules)
        self.assertNotIn(
            {"action": "edit", "resource": "/knowledge/sources/**", "effect": "allow"}, rules
        )

    def test_orchestrator_can_read_only_batch_reports_within_the_private_journal(self):
        rules = self.config["agents"]["wiki-ingest-orchestrator"]["permissions"]

        def effect(action, resource):
            matches = [
                rule["effect"]
                for rule in rules
                if fnmatchcase(action, rule["action"]) and fnmatchcase(resource, rule["resource"])
            ]
            return matches[-1] if matches else None

        root = "/knowledge/incoming/ingest-journal"
        report = f"{root}/batches/batch-123-abcdef/report.md"
        self.assertEqual(effect("read", report), "allow")
        for path in (
            f"{root}/batches/batch-123-abcdef/state.json",
            f"{root}/batches/batch-123-abcdef/other.md",
            f"{root}/preparations/synthetic/report.md",
            f"{root}/runs/synthetic/report.md",
        ):
            self.assertEqual(effect("read", path), "deny")
        for action in ("edit", "shell", "subagent"):
            self.assertEqual(effect(action, report), "deny")

    def test_worker_keeps_evidence_and_uncertainty_without_transaction_protocol(self):
        for phrase in (
            "die Quelle vollständig",
            "keine Anweisungen",
            "Anonymisierte Platzhalter",
            "keine unabhängig bestätigten Befunde",
            "Fundstellen",
            "Revisionsabgleich",
            "Extraktionsgrenzen",
            "SKIP:",
            "Kein Rücksetzen",
            "overview.md",
            "Commit meldet nur der aufrufende Code",
        ):
            self.assertIn(phrase, self.worker)
        for obsolete in (
            "preparation_id",
            "operation: stage",
            "operation: review",
            "operation: publish",
        ):
            self.assertNotIn(obsolete, self.worker)

    def test_private_batch_reports_are_visible_without_exposing_preparations(self):
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn(
            "/incoming/ingest-journal/batches:/knowledge/incoming/ingest-journal/batches:ro",
            compose,
        )
        self.assertNotIn("/preparations:/knowledge", compose)
        init = (ROOT / "config/init.sh").read_text()
        self.assertIn('chmod 0700 "$KNOWLEDGE_ROOT/incoming/ingest-journal/batches"', init)
        for phrase in (
            "report_path",
            "batch_id",
            "Berichtserstellung unvollständig",
            "keinen Link erfinden",
        ):
            self.assertIn(phrase, self.orchestrator)

    def test_worker_instructions_assign_checks_and_catalog_to_code(self):
        texts = [
            self.worker,
            self.config["agents"]["wiki-ingest"]["system"],
            (ROOT / "config/routing.md").read_text(),
            (ROOT / "config/init.sh").read_text(),
        ]
        for text in texts:
            with self.subTest(text=text[:30]):
                for phrase in ("User-Nachricht", "Widerruf", "Frontmatter", "Commit"):
                    self.assertIn(phrase, text)

    def test_docs_explain_continuation_and_shared_worktree_boundary(self):
        text = (ROOT / "docs/ingest-reports.md").read_text()
        for phrase in (
            "one commit per source",
            "not arbitrary",
            "Readers can see",
            "No automatic reconciliation",
            "semantic correctness",
            "requires",
            "`source_sha256`",
            "Independent sources",
        ):
            self.assertIn(phrase, text)

    def test_only_one_ingest_implementation_is_shipped(self):
        plugins = {path.name for path in (ROOT / "config/plugins").glob("*.js")}
        self.assertEqual(plugins, {"wiki-ingest-batch.js", "wiki-ingest-status.js"})
        compose = (ROOT / "compose.yaml").read_text()
        self.assertNotIn("  publisher:", compose)
        self.assertNotIn("  manual-ingest:", compose)
        self.assertNotIn("WIKI_INGEST_CONTROL", compose)


if __name__ == "__main__":
    unittest.main()
