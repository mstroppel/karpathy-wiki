import tempfile
import unittest
from pathlib import Path

from karpathy_wiki_ingest.shared import TargetedAnonymizer
from karpathy_wiki_ingest.plugins.webdav import sanitize_once


class WebdavTests(unittest.TestCase):
    def test_sanitizes_text_and_quarantines_binary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incoming = root / "incoming"
            sanitized = root / "sanitized"
            quarantine = root / "quarantine"
            (incoming / "nested").mkdir(parents=True)
            (incoming / "nested/source.txt").write_text("Hallo Max Mustermann", encoding="utf-8")
            (incoming / "private.pdf").write_bytes(b"%PDF\xff")
            anonymizer = TargetedAnonymizer.from_config(
                {"people": [{"replacement": "[ICH]", "values": ["Max Mustermann"]}]}
            )

            self.assertEqual(sanitize_once(incoming, sanitized, quarantine, anonymizer), (1, 1))
            self.assertEqual((sanitized / "nested/source.txt").read_text(), "Hallo [ICH]")
            self.assertFalse((sanitized / "private.pdf").exists())
            self.assertIn("UnicodeDecodeError", (quarantine / "private.pdf.error").read_text())
