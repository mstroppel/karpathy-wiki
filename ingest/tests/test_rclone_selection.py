"""Discovery rules must not depend on Python's changing dotfile suffix rules."""

import tempfile
import unittest
from pathlib import Path

from karpathy_wiki_ingest_audio.connector import audio_inventory
from karpathy_wiki_ingest_webdav import source_files
from karpathy_wiki_speech.types import DEFAULT_LIMITS


class SuffixSelectionTests(unittest.TestCase):
    def test_local_dot_only_stems_are_ignored_by_both_providers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            expected = set()
            for suffix in (".md", ".html", ".htm", ".xlsx", ".xls", ".mp3", ".wav"):
                for stem in ("", ".", "..", "...", "a", ".hidden", "...name.."):
                    relative = Path("nested") / f"{stem}{suffix.upper()}"
                    path = root / relative
                    path.parent.mkdir(exist_ok=True)
                    path.write_text("synthetic fixture")
                    if stem.strip("."):
                        expected.add(relative)
            self.assertEqual(
                source_files(root),
                {path for path in expected if path.suffix.lower() not in {".mp3", ".wav"}},
            )
            self.assertEqual(
                set(audio_inventory(root, DEFAULT_LIMITS)),
                {path.as_posix() for path in expected if path.suffix.lower() in {".mp3", ".wav"}},
            )
