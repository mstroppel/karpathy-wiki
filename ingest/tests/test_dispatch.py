import importlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from karpathy_wiki_ingest import __main__ as dispatcher

ENTRY_POINT_GROUP = "karpathy_wiki_ingest.plugins"


def entry_point(name):
    entry = mock.Mock()
    entry.name = name
    entry.load.return_value = mock.Mock()
    return entry


def no_plugins():
    return set()


class DispatchTests(unittest.TestCase):
    def test_unknown_plugin_exits_with_usage_error(self):
        with (
            mock.patch("sys.stderr"),
            mock.patch(f"{dispatcher.__name__}.metadata.entry_points", return_value=[]),
            mock.patch(f"{dispatcher.__name__}.convention_plugins", new=no_plugins),
        ):
            with self.assertRaises(SystemExit) as raised:
                dispatcher.main(["karpathy_wiki_ingest", "no-such-plugin"])
        self.assertEqual(raised.exception.code, 2)

    def test_help_lists_installed_plugins(self):
        plugins = [entry_point("paperless"), entry_point("webdav")]
        with (
            mock.patch("sys.stdout") as output,
            mock.patch(
                f"{dispatcher.__name__}.metadata.entry_points", return_value=plugins
            ) as entry_points,
            mock.patch(f"{dispatcher.__name__}.convention_plugins", new=no_plugins),
        ):
            self.assertEqual(dispatcher.main(["karpathy_wiki_ingest", "--help"]), 0)
        entry_points.assert_called_with(group=ENTRY_POINT_GROUP)
        self.assertTrue(output.write.called)

    def test_runs_the_requested_entry_point_plugin(self):
        plugin = entry_point("paperless")
        with (
            mock.patch("sys.argv", ["karpathy_wiki_ingest", "paperless", "--once"]),
            mock.patch(f"{dispatcher.__name__}.metadata.entry_points", return_value=[plugin]),
        ):
            self.assertEqual(dispatcher.main(), 0)
            plugin.load.return_value.assert_called_once()
            self.assertEqual(sys.argv, ["karpathy_wiki_ingest", "--once"])

    def test_defaults_to_the_sole_installed_plugin(self):
        plugin = entry_point("webdav")
        with (
            mock.patch("sys.argv", ["karpathy_wiki_ingest", "--once"]),
            mock.patch(f"{dispatcher.__name__}.metadata.entry_points", return_value=[plugin]),
            mock.patch(f"{dispatcher.__name__}.convention_plugins", new=no_plugins),
        ):
            self.assertEqual(dispatcher.main(), 0)
            plugin.load.return_value.assert_called_once()
            self.assertEqual(sys.argv, ["karpathy_wiki_ingest", "--once"])

    def test_ambiguous_installation_requires_a_plugin_name(self):
        plugins = [entry_point("paperless"), entry_point("webdav")]
        with (
            mock.patch("sys.stderr"),
            mock.patch("sys.argv", ["karpathy_wiki_ingest", "--once"]),
            mock.patch(f"{dispatcher.__name__}.metadata.entry_points", return_value=plugins),
            mock.patch(f"{dispatcher.__name__}.convention_plugins", new=no_plugins),
        ):
            with self.assertRaises(SystemExit) as raised:
                dispatcher.main()
        self.assertEqual(raised.exception.code, 2)

    def test_external_module_convention(self):
        # This exercises the documented third-party discovery path itself
        # (a package named karpathy_wiki_ingest_<name> dropped on sys.path);
        # the repository packages are imported normally, without path tricks.
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

    def test_defaults_to_the_sole_convention_plugin(self):
        # Implicit single-plugin dispatch also works for convention packages
        # that register no entry point. The convention set is pinned so the
        # development environment's own installed plugins cannot make the run
        # ambiguous; the tempdir package supplies the load path.
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, "karpathy_wiki_ingest_solo").mkdir()
            Path(directory, "karpathy_wiki_ingest_solo/__init__.py").write_text(
                "def main():\n    return 'ok'\n"
            )
            sys.path.insert(0, directory)
            try:
                importlib.invalidate_caches()
                with (
                    mock.patch("sys.argv", ["karpathy_wiki_ingest", "--once"]),
                    mock.patch(f"{dispatcher.__name__}.metadata.entry_points", return_value=[]),
                    mock.patch(f"{dispatcher.__name__}.convention_plugins", return_value={"solo"}),
                ):
                    self.assertEqual(dispatcher.main(), 0)
                    self.assertEqual(sys.argv, ["karpathy_wiki_ingest", "--once"])
            finally:
                sys.path.remove(directory)
                importlib.invalidate_caches()

    def test_convention_and_entry_point_plugins_are_listed_together(self):
        with (
            mock.patch(f"{dispatcher.__name__}.entry_point_plugins", return_value={"paperless"}),
            mock.patch(f"{dispatcher.__name__}.convention_plugins", return_value={"solo"}),
        ):
            self.assertEqual(dispatcher.installed_plugins(), ["paperless", "solo"])
