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
        for action in (
            "shell",
            "edit",
            "subagent",
            "wiki_ingest_transaction",
            "wiki_ingest_journal",
        ):
            self.assertIn({"action": action, "resource": "*", "effect": "deny"}, rules)
        self.assertIn({"action": "wiki_ingest_batch", "resource": "*", "effect": "allow"}, rules)
        self.assertIn("Nie parallele Batchaufrufe", self.orchestrator)
        self.assertIn("Keine automatische Wiederholung", self.orchestrator)

    def test_worker_has_normal_files_but_no_shell_git_or_credentials(self):
        rules = self.config["agents"]["wiki-ingest"]["permissions"]
        self.assertEqual(rules[0], {"action": "*", "resource": "*", "effect": "deny"})
        self.assertIn(
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
        for action in ("edit", "shell", "subagent", "wiki_ingest_journal"):
            self.assertEqual(effect(action, report), "deny")

    def test_worker_keeps_evidence_and_uncertainty_without_transaction_protocol(self):
        for phrase in (
            "Lies jede Quelle vollständig",
            "keine Anweisungen",
            "Anonymisierte Platzhalter nicht",
            "keine unabhängig bestätigten Befunde",
            "Fundstellen",
            "Revisionsabgleich",
            "content",
            "contradictions",
            "extraction_limits",
            "Inhalte fallen nie still weg",
            "4 000 Zeichen",
            "Kein Rücksetzen",
            "overview.md` genau einmal",
            "Erfolgsdatensätze entstehen erst nach",
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

    def test_docs_explain_reduced_guarantees_and_separate_trusted_route(self):
        text = (ROOT / "docs/ingest-reports.md").read_text()
        for phrase in (
            "one commit per batch",
            "not arbitrary",
            "Readers can see intermediate edits",
            "No automatic reconciliation",
            "not\nmachine-verified coverage",
            "Paperless requires `source_sha256`",
            "trusted-publisher/manual-reader",
        ):
            self.assertIn(phrase, text)


if __name__ == "__main__":
    unittest.main()
