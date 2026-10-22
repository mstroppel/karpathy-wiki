"""Exercise both providers' filters with the real rclone matching engine.

The Compose suite runs these tests with the pinned runtime binary. The local
backend stands in for WebDAV so only synthetic disposable files are touched.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from karpathy_wiki_ingest_audio.connector import synchronize as sync_audio
from karpathy_wiki_ingest_webdav import synchronize as sync_webdav
from karpathy_wiki_speech.types import DEFAULT_LIMITS, TranscriptionLimits


@unittest.skipUnless(shutil.which("rclone"), "requires the runtime rclone binary")
class RcloneSyncTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.upstream = self.root / "upstream"
        self.incoming = self.root / "incoming"
        self.upstream.mkdir()
        self.incoming.mkdir()
        environment = patch.dict(
            os.environ,
            {"RCLONE_CONFIG_WEBDAV_TYPE": "local", "RCLONE_CONFIG": os.devnull},
        )
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, root, relative, content="synthetic fixture"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def files(self):
        return {
            path.relative_to(self.incoming).as_posix()
            for path in self.incoming.rglob("*")
            if path.is_file()
        }

    def exercise(self, synchronize, suffixes):
        expected = set()
        for suffix in suffixes:
            for relative in (
                f"root{suffix}",
                f"deep/Folder/source{suffix.upper()}",
                f"deep/mixed{suffix.title()}",
            ):
                self.write(self.upstream, relative)
                expected.add(relative)
        for relative in (
            "large.bin",
            "deep/notes.pdf",
            "deep/file",
            "deep/file.md.bak",
            "old.xls",
            "macro.xlsm",
            "binary.xlsb",
            "deep/foreign.zzz",
        ):
            self.write(self.upstream, relative)
        self.write(self.incoming, "keep-local.bin")
        self.write(self.incoming, f"deleted{suffixes[0]}")
        synchronize(self.incoming, str(self.upstream))
        self.assertEqual(self.files(), expected | {"keep-local.bin"})
        changed = sorted(expected)[0]
        self.write(self.upstream, changed, "longer updated synthetic fixture")
        removed = sorted(expected)[-1]
        (self.upstream / removed).unlink()
        synchronize(self.incoming, str(self.upstream))
        self.assertEqual(self.files(), (expected - {removed}) | {"keep-local.bin"})
        self.assertEqual((self.incoming / changed).read_text(), "longer updated synthetic fixture")

    def test_webdav_supported_formats(self):
        self.exercise(sync_webdav, (".md", ".html", ".htm", ".xlsx"))

    def test_audio_supported_formats(self):
        self.write(self.upstream, "deep/notes.md")
        self.write(self.upstream, "deep/workbook.xlsx")
        self.exercise(sync_audio, DEFAULT_LIMITS.allowed_extensions)

    def test_audio_uses_discovery_limits(self):
        self.write(self.upstream, "allowed.WaV")
        self.write(self.upstream, "excluded.mp3")
        sync_audio(
            self.incoming, str(self.upstream), limits=TranscriptionLimits(100, 60, (".wav",))
        )
        self.assertEqual(self.files(), {"allowed.WaV"})

    def test_empty_audio_allowlist_downloads_nothing(self):
        self.write(self.upstream, "excluded.wav")
        sync_audio(self.incoming, str(self.upstream), limits=TranscriptionLimits(100, 60, ()))
        self.assertEqual(self.files(), set())
