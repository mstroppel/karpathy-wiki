"""Redact HTML using decoded views projected onto the untouched source.

No rendering, resource fetching, or HTML serialization takes place. This is
deny-list redaction, not an HTML sanitizer or a browser/JavaScript interpreter.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from html.entities import html5
from html.parser import HTMLParser

from karpathy_wiki_ingest.shared import PrivacyValidationError, TargetedAnonymizer

ENTITY = re.compile(r"&(?:#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\f <&#;]{1,32};?)")
ATTRIBUTE = re.compile(r"""[^\s/=>]+(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?""")
BLOCKS = frozenset(
    "address article aside blockquote br dd div dl dt fieldset figcaption figure "
    "footer form h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section table "
    "tbody td th thead tr ul script style title".split()
)
ATTRIBUTE_CONTEXT = 1
RAW_TEXT_CONTEXT = 2


def decoded_view(
    source: str, offset: int = 0, contexts: bytearray | None = None
) -> tuple[str, list[tuple[int, int]]]:
    """Map each decoded character back to its literal character or whole entity."""
    characters: list[str] = []
    spans: list[tuple[int, int]] = []
    position = 0
    while position < len(source):
        context = contexts[offset + position] if contexts is not None else 0
        entity = ENTITY.match(source, position) if context != RAW_TEXT_CONTEXT else None
        end = position + 1
        value = source[position]
        if entity:
            token = entity.group()
            if token.startswith("&#"):
                end = entity.end()
                value = html.unescape(token)
            else:
                # HTML permits a subset of named references without semicolons,
                # including references followed by ordinary literal characters.
                for length in range(len(token) - 1, 0, -1):
                    name = token[1 : length + 1]
                    if name in html5:
                        following = source[position + length + 1 : position + length + 2]
                        if (
                            context == ATTRIBUTE_CONTEXT
                            and not name.endswith(";")
                            and following
                            and (following in "=" or following.isascii() and following.isalnum())
                        ):
                            # In attributes, an ambiguous ampersand is literal,
                            # unlike the same no-semicolon reference in text.
                            break
                        end = position + length + 1
                        value = html5[name]
                        break
        characters.extend(value)
        spans.extend([(offset + position, offset + end)] * len(value))
        position = end
    return "".join(characters), spans


class SourceViews(HTMLParser):
    """Collect text across inline markup and protect structural tag syntax."""

    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=False)
        self.source = source
        self.lines = [0]
        self.lines.extend(match.end() for match in re.finditer("\n", source))
        self.text: list[str] = []
        self.spans: list[tuple[int, int]] = []
        self.protected = bytearray(len(source))
        self.contexts = bytearray(len(source))
        self.in_raw_text = False
        self.feed(source)
        self.close()

    def source_offset(self) -> int:
        line, column = self.getpos()
        return self.lines[line - 1] + column

    def boundary(self, tag: str) -> None:
        if tag in BLOCKS:
            self.text.append("\n")
            self.spans.append((0, 0))

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.boundary(tag)
        if tag in {"script", "style"}:
            self.in_raw_text = True
        raw = self.get_starttag_text()
        assert raw is not None
        start = self.source_offset()
        self.protected[start : start + len(raw)] = b"\1" * len(raw)
        name = re.match(r"<[^\s/>]+", raw)
        assert name is not None
        for attribute in ATTRIBUTE.finditer(raw, name.end()):
            for group in (1, 2, 3):
                if attribute.start(group) >= 0:
                    left, right = attribute.span(group)
                    self.protected[start + left : start + right] = b"\0" * (right - left)
                    self.contexts[start + left : start + right] = bytes([ATTRIBUTE_CONTEXT]) * (
                        right - left
                    )

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:
        self.boundary(tag)
        if tag in {"script", "style"}:
            self.in_raw_text = False
        start = self.source_offset()
        end = self.source.find(">", start) + 1
        self.protected[start:end] = b"\1" * (end - start)

    def handle_data(self, data: str) -> None:
        start = self.source_offset()
        if self.in_raw_text:
            self.contexts[start : start + len(data)] = bytes([RAW_TEXT_CONTEXT]) * len(data)
        text, spans = decoded_view(data, start, self.contexts)
        self.text.append(text)
        self.spans.extend(spans)

    def handle_entityref(self, name: str) -> None:
        start = self.source_offset()
        end = start + len(name) + 1
        if self.source[end : end + 1] == ";":
            end += 1
        text, spans = decoded_view(self.source[start:end], start)
        self.text.append(text)
        self.spans.extend(spans)

    def handle_charref(self, name: str) -> None:
        self.handle_entityref("#" + name)

    def handle_decl(self, decl: str) -> None:
        self.protect_declaration()

    def handle_pi(self, data: str) -> None:
        self.protect_declaration()

    def unknown_decl(self, data: str) -> None:
        self.protect_declaration()

    def protect_declaration(self) -> None:
        start = self.source_offset()
        end = self.source.find(">", start) + 1
        self.protected[start:end] = b"\1" * (end - start)

    def handle_comment(self, data: str) -> None:
        start = self.source_offset()
        self.protected[start : start + 4] = b"\1" * 4
        end = self.source.find(">", start + 4 + len(data)) + 1
        left = start + 4 + len(data)
        self.protected[left:end] = b"\1" * (end - left)


def matching_views(source: str) -> tuple[SourceViews, list[tuple[str, list[tuple[int, int]]]]]:
    try:
        parsed = SourceViews(source)
    except AssertionError:
        # HTMLParser can reject malformed marked declarations. Do not leak
        # parser messages (which may contain source text) or kill the daemon.
        raise PrivacyValidationError("HTML source could not be parsed") from None
    return parsed, [
        ("".join(parsed.text), parsed.spans),
        decoded_view(source, contexts=parsed.contexts),
        (source, [(position, position + 1) for position in range(len(source))]),
    ]


@dataclass(frozen=True)
class ProjectedMatch:
    positions: tuple[int, ...]
    replacement: str
    priority: int


def redact_html(source: str, anonymizer: TargetedAnonymizer) -> str:
    """Replace matched source spans, keeping intervening tags and all other bytes.

    Raw decoded source also covers attributes, comments, and script/style data.
    Text-node matching catches names split across inline tags. A match touching
    tag syntax is rejected rather than damaging the document structure.
    """
    parsed, views = matching_views(source)
    candidates: list[ProjectedMatch] = []
    for text, spans in views:
        for match in anonymizer.locate(text):
            positions = tuple(
                sorted(
                    {
                        position
                        for left, right in spans[match.start : match.end]
                        for position in range(left, right)
                    }
                )
            )
            if positions:
                candidates.append(ProjectedMatch(positions, match.replacement, match.priority))
    replacements: dict[int, str] = {}
    removed: set[int] = set()
    # Preserve locate()'s configured rule precedence across every view, not
    # just within each view. Equal-priority candidates scan left-to-right;
    # at the same source start prefer the most complete projected match.
    for candidate in sorted(
        candidates,
        key=lambda candidate: (
            candidate.priority,
            candidate.positions[0],
            -len(candidate.positions),
        ),
    ):
        if removed.intersection(candidate.positions):
            continue
        if any(parsed.protected[position] for position in candidate.positions):
            raise PrivacyValidationError("HTML match touches structural markup")
        replacements[candidate.positions[0]] = candidate.replacement
        removed.update(candidate.positions)
    output = "".join(
        replacements.get(position, "") if position in removed else character
        for position, character in enumerate(source)
    )
    _, output_views = matching_views(output)
    if any(anonymizer.locate(text) for text, _ in output_views):
        raise PrivacyValidationError("configured value remains in HTML after redaction")
    return output
