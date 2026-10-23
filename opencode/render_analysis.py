#!/usr/bin/env python3
"""Render a saved wiki analysis to its self-contained print view."""

import argparse
import base64
import html
import io
import math
import os
import re
import secrets
import stat
import warnings
from collections import Counter
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urlsplit
from xml.etree.ElementTree import Element, tostring

import bleach
import markdown
import tinycss2
from defusedxml import ElementTree as SafeXML
from defusedxml.common import DefusedXmlException
from markdown.extensions import Extension
from markdown.inlinepatterns import InlineProcessor
from markdown.treeprocessors import Treeprocessor
from PIL import Image, UnidentifiedImageError
from tinycss2.color3 import parse_color

TEMPLATE = Path(
    os.environ.get(
        "KARPATHY_WIKI_PRINT_TEMPLATE",
        "/etc/opencode/skills/wiki-analysis-save/print-template.html",
    )
)
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

MAX_PNG_BYTES = 10 * 1024 * 1024
MAX_SVG_BYTES = 2 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_IMAGES = 50
MAX_HTML_BYTES = 50 * 1024 * 1024
MAX_SVG_ELEMENTS = 10_000
MAX_SVG_DEPTH = 64
MAX_SVG_EXPANSION = 100_000
MAX_ATTRIBUTE = 100_000
SVG_NS = "http://www.w3.org/2000/svg"
XLINK_NS = "http://www.w3.org/1999/xlink"
SVG_TAGS = set(
    "svg g defs title desc path rect circle ellipse line polyline polygon text tspan "
    "clipPath pattern linearGradient radialGradient stop marker use".split()
)
NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?"
NUMBERS = re.compile(rf"{NUMBER}(?:[\s,]+{NUMBER})*\Z")
IDENTIFIER = re.compile(r"[a-zA-Z_][a-zA-Z0-9_.-]*\Z")
REFERENCE = re.compile(r"url\(#([a-zA-Z_][a-zA-Z0-9_.-]*)\)\Z")
PRESENTATION = set(
    "fill stroke color fill-opacity stroke-opacity opacity stroke-width stroke-dasharray "
    "stroke-dashoffset stroke-linecap stroke-linejoin stroke-miterlimit fill-rule clip-rule "
    "clip-path marker-start marker-mid marker-end font-family font-size font-style font-weight "
    "text-anchor dominant-baseline stop-color stop-opacity visibility display".split()
)
GEOMETRY = {
    "svg": {"viewBox", "width", "height", "preserveAspectRatio", "version"},
    "path": {"d", "pathLength"},
    "rect": {"x", "y", "width", "height", "rx", "ry"},
    "circle": {"cx", "cy", "r"},
    "ellipse": {"cx", "cy", "rx", "ry"},
    "line": {"x1", "y1", "x2", "y2"},
    "polyline": {"points"},
    "polygon": {"points"},
    "text": {"x", "y", "dx", "dy", "rotate", "textLength", "lengthAdjust"},
    "tspan": {"x", "y", "dx", "dy", "rotate", "textLength", "lengthAdjust"},
    "clipPath": {"clipPathUnits"},
    "pattern": {
        "x",
        "y",
        "width",
        "height",
        "patternUnits",
        "patternContentUnits",
        "patternTransform",
        "viewBox",
        "preserveAspectRatio",
    },
    "linearGradient": {
        "x1",
        "y1",
        "x2",
        "y2",
        "gradientUnits",
        "gradientTransform",
        "spreadMethod",
    },
    "radialGradient": {
        "cx",
        "cy",
        "r",
        "fx",
        "fy",
        "fr",
        "gradientUnits",
        "gradientTransform",
        "spreadMethod",
    },
    "stop": {"offset"},
    "marker": {
        "refX",
        "refY",
        "markerWidth",
        "markerHeight",
        "markerUnits",
        "orient",
        "viewBox",
        "preserveAspectRatio",
    },
    "use": {"x", "y", "width", "height", "href"},
}


