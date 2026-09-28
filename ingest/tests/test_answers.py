"""Answer inbox publication and source-tracking boundary."""

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from karpathy_wiki_ingest.answers import publish
from karpathy_wiki_ingest.manifest import validate_manifest


class AnswerPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.inbox = root / "incoming"
        self.inbox.mkdir()
        self.sources = root / "sources"
        self.redactions = root / "redactions.json"
        self.redactions.write_text(
            json.dumps({"people": [{"values": ["Ada Lovelace"], "replacement": "[PERSON]"}]})
        )

    def manifest(self):
        return validate_manifest(json.loads((self.sources / "manifest.json").read_text()))

    def test_confirmed_draft_is_redacted_and_tracked_idempotently(self):
        (self.inbox / "review-1.md").write_text(
            "# Antworten\n\n1. Ada Lovelace bestätigt es.\n\n<!-- END CONFIRMED ANSWERS -->"
        )
        self.assertEqual(publish(self.inbox, self.sources, self.redactions), 1)
        item = self.manifest()["items"][0]
        self.assertEqual(item["source_key"], "review-1.md")
        self.assertEqual(item["wiki_path"], "answers/review-1/index.md")
        self.assertEqual(self.manifest()["wiki_root"], "answers")
        self.assertEqual(stat.S_IMODE((self.sources / "manifest.json").stat().st_mode), 0o640)
        self.assertEqual(stat.S_IMODE((self.sources / item["source_path"]).stat().st_mode), 0o640)
        source = (self.sources / item["source_path"]).read_text()
        self.assertIn("[PERSON]", source)
        self.assertNotIn("Ada Lovelace", source)
        self.assertEqual(publish(self.inbox, self.sources, self.redactions), 1)
        self.assertEqual(self.manifest()["items"][0]["source_revision"], item["source_revision"])

        (self.inbox / "review-1.md").write_text(
            "# Antworten\n\n1. Andere Antwort.\n\n<!-- END CONFIRMED ANSWERS -->"
        )
        publish(self.inbox, self.sources, self.redactions)
        updated = self.manifest()["items"][0]
        self.assertNotEqual(updated["source_revision"], item["source_revision"])
        self.assertFalse((self.sources / item["source_path"]).exists())

    def test_rejected_draft_and_missing_draft_keep_last_publication(self):
        draft = self.inbox / "review-1.md"
        draft.write_text("Bestätigte Antwort\n<!-- END CONFIRMED ANSWERS -->")
        publish(self.inbox, self.sources, self.redactions)
        previous = (self.sources / "manifest.json").read_bytes()
        (self.inbox / "bad.md").write_bytes(b"\xff")
        with self.assertRaises(UnicodeError):
            publish(self.inbox, self.sources, self.redactions)
        self.assertEqual(previous, (self.sources / "manifest.json").read_bytes())
        self.assertEqual(len(list((self.sources / "revisions").iterdir())), 1)
        (self.inbox / "bad.md").unlink()
        draft.unlink()
        with self.assertRaisesRegex(ValueError, "missing"):
            publish(self.inbox, self.sources, self.redactions)
        self.assertEqual(previous, (self.sources / "manifest.json").read_bytes())

    def test_symlinks_are_rejected_and_redaction_changes_republish(self):
        (self.inbox / "review-1.md").write_text("Ada Lovelace\n<!-- END CONFIRMED ANSWERS -->")
        publish(self.inbox, self.sources, self.redactions)
        previous = self.manifest()["items"][0]["source_revision"]
        self.redactions.write_text(
            json.dumps({"people": [{"values": ["Ada Lovelace"], "replacement": "[AUTHOR]"}]})
        )
        publish(self.inbox, self.sources, self.redactions)
        self.assertNotEqual(self.manifest()["items"][0]["source_revision"], previous)
        self.assertEqual(len(list((self.sources / "revisions").iterdir())), 1)
        (self.inbox / "review-2.md").symlink_to(self.inbox / "review-1.md")
        with self.assertRaisesRegex(ValueError, "symlink"):
            publish(self.inbox, self.sources, self.redactions)

    def test_partial_draft_is_not_published(self):
        (self.inbox / "review-1.md").write_text("1. unfinished")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            publish(self.inbox, self.sources, self.redactions)
        self.assertFalse((self.sources / "manifest.json").exists())

    def test_later_invalid_draft_does_not_expose_earlier_valid_revision(self):
        (self.inbox / "b.md").write_text("Private reply\n<!-- END CONFIRMED ANSWERS -->")
        (self.inbox / "z.md").write_text("Unfinished reply")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            publish(self.inbox, self.sources, self.redactions)
        self.assertEqual(list((self.sources / "revisions").iterdir()), [])
        self.assertFalse((self.sources / "manifest.json").exists())

    def test_manifest_write_failure_rolls_back_new_revision(self):
        draft = self.inbox / "review-1.md"
        draft.write_text("First reply\n<!-- END CONFIRMED ANSWERS -->")
        publish(self.inbox, self.sources, self.redactions)
        previous = self.manifest()["items"][0]
        draft.write_text("Revised reply\n<!-- END CONFIRMED ANSWERS -->")
        with patch("karpathy_wiki_ingest.answers.write_manifest", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                publish(self.inbox, self.sources, self.redactions)
        self.assertEqual(self.manifest()["items"][0], previous)
        self.assertEqual(len(list((self.sources / "revisions").iterdir())), 1)


if __name__ == "__main__":
    unittest.main()
