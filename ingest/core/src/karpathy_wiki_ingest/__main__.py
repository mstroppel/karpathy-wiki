"""Discover and run ingest plugins.

The core distribution ships no plugins of its own. Plugins are separate
distributions that either register an entry point named after the plugin in
the group ``karpathy_wiki_ingest.plugins`` or expose a package named
``karpathy_wiki_ingest_<plugin>`` with a callable ``main()``.
"""

from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from importlib import metadata

ENTRY_POINT_GROUP = "karpathy_wiki_ingest.plugins"
PLUGIN_NAME = re.compile(r"[a-z0-9_-]+")

USAGE = "usage: karpathy_wiki_ingest PLUGIN [PLUGIN_OPTIONS]"


def installed_plugins() -> list[str]:
    """Names of all plugins registered through entry points."""
    return sorted(
        {entry_point.name for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP)}
    )


def usage_error(plugin: str):
    print(f"Unknown ingest plugin: {plugin}", file=sys.stderr)
    print(USAGE, file=sys.stderr)
    plugins = installed_plugins()
    print(
        "available plugins: " + (", ".join(plugins) if plugins else "(none installed)"),
        file=sys.stderr,
    )
    raise SystemExit(2)


def load_plugin(name: str):
    for entry_point in metadata.entry_points(group=ENTRY_POINT_GROUP):
        if entry_point.name == name:
            return entry_point.load()

    # Third-party plugins can also expose a package named
    # karpathy_wiki_ingest_<name> with a callable main() and no extra metadata.
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
        plugins = installed_plugins()
        print("installed plugins: " + (", ".join(plugins) if plugins else "(none)"))
        return 0
    if arguments and PLUGIN_NAME.fullmatch(arguments[0]) and not arguments[0].startswith("-"):
        name, rest = arguments[0], arguments[1:]
    else:
        # Without an explicit plugin name, run the sole installed plugin. This
        # makes per-plugin images runnable without repeating the plugin name;
        # ambiguous or empty installations require an explicit name.
        plugins = installed_plugins()
        if len(plugins) != 1:
            print(
                "No ingest plugin specified; pass a plugin name or install exactly one plugin",
                file=sys.stderr,
            )
            print(USAGE, file=sys.stderr)
            print(
                "installed plugins: " + (", ".join(plugins) if plugins else "(none)"),
                file=sys.stderr,
            )
            raise SystemExit(2)
        name, rest = plugins[0], arguments
    plugin = load_plugin(name)
    if plugin is None:
        usage_error(name)
    sys.argv = [sys.argv[0], *rest]
    plugin()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