def checked_png(data: bytes) -> bytes:
    """Decode, bound and re-encode a static PNG without source metadata."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG":
                    raise ValueError("file is not a PNG")
                if image.width * image.height > MAX_PIXELS:
                    raise ValueError("PNG exceeds the 40 megapixel limit")
                if getattr(image, "n_frames", 1) != 1:
                    raise ValueError("animated PNG is unsupported")
                image.load()
                # A fresh image drops textual metadata, EXIF and color profiles.
                pixels = image.convert("RGBA")
                clean = Image.new("RGBA", pixels.size)
                clean.paste(pixels)
                output = io.BytesIO()
                clean.save(output, format="PNG")
                if output.tell() > MAX_PNG_BYTES:
                    raise ValueError("re-encoded PNG exceeds the 10 MiB limit")
                return output.getvalue()
    except (
        OSError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ) as exc:
        raise ValueError("invalid or oversized PNG") from exc


def svg_value(name: str, value: str) -> str | None:
    """Validate a static SVG attribute; return its local reference, if any."""
    if len(value) > MAX_ATTRIBUTE:
        raise ValueError("SVG attribute is too long")
    if name == "id":
        valid = IDENTIFIER.fullmatch(value)
    elif name in {"href", "clip-path", "marker-start", "marker-mid", "marker-end"}:
        if name == "href":
            if not value.startswith("#") or not IDENTIFIER.fullmatch(value[1:]):
                raise ValueError("SVG href must be a local fragment")
            return value[1:]
        if value == "none":
            return None
        match = REFERENCE.fullmatch(value)
        if not match:
            raise ValueError(f"SVG {name} must be a local fragment")
        return match.group(1)
    elif name in {"fill", "stroke", "color", "stop-color"}:
        if match := REFERENCE.fullmatch(value):
            return match.group(1)
        valid = value == "none" or parse_color(value) is not None
    elif name in {"transform", "gradientTransform", "patternTransform"}:
        valid = re.fullmatch(
            rf"\s*(?:(?:matrix|translate|scale|rotate|skewX|skewY)\([\s,]*"
            rf"{NUMBER}(?:[\s,]+{NUMBER})*[\s,]*\)\s*)+",
            value,
        )
    elif name == "d":
        valid = re.fullmatch(r"[MmZzLlHhVvCcSsQqTtAa0-9eE+.,\s-]+", value)
    elif name in {
        "font-family",
        "font-style",
        "font-weight",
        "text-anchor",
        "dominant-baseline",
        "stroke-linecap",
        "stroke-linejoin",
        "fill-rule",
        "clip-rule",
        "visibility",
        "display",
        "lengthAdjust",
        "clipPathUnits",
        "patternUnits",
        "patternContentUnits",
        "gradientUnits",
        "spreadMethod",
        "markerUnits",
        "preserveAspectRatio",
        "version",
    }:
        valid = re.fullmatch(r"[a-zA-Z0-9 _.,'-]+", value)
    elif name == "space":
        valid = value in {"default", "preserve"}
    elif name == "orient" and value in {"auto", "auto-start-reverse"}:
        valid = True
    elif name == "stroke-dasharray" and value == "none":
        valid = True
    elif name in {"viewBox", "points", "stroke-dasharray", "rotate"}:
        valid = NUMBERS.fullmatch(value)
    elif name in {"width", "height", "markerWidth", "markerHeight"}:
        valid = re.fullmatch(rf"{NUMBER}(?:px|pt|pc|mm|cm|in|em|ex|%)?", value)
    else:
        valid = re.fullmatch(rf"{NUMBER}(?:px|pt|pc|mm|cm|in|em|ex|%)?", value)
        if not valid:
            valid = NUMBERS.fullmatch(value)
    if not valid:
        raise ValueError(f"unsupported SVG value for {name}")
    # Bound numeric magnitudes without interpreting digits in IDs or color names.
    if name not in {"id", "fill", "stroke", "color", "stop-color", "font-family"}:
        for number in re.findall(NUMBER, value):
            if not math.isfinite(float(number)) or abs(float(number)) > 10_000_000:
                raise ValueError("SVG numeric value exceeds limit")
    return None


def checked_svg(data: bytes) -> bytes:
    """Accept a bounded static subset, rejecting rather than stripping content."""
    try:
        # Streaming limits apply during parsing, before constructing a large tree.
        count = depth = 0
        for event, _ in SafeXML.iterparse(
            io.BytesIO(data), events=("start", "end", "pi"), forbid_dtd=True
        ):
            if event == "pi":
                raise ValueError("SVG processing instructions are unsupported")
            depth += 1 if event == "start" else -1
            count += event == "start"
            if depth > MAX_SVG_DEPTH or count > MAX_SVG_ELEMENTS:
                raise ValueError("SVG tree exceeds complexity limits")
        root = SafeXML.fromstring(data, forbid_dtd=True)
    except (SafeXML.ParseError, DefusedXmlException) as exc:
        raise ValueError("invalid or unsafe SVG XML") from exc
    if root.tag != f"{{{SVG_NS}}}svg":
        raise ValueError("SVG root must use the SVG namespace")
    ids = {}
    references = {}
    for node in root.iter():
        tag = node.tag.removeprefix(f"{{{SVG_NS}}}")
        if not node.tag.startswith(f"{{{SVG_NS}}}") or tag not in SVG_TAGS:
            raise ValueError(f"unsupported SVG element: {tag[:80]}")
        attributes = dict(node.attrib)
        if style := attributes.pop("style", None):
            for declaration in tinycss2.parse_declaration_list(
                style, skip_comments=True, skip_whitespace=True
            ):
                if declaration.type != "declaration" or declaration.important:
                    raise ValueError("unsupported SVG style declaration")
                name = declaration.lower_name
                if name not in PRESENTATION:
                    raise ValueError(f"unsupported SVG style property: {name[:80]}")
                attributes[name] = tinycss2.serialize(declaration.value).strip()
        node.attrib.clear()
        refs = []
        for original, value in attributes.items():
            name = original
            if original == f"{{{XLINK_NS}}}href":
                name = "href"
            elif original == "{http://www.w3.org/XML/1998/namespace}space":
                name = "space"
            if name not in PRESENTATION | GEOMETRY.get(tag, set()) | {"id", "transform", "space"}:
                raise ValueError(f"unsupported SVG attribute: {name[:80]}")
            ref = svg_value(name, value)
            if ref:
                refs.append(ref)
            if name == "id":
                if value in ids:
                    raise ValueError("duplicate SVG id")
                ids[value] = node
            if name in node.attrib:
                raise ValueError("ambiguous SVG attribute aliases")
            node.set(original if name == "space" else name, value)
        references[node] = refs
    # Validate viewports, including positive dimensions (no unbounded raster surface).
    units = {
        "": 1,
        "px": 1,
        "pt": 96 / 72,
        "pc": 16,
        "mm": 96 / 25.4,
        "cm": 96 / 2.54,
        "in": 96,
        "em": 16,
        "ex": 16,
        "%": 1,
    }
    for node in root.iter():
        dimensions = {}
        for name in ("width", "height", "markerWidth", "markerHeight"):
            if name in node.attrib:
                match = re.fullmatch(rf"({NUMBER})(px|pt|pc|mm|cm|in|em|ex|%)?", node.attrib[name])
                number, unit = float(match[1]), match[2] or ""
                if number <= 0 or number * units[unit] > 20_000 or (unit == "%" and number > 100):
                    raise ValueError("SVG dimensions must be positive and bounded")
                dimensions[name] = number * units[unit]
        if dimensions.get("width", 1) * dimensions.get("height", 1) > MAX_PIXELS:
            raise ValueError("SVG viewport exceeds the 40 megapixel limit")
        if "viewBox" in node.attrib:
            values = re.split(r"[\s,]+", node.attrib["viewBox"].strip())
            if len(values) != 4 or any(float(v) <= 0 for v in values[2:]):
                raise ValueError("SVG viewBox must have four values and positive dimensions")
    # Includes child edges and all fragment references: prevents recursive use,
    # pattern/gradient cycles and exponentially expanded use graphs.
    costs = {}
    active = set()

    def cost(node, depth=0):
        if node in active or depth > MAX_SVG_DEPTH:
            raise ValueError("cyclic or deeply expanded SVG reference")
        if node in costs:
            total, height = costs[node]
            if height + depth > MAX_SVG_DEPTH:
                raise ValueError("deeply expanded SVG reference")
            return total, height
        active.add(node)
        total = 1
        height = 0
        for target in references[node]:
            if target not in ids:
                raise ValueError("missing SVG fragment target")
        for child in list(node) + [ids[target] for target in references[node]]:
            child_cost, child_height = cost(child, depth + 1)
            total += child_cost
            height = max(height, child_height + 1)
            if total > MAX_SVG_EXPANSION:
                raise ValueError("SVG reference expansion exceeds limit")
        active.remove(node)
        costs[node] = total, height
        return total, height

    cost(root)
    return tostring(root, encoding="utf-8")


class RawImages(HTMLParser):
    def handle_starttag(self, tag, attrs):
        if tag in {"img", "svg", "picture", "image", "object", "embed", "canvas"}:
            raise ValueError("raw HTML/SVG images are unsupported; use local Markdown images")


class AnalysisImages(Treeprocessor):
    def __init__(self, md, slug: str, asset_root: Path):
        super().__init__(md)
        self.slug = slug
        self.asset_root = asset_root
        self.embedded: dict[str, str] = {}
        self.cache: dict[str, tuple[str, str]] = {}
        self.counts: Counter[str] = Counter()

    def read_asset(self, filename: str, limit: int) -> bytes:
        # Resolve each component relative to an already opened directory, without
        # following symlinks. O_NONBLOCK avoids blocking on a malicious FIFO.
        descriptor = os.open(self.asset_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            for component in ("assets", "analyses", self.slug):
                child = os.open(
                    component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
                )
                os.close(descriptor)
                descriptor = child
            file = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor)
            with os.fdopen(file, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                    raise ValueError("image is not a regular file or exceeds its size limit")
                data = stream.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("image exceeds its size limit")
                return data
        except OSError as exc:
            raise ValueError("image is missing, unreadable, or uses an unsafe path") from exc
        finally:
            os.close(descriptor)

    def run(self, root):
        for raw in self.md.htmlStash.rawHtmlBlocks:
            RawImages().feed(raw)
        for node in root.iter("img"):
            if sum(self.counts.values()) >= MAX_IMAGES:
                raise ValueError("analysis exceeds the 50 image limit")
            src = node.get("src", "")
            prefix = f"/.fs/assets/analyses/{self.slug}/"
            filename = src.removeprefix(prefix)
            if not src.startswith(prefix) or not re.fullmatch(
                r"[a-z0-9]+(?:-[a-z0-9]+)*\.(?:png|svg)", filename
            ):
                raise ValueError(
                    "image must use /.fs/assets/analyses/<slug>/<kebab-name>.png or .svg"
                )
            alt = node.get("alt", "").strip()
            if not alt:
                raise ValueError("analysis images require nonempty alternative text")
            extension = filename.rsplit(".", 1)[1]
            if filename not in self.cache:
                try:
                    data = self.read_asset(
                        filename, MAX_PNG_BYTES if extension == "png" else MAX_SVG_BYTES
                    )
                    data = checked_png(data) if extension == "png" else checked_svg(data)
                except ValueError as exc:
                    raise ValueError(f"image {filename}: {exc}") from exc
                mime = "image/png" if extension == "png" else "image/svg+xml"
                self.cache[filename] = (mime, base64.b64encode(data).decode("ascii"))
            mime, encoded = self.cache[filename]
            image = f'<img src="data:{mime};base64,{encoded}" alt="{html.escape(alt, quote=True)}"'
            if title := node.get("title"):
                image += f' title="{html.escape(title, quote=True)}"'
            image += ">"
            token = "ANALYSISIMAGE" + secrets.token_hex(32)
            self.embedded[token] = image
            self.counts[extension] += 1
            if sum(len(v) for v in self.embedded.values()) > MAX_HTML_BYTES:
                raise ValueError("embedded images exceed the 50 MiB HTML limit")
            tail = node.tail
            node.clear()
            node.tag = "span"
            node.text = token
            node.tail = tail
        return root


class ImageExtension(Extension):
    def __init__(self, slug: str, asset_root: Path):
        super().__init__()
        self.slug = slug
        self.asset_root = asset_root

    def extendMarkdown(self, md):  # noqa: N802 (Python-Markdown API)
        self.processor = AnalysisImages(md, self.slug, self.asset_root)
        md.treeprocessors.register(self.processor, "analysis-images", 0)


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


def render(
    source: str,
    template: str,
    wiki_name: str,
    base_url: str,
    slug: str,
    *,
    asset_root: Path | None = None,
    image_counts: Counter | None = None,
) -> str:
    if not SLUG.fullmatch(slug):
        raise ValueError("analysis slug must use ASCII kebab-case")
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
    images = ImageExtension(slug, asset_root or Path.cwd())
    converter = markdown.Markdown(
        extensions=["fenced_code", "tables", "sane_lists", WikiLinks(base_url), images],
    )
    body = converter.convert("\n".join(lines[2:]))
    body = bleach.clean(
        body,
        tags=TAGS,
        attributes={"a": ["href", "title"]},
        protocols=["http", "https", "mailto"],
        strip=True,
    )
    # Only generated, unguessable tokens survive sanitization. Author-provided
    # img/data URLs never bypass Bleach; only our validated image HTML does.
    for token, image in images.processor.embedded.items():
        body = body.replace(token, image)
    values = {
        "titel": html.escape(title, quote=True),
        "datum": html.escape(date_match.group(1), quote=True),
        "seiten_url": html.escape(page_url, quote=True),
        "wiki_name": html.escape(wiki_name, quote=True),
        "inhalt": body,
    }
    output = re.sub(
        r"\{\{(titel|datum|seiten_url|wiki_name|inhalt)\}\}", lambda m: values[m.group(1)], template
    )
    if len(output.encode("utf-8")) > MAX_HTML_BYTES:
        raise ValueError("analysis exceeds the 50 MiB HTML limit")
    if image_counts is not None:
        image_counts.update(images.processor.counts)
    return output


def write_view(output: str, slug: str) -> Path:
    """Replace the view only after complete validation, without following symlinks."""
    descriptor = os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    temporary = ".render-analysis-" + secrets.token_hex(16)
    created = False
    try:
        for component in ("assets", "analyses"):
            try:
                os.mkdir(component, dir_fd=descriptor)
            except FileExistsError:
                pass
            child = os.open(
                component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = child
        file = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644, dir_fd=descriptor)
        created = True
        with os.fdopen(file, "w", encoding="utf-8") as stream:
            stream.write(output)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, slug + ".html", src_dir_fd=descriptor, dst_dir_fd=descriptor)
        created = False
    finally:
        if created:
            os.unlink(temporary, dir_fd=descriptor)
        os.close(descriptor)
    return Path("assets/analyses") / (slug + ".html")


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
    counts: Counter[str] = Counter()
    try:
        output = render(
            source.read_text(encoding="utf-8"),
            TEMPLATE.read_text(encoding="utf-8"),
            wiki_name,
            args.wiki_url,
            source.stem,
            image_counts=counts,
        )
        destination = write_view(output, source.stem)
    except (ValueError, OSError) as exc:
        parser.error(str(exc))
    print(destination)
    print(f"Embedded images: {counts['png']} PNG, {counts['svg']} SVG")


if __name__ == "__main__":
    main()
