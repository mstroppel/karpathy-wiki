"""Discover and run ingest plugins."""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from importlib import metadata

BUILTIN_PLUGINS = ("webdav", "paperless")
ENTRY_POINT_GROUP = "karpathy_wiki_ingest.plugins"
PLUGIN_NAME = re.compile(r"[a-z0-9_-]+")

USAGE = "usage: karpathy_wiki_ingest PLUGIN [PLUGIN_OPTIONS]"
PLUGINS_HINT = "available built-in plugins: " + ", ".join(BUILTIN_PLUGINS)


def usage_error(plugin: str):
    print(f"Unknown ingest plugin: {plugin}", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    print(PLUGINS_HINT, file=sys.stderr)
    raise SystemExit(2)


def load_plugin(name: str):
    if name in BUILTIN_PLUGINS:
        module = importlib.import_module(f".plugins.{name}", package="karpathy_wiki_ingest")
        return getattr(module, "main")

    # Third-party plugins can either register an entry point pointing at
    # their main() callable or expose a package named karpathy_wiki_ingest_<name>.
    for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP):
        if entry_point.name == name:
            return entry_point.load()

    if importlib.util.find_spec(f"karpathy_wiki_ingest_{name}") is not None:
        module = importlib.import_module(f"karpathy_wiki_ingest_{name}")
        main_function = getattr(module, "main", None)
        if callable(main_function):
            return main_function
        raise SystemExit(f"{module.__name__} must expose a callable main()")
    return None


def main(argv=None):
    arguments = list(sys.argv[1:] if argv is None else argv[1:])
    if arguments and arguments[0] in ("-h", "--help"):
        print(__doc__.strip())
        print(USAGE)
        print(PLUGINS_HINT)
        return 0
    if arguments and PLUGIN_NAME.fullmatch(arguments[0]) and not arguments[0].startswith("-"):
        name, rest = arguments[0], arguments[1:]
    else:
        name, rest = "paperless", arguments
    plugin = load_plugin(name)
    if plugin is None:
        usage_error(name)
    sys.argv = [sys.argv[0], *rest]
    plugin()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
