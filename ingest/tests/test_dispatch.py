import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from karpathy_wiki_ingest import __main__ as dispatcher


class DispatchTests(unittest.TestCase):
    def test_unknown_plugin_exits_with_usage_error(self):
        with mock.patch("sys.stderr"):
            with self.assertRaises(SystemExit) as raised:
                dispatcher.main(["karpathy_wiki_ingest", "no-such-plugin"])
        self.assertEqual(raised.exception.code, 2)

    def test_help_lists_builtins(self):
        with mock.patch("sys.stdout") as output:
            self.assertEqual(dispatcher.main(["karpathy_wiki_ingest", "--help"]), 0)
        self.assertTrue(output.write.called)

    def test_default_is_paperless(self):
        with (
            mock.patch("sys.argv", ["karpathy_wiki_ingest", "--once"]),
            mock.patch("karpathy_wiki_ingest.plugins.paperless.main") as plugin,
        ):
            self.assertEqual(dispatcher.main(), 0)
            plugin.assert_called_once()
            self.assertEqual(sys.argv, ["karpathy_wiki_ingest", "--once"])

    def test_external_module_convention(self):
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "karpathy_wiki_ingest_dummy").mkdir()
            Path(directory, "karpathy_wiki_ingest_dummy/__init__.py").write_text(
                "def main():\n    return 'ok'\n"
            )
            sys.path.insert(0, directory)
            try:
                with mock.patch("sys.argv", ["karpathy_wiki_ingest", "dummy"]):
                    self.assertEqual(dispatcher.main(), 0)
            finally:
                sys.path.remove(directory)
                importlib.invalidate_caches()
