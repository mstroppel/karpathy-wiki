"""Pin the confirmed-answer workflow; model execution is not asserted here."""

import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "config/skills/wiki-gap-review/SKILL.md"


class GapReviewContractTests(unittest.TestCase):
    def test_confirmation_precedes_local_save_and_publication_precedes_import(self):
        text = SKILL.read_text()
        steps = (
            "ausdrückliche Einreichungsbestätigung",
            "1. **Lokal speichern.**",
            "2. **Veröffentlichung prüfen.**",
            "3. **Konkrete Quelle importieren.**",
            "4. **Übernahme belegen.**",
        )
        positions = [text.index(step) for step in steps]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("Eingangsdatei unabhängig vom Dienststatus", text)
        self.assertNotIn("Nur mit laufendem", text)
        for phrase in (
            "<!-- END CONFIRMED ANSWERS -->",
            "vorhandene Entwürfe weder lesen noch ändern oder löschen",
            "inhaltliche Änderungen brauchen eine neue Vorschau und Bestätigung",
            "Bei Schreibfehler: genaue Fehlermeldung nennen",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_resume_targets_the_existing_draft_and_handoff_includes_source_identity(self):
        text = SKILL.read_text()
        for phrase in (
            "source_key: <name>.md",
            "wait_seconds: 60",
            "denselben Dateinamen erneut",
            "erneut geschrieben",
            "Andere aktuelle Antwortquellen belegen keinen Erfolg",
            "`invalid`, `conflict` oder `revoked`",
            "`source_key`, `source_revision`",
            "`source_path`",
            "ausschließlich diese Quelle selbst vollständig",
            "`current` für die übergebene Revision",
            "Ohne Commitbeleg",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_follow_up_keeps_primary_session_and_narrow_permissions(self):
        routing = (ROOT / "config/routing.md").read_text()
        self.assertIn("„Speichere/ingeste die Antworten“", routing)
        self.assertIn("diese Regel hat Vorrang vor", routing)
        config = json.loads((ROOT / "config/opencode.json").read_text())
        command = config["commands"]["gap-review"]
        self.assertEqual(command["agent"], "build")
        self.assertIs(command["subagent"], False)
        allowed = [
            rule["resource"]
            for rule in config["permissions"]
            if rule["action"] == "external_directory" and rule["effect"] == "allow"
        ]
        self.assertIn("/knowledge/incoming/answers/**", allowed)
        self.assertNotIn("/knowledge/**", allowed)


if __name__ == "__main__":
    unittest.main()
