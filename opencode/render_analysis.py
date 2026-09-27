#!/usr/bin/env python3
"""Render a saved wiki analysis to its self-contained print view."""

import argparse
import html
import re
from datetime import date
from pathlib import Path
from urllib.parse import quote, urlsplit
from xml.etree.ElementTree import Element

import bleach
import markdown
from markdown.extensions import Extension
from markdown.inlinepatterns import InlineProcessor

TEMPLATE = Path("/etc/opencode/skills/wiki-analysis-save/print-template.html")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
DATE = re.compile(r"Erstellt: (\d{4}-\d{2}-\d{2})\Z")
WIKILINK = r"(?<!!)\[\[([^\]\n|]+)(?:\|([^\]\n]+))?\]\]"
TAGS = (
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "hr",
    "li",
    "ol",
    "p",
    "pre",
    "strong",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "tr",
    "ul",
)


class WikiLink(InlineProcessor):
    def __init__(self, base_url: str, md: markdown.Markdown):
        super().__init__(WIKILINK, md)
        self.base_url = base_url

    def handleMatch(self, match, data):  # noqa: N802 (Python-Markdown API)
        target = match.group(1).strip().removesuffix(".md")
        parts = target.split("/")
        if not all(SLUG.fullmatch(part) for part in parts):
            return None, match.start(0), match.end(0)
        link = Element("a")
        link.set("href", self.base_url + "/" + "/".join(quote(part) for part in parts))
        link.text = (match.group(2) or target).strip()
        return link, match.start(0), match.end(0)


class WikiLinks(Extension):
    def __init__(self, base_url: str):
        self.base_url = base_url
        super().__init__()

    def extendMarkdown(self, md):  # noqa: N802 (Python-Markdown API)
        md.inlinePatterns.register(WikiLink(self.base_url, md), "wikilink", 175)


def render(source: str, template: str, wiki_name: str, base_url: str, slug: str) -> str:
    lines = source.splitlines()
    if len(lines) < 2 or not lines[0].startswith("# ") or not (title := lines[0][2:].strip()):
        raise ValueError("analysis must start with '# <title>'")
    date_match = DATE.fullmatch(lines[1])
    if not date_match:
        raise ValueError("analysis must have 'Erstellt: YYYY-MM-DD' on line 2")
    try:
        date.fromisoformat(date_match.group(1))
    except ValueError as exc:
        raise ValueError("analysis creation date must be valid") from exc
    parsed = urlsplit(base_url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("wiki URL must be a public HTTP(S) base URL")
    base_url = base_url.rstrip("/")
    page_url = base_url + "/analyses/" + slug
    body = markdown.markdown(
        "\n".join(lines[2:]),
        extensions=["fenced_code", "tables", "sane_lists", WikiLinks(base_url)],
    )
    body = bleach.clean(
        body,
        tags=TAGS,
        attributes={"a": ["href", "title"]},
        protocols=["http", "https", "mailto"],
        strip=True,
    )
    values = {
        "titel": html.escape(title, quote=True),
        "datum": html.escape(date_match.group(1), quote=True),
        "seiten_url": html.escape(page_url, quote=True),
        "wiki_name": html.escape(wiki_name, quote=True),
        "inhalt": body,
    }
    return re.sub(
        r"\{\{(titel|datum|seiten_url|wiki_name|inhalt)\}\}", lambda m: values[m.group(1)], template
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="analyses/<slug>.md in the wiki checkout")
    parser.add_argument("--wiki-url", required=True, help="public wiki base URL")
    args = parser.parse_args()
    source = Path(args.source)
    if (
        source.parent != Path("analyses")
        or source.suffix != ".md"
        or not SLUG.fullmatch(source.stem)
    ):
        parser.error("source must be analyses/<ascii-kebab-slug>.md")
    wiki_name = (
        Path("AGENTS.md").read_text(encoding="utf-8").splitlines()[0].removeprefix("# ").strip()
    )
    if not wiki_name:
        parser.error("AGENTS.md must start with a wiki name")
    try:
        output = render(
            source.read_text(encoding="utf-8"),
            TEMPLATE.read_text(encoding="utf-8"),
            wiki_name,
            args.wiki_url,
            source.stem,
        )
    except ValueError as exc:
        parser.error(str(exc))
    destination = Path("assets/analyses") / (source.stem + ".html")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(output, encoding="utf-8")
    print(destination)


if __name__ == "__main__":
    main()
