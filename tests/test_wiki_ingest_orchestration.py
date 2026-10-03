"""Validate the bounded-context ingestion orchestration contract.

Bulk ingestion (issue #152) is orchestrated by a narrow orchestrator agent that
plans batches against a working-context budget, delegates every wiki write to
``wiki-ingest`` workers, records durable per-source results in a private
journal, and reports the complete per-source details as a file. These tests pin
that contract in the skills, the command, the permissions, and the deployment
files, in the style of ``tests/test_review_skill.py``: the workflow itself runs
against a real model and is deliberately not asserted here.
"""

import json
import re
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_SKILL = ROOT / "config" / "skills" / "wiki-ingest-orchestrator" / "SKILL.md"
WORKER_SKILL = ROOT / "config" / "skills" / "wiki-ingest" / "SKILL.md"
CONFIG = ROOT / "config" / "opencode.json"
COMPOSE = ROOT / "compose.yaml"
INIT = ROOT / "config" / "init.sh"
REPORTS_DOC = ROOT / "docs" / "ingest-reports.md"

# The orchestrator's non-negotiable boundaries: it never touches sources or the
# wiki itself, workers run strictly sequentially, global findings stop the run,
# oversized sources get staged reading or a named blocker, and the report path
# plus statuses are the only report content in chat.
ORCHESTRATOR_PHRASES = (
    "Quellen nicht selbst ein",
    "wiki-ingest",
    "wiki_ingest_journal",
    "run_start",
    "next_batch",
    "run_finish",
    "nie zwei Worker gleichzeitig",
    "invalid",
    "conflict",
    "stückweises, validiertes Lesen",
    "rollover",
    "Berichtspfad",
    "erfinde keine Details",
)

# The worker's evidence contract: one durable record per source, written only
# after the verified commit, unverified results blocked instead of softened,
# and no silent truncation anywhere.
WORKER_PHRASES = (
    "Ergebnisdatensatz",
    "wiki_ingest_journal",
    "status: blocked",
    "entsteht erst nach dem Commit und nie davor",
    "Inhalte fallen nie still weg",
    "Quelldatei unverändert bestätigen",
    "committe genau einmal pro Quelle",
)

# The agreed report contract for bulk runs: complete per-source details live in
# the report file, the final answer carries status, unfinished sources, and the
# report path.
REPORT_CONTRACT_PHRASES = (
    "Contract change for bulk runs",
    "WIKI_INGEST_BATCH_BUDGET_TOKENS",
    "WIKI_INGEST_BATCH_MAX_SOURCES",
    "WIKI_INGEST_RUN_MAX_BATCHES",
    "not measured model tokens",
)


class SkillContractTests(unittest.TestCase):
    def test_orchestrator_skill_states_the_orchestration_boundaries(self):
        text = ORCHESTRATOR_SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"), "skill must start with YAML frontmatter")
        self.assertIn("name: wiki-ingest-orchestrator\n", text)
        for phrase in ORCHESTRATOR_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_worker_skill_states_the_evidence_contract(self):
        text = WORKER_SKILL.read_text(encoding="utf-8")
        for phrase in WORKER_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


class CommandAndAgentTests(unittest.TestCase):
    config: dict[str, Any] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads(CONFIG.read_text(encoding="utf-8"))

    def test_ingest_new_runs_the_orchestrator_in_the_current_session(self):
        command = self.config["commands"]["ingest-new"]
        self.assertEqual(command["agent"], "wiki-ingest-orchestrator")
        self.assertIs(command["subagent"], False)
        self.assertIn("wiki-ingest-orchestrator", command["template"])
        self.assertIn("Bericht", command["template"])

    def test_orchestrator_agent_cannot_write_or_escape(self):
        agent = self.config["agents"]["wiki-ingest-orchestrator"]
        effects: dict[str, list[str]] = {}
        for rule in agent["permissions"]:
            effects.setdefault(rule["action"], []).append(rule["effect"])
        self.assertEqual(effects["edit"], ["deny"])
        self.assertEqual(effects["shell"], ["deny"])
        self.assertEqual(effects["webfetch"], ["deny"])
        self.assertEqual(effects["websearch"], ["deny"])
        self.assertEqual(effects["subagent"], ["deny", "allow"])
        allowed = [
            rule["resource"]
            for rule in agent["permissions"]
            if rule["action"] == "subagent" and rule["effect"] == "allow"
        ]
        self.assertEqual(allowed, ["wiki-ingest"])

    def test_journal_tool_is_allowed_and_private(self):
        rules = self.config["permissions"]
        journal_allows = [
            rule
            for rule in rules
            if rule["action"] == "wiki_ingest_journal" and rule["effect"] == "allow"
        ]
        self.assertTrue(journal_allows, "journal tool must be allowed for workers")
        external = [
            rule["resource"]
            for rule in rules
            if rule["action"] == "external_directory" and rule["effect"] == "allow"
        ]
        self.assertIn("/knowledge/incoming/ingest-journal/**", external)
        for name in ("wiki-analysis", "wiki-analysis-save"):
            with self.subTest(agent=name):
                denies = [
                    rule["effect"]
                    for rule in self.config["agents"][name]["permissions"]
                    if rule["action"] == "wiki_ingest_journal"
                ]
                self.assertEqual(denies, ["deny"], f"{name} must not write journal records")


class DeploymentTests(unittest.TestCase):
    def test_compose_mounts_the_private_journal_and_passes_the_budget(self):
        text = COMPOSE.read_text(encoding="utf-8")
        self.assertIn("incoming/ingest-journal:/knowledge/incoming/ingest-journal:rw", text)
        self.assertNotIn("state:/knowledge/state:rw", text)
        self.assertNotIn("ingest-journal:/knowledge/state", text)
        for variable in (
            "WIKI_INGEST_BATCH_BUDGET_TOKENS",
            "WIKI_INGEST_BATCH_MAX_SOURCES",
            "WIKI_INGEST_RUN_MAX_BATCHES",
        ):
            with self.subTest(variable=variable):
                self.assertIn(f"{variable}: ${{{variable}", text)

    def test_init_creates_the_private_journal_directory(self):
        text = INIT.read_text(encoding="utf-8")
        self.assertIn('"$KNOWLEDGE_ROOT/incoming/ingest-journal"', text)
        self.assertRegex(
            text, re.compile(r"chmod 0700 \"\$KNOWLEDGE_ROOT/incoming/ingest-journal\"")
        )

    def test_reports_document_the_agreed_contract_and_budget(self):
        text = REPORTS_DOC.read_text(encoding="utf-8")
        for phrase in REPORT_CONTRACT_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("docs/ingest-reports.md", readme)


if __name__ == "__main__":
    unittest.main()
