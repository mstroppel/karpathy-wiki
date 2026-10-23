import base64
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from collections import Counter
from html.parser import HTMLParser
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from PIL import Image, PngImagePlugin

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = Path(os.environ.get("ANALYSIS_RENDERER_SCRIPT", ROOT / "opencode" / "render_analysis.py"))
TEMPLATE = (ROOT / "config" / "skills" / "wiki-analysis-save" / "print-template.html").read_text()
SPEC = importlib.util.spec_from_file_location(
    "render_analysis", SCRIPT, loader=SourceFileLoader("render_analysis", str(SCRIPT))
)
assert SPEC and SPEC.loader
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


class ImageTags(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.images = []
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            self.images.append(dict(attrs))


class AnalysisImageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.assets = self.root / "assets" / "analyses" / "example"
        self.assets.mkdir(parents=True)
        self.source = "# Diagramme\nErstellt: 2026-10-23\n\n"

    def png(self, name="chart.png"):
        image = Image.new("RGBA", (32, 16), (25, 105, 145, 120))
        metadata = PngImagePlugin.PngInfo()
        metadata.add_text("private", "metadata must not survive")
        image.save(self.assets / name, pnginfo=metadata)

    def svg(self, content='<path d="M0 0 L20 20" stroke="blue"/>'):
        data = f'<svg xmlns="{renderer.SVG_NS}" viewBox="0 0 100 50">{content}</svg>'
        (self.assets / "chart.svg").write_text(data)
        return data

    def render(self, body, counts=None):
        return renderer.render(
            self.source + body,
            TEMPLATE,
            "Test Wiki",
            "https://wiki.example.org",
            "example",
            asset_root=self.root,
            image_counts=counts,
        )

    def image(self, name="chart.svg"):
        return f'![Diagramm & Daten](/.fs/assets/analyses/example/{name} "Titel")'

    def test_png_is_reencoded_with_transparency_without_metadata(self):
        self.png()
        counts: Counter[str] = Counter()
        result = self.render(self.image("chart.png"), counts)
        images = ImageTags(result).images
        self.assertEqual(counts, {"png": 1})
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]["alt"], "Diagramm & Daten")
        self.assertEqual(images[0]["title"], "Titel")
        data = base64.b64decode(images[0]["src"].split(",", 1)[1])
        with Image.open(io.BytesIO(data)) as image:
            self.assertEqual(image.getpixel((0, 0)), (25, 105, 145, 120))
            self.assertNotIn("private", image.info)
        self.assertNotIn("ANALYSISIMAGE", result)

    def test_svg_features_are_embedded_as_an_isolated_image(self):
        self.svg("""<defs>
<clipPath id="clip"><rect width="90" height="40"/></clipPath>
<pattern id="hatch" width="4" height="4" patternUnits="userSpaceOnUse">
<path d="M0 0 L4 4" stroke="grey"/></pattern>
<linearGradient id="gradient"><stop offset="0%" stop-color="red"/>
<stop offset="100%" stop-color="blue"/></linearGradient>
<marker id="arrow" markerWidth="4" markerHeight="4" orient="auto">
<path d="M0 0 L4 2 L0 4 Z"/></marker>
<path id="tick" d="M0 0 L0 4"/></defs>
<g clip-path="url(#clip)" transform="translate(2, 2)">
<rect width="30" height="20" fill="url(#hatch)"/>
<circle cx="40" cy="20" r="8" fill="url(#gradient)"/>
<path d="M0 10 L60 10" marker-end="url(#arrow)"/>
<use xmlns:xlink="http://www.w3.org/1999/xlink" xlink:href="#tick"/>
<text x="1" y="35" style="font-size: 10px; fill: navy">Achse &amp; Werte</text></g>""")
        result = self.render(self.image())
        images = ImageTags(result).images
        self.assertTrue(images[0]["src"].startswith("data:image/svg+xml;base64,"))
        svg = ElementTree.fromstring(base64.b64decode(images[0]["src"].split(",", 1)[1]))
        text = svg.find(f".//{{{renderer.SVG_NS}}}text")
        assert text is not None
        self.assertEqual(text.get("fill"), "navy")
        self.assertEqual(text.text, "Achse & Werte")
        self.assertNotIn("style", text.attrib)
        self.assertNotIn("<svg", result)

    def test_repeated_asset_is_validated_once_but_counted_per_occurrence(self):
        self.svg()
        with patch.object(renderer, "checked_svg", wraps=renderer.checked_svg) as validate:
            result = self.render(self.image() + "\n\n" + self.image())
        self.assertEqual(validate.call_count, 1)
        self.assertEqual(len(ImageTags(result).images), 2)

    def test_inline_image_preserves_surrounding_text_and_escapes_labels(self):
        self.svg()
        result = self.render("Before " + self.image() + " after **important** text.")
        self.assertIn("Before <img", result)
        self.assertIn(" after <strong>important</strong> text.", result)
        result = self.render("![&quot; onload=&quot;bad](/.fs/assets/analyses/example/chart.svg)")
        self.assertNotIn(' onload="', result)
        self.assertEqual(len(ImageTags(result).images), 1)

    def test_seven_synthetic_diagrams_and_accessible_table(self):
        self.svg('<rect width="20" height="30" fill="blue"/><text y="40">25 ml</text>')
        self.png()
        body = "\n\n".join(
            f"## Diagramm {i}\n\n" + self.image("chart.png" if i % 2 else "chart.svg")
            for i in range(1, 8)
        )
        body += "\n\n| Wert | Quelle |\n| --- | --- |\n| 25 | [[concepts/test]] |"
        counts: Counter[str] = Counter()
        result = self.render(body, counts)
        self.assertEqual(len(ImageTags(result).images), 7)
        self.assertEqual(counts, {"png": 4, "svg": 3})
        self.assertIn("<table>", result)
        self.assertIn("https://wiki.example.org/concepts/test", result)

    def test_rejects_noncanonical_and_external_paths(self):
        for src in (
            "https://example.org/chart.svg",
            "//example.org/chart.png",
            "file:///etc/passwd",
            "data:image/png;base64,AAAA",
            "../assets/analyses/example/chart.svg",
            "/.fs/assets/analyses/other/chart.svg",
            "/.fs/assets/analyses/example/../chart.svg",
            "/.fs/assets/analyses/example/%2e%2e/chart.svg",
            "/.fs/assets/analyses/example/chart.svg?x=1",
            "/.fs/assets/analyses/example/chart.svg#x",
            "/.fs/assets/analyses/example/chart.jpg",
            "/.fs/assets/analyses/example/sub/chart.svg",
        ):
            with self.subTest(src=src), self.assertRaisesRegex(ValueError, "image must use"):
                self.render(f"![Chart]({src})")

    def test_raw_images_fail_but_code_examples_remain_text(self):
        for body in (
            '<img src="test.png">',
            '<IMG SRC="test.png"/>',
            "<svg><path/></svg>",
            '<picture><img src="test.png"></picture>',
            '<object data="test.svg"></object>',
            'Text <img src="test.png"> text',
        ):
            with self.subTest(body=body), self.assertRaisesRegex(ValueError, "raw HTML/SVG"):
                self.render(body)
        self.assertIn("&lt;img", self.render('`<img src="test.png">`'))
        self.assertIn("&lt;svg", self.render("```html\n<svg></svg>\n```"))

    def test_alternative_text_required(self):
        with self.assertRaisesRegex(ValueError, "alternative text"):
            self.render("![](/.fs/assets/analyses/example/chart.svg)")

    def test_missing_asset_symlinks_and_fifo_rejected(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            self.render(self.image())
        target = self.root / "outside.svg"
        target.write_text(self.svg())
        (self.assets / "chart.svg").unlink()
        (self.assets / "chart.svg").symlink_to(target)
        with self.assertRaisesRegex(ValueError, "unsafe path"):
            self.render(self.image())
        (self.assets / "chart.svg").unlink()
        os.mkfifo(self.assets / "chart.svg")
        with self.assertRaisesRegex(ValueError, "regular file"):
            self.render(self.image())
        (self.assets / "chart.svg").unlink()
        self.assets.rmdir()
        self.assets.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "unsafe path"):
            self.render(self.image())

    def test_asset_size_image_count_and_total_output_limits(self):
        self.svg()
        with patch.object(renderer, "MAX_SVG_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "size limit"):
                self.render(self.image())
        with patch.object(renderer, "MAX_IMAGES", 1):
            with self.assertRaisesRegex(ValueError, "image limit"):
                self.render(self.image() + "\n\n" + self.image())
        with patch.object(renderer, "MAX_HTML_BYTES", 10):
            with self.assertRaisesRegex(ValueError, "HTML limit"):
                self.render(self.image())
            with self.assertRaisesRegex(ValueError, "HTML limit"):
                self.render("no images")

    def test_invalid_mismatched_animated_and_oversized_png(self):
        (self.assets / "chart.png").write_text("not png")
        with self.assertRaisesRegex(ValueError, "PNG"):
            self.render(self.image("chart.png"))
        Image.new("RGB", (4, 4)).save(self.assets / "chart.png", format="JPEG")
        with self.assertRaisesRegex(ValueError, "not a PNG"):
            self.render(self.image("chart.png"))
        Image.new("RGB", (4, 4)).save(
            self.assets / "chart.png",
            save_all=True,
            append_images=[Image.new("RGB", (4, 4), "red")],
            duration=100,
        )
        with self.assertRaisesRegex(ValueError, "animated"):
            self.render(self.image("chart.png"))
        self.png()
        original = (self.assets / "chart.png").read_bytes()
        # Damage the IDAT checksum, not the image signature or compressed pixels.
        broken = bytearray(original)
        start = original.index(b"IDAT")
        length = int.from_bytes(original[start - 4 : start], "big")
        broken[start + 4 + length] ^= 1
        (self.assets / "chart.png").write_bytes(broken)
        with self.assertRaisesRegex(ValueError, "PNG"):
            self.render(self.image("chart.png"))
        self.png()
        with patch.object(renderer, "MAX_PIXELS", 10):
            with self.assertRaisesRegex(ValueError, "megapixel"):
                self.render(self.image("chart.png"))
        with patch.object(renderer.Image, "MAX_IMAGE_PIXELS", 300):
            with self.assertRaisesRegex(ValueError, "oversized"):
                self.render(self.image("chart.png"))

    def test_rejects_svg_active_content_and_external_references(self):
        cases = (
            "<script>alert(1)</script>",
            "<foreignObject/>",
            "<animate/>",
            '<image href="https://example.org/track.png"/>',
            "<style>text {fill: red}</style>",
            '<rect onclick="alert(1)"/>',
            '<use href="https://example.org/chart.svg#x"/>',
            '<use href="data:image/svg+xml;base64,AA"/>',
            '<rect fill="url(https://example.org/track)"/>',
            '<rect style="fill: url(https://example.org/track)"/>',
            '<rect style="background: red"/>',
            '<rect style="@import url(test);"/>',
            '<rect style="fill: red !important"/>',
            '<rect style="bad"/>',
            '<rect style="fill: ' + "rgb(" * 1000 + "1" + ")" * 1000 + '"/>',
            '<rect fill="var(--x)"/>',
            '<rect xmlns="https://example.org/ns"/>',
            '<rect xmlns:x="https://example.org/ns" x:fill="red"/>',
            '<?xml-stylesheet href="https://example.org/style"?>',
        )
        for content in cases:
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.svg(content)
                self.render(self.image())
        for data in (
            '<!DOCTYPE svg [<!ENTITY x "entity">]><svg xmlns="http://www.w3.org/2000/svg">&x;</svg>',
            '<!DOCTYPE svg SYSTEM "file:///etc/passwd"><svg/>',
            "<svg>",
            "<svg/>",
        ):
            with self.subTest(data=data), self.assertRaises(ValueError):
                renderer.checked_svg(data.encode())

    def test_rejects_svg_cycles_expansion_and_invalid_geometry(self):
        cases = (
            '<path id="a"/><path id="a"/>',
            '<use href="#missing"/>',
            '<g id="a"><use href="#a"/></g>',
            '<defs><g id="a"><use href="#b"/></g><g id="b"><use href="#a"/></g></defs>',
            '<rect width="-1"/>',
            '<rect width="1 2"/>',
            '<rect width="99999"/>',
            '<svg width="10000" height="10000"/>',
            '<rect width="200%"/>',
            '<rect xmlns=""/>',
            '<svg viewBox="0 0 0 10"/>',
            '<svg viewBox="0 0 10"/>',
            '<rect x="1e99"/>',
            '<path d="M' + "1" * 100_001 + '"/>',
        )
        for content in cases:
            with self.subTest(content=content[:100]), self.assertRaises(ValueError):
                self.svg(content)
                self.render(self.image())
        self.svg('<g id="a"><path d="M0 0"/></g><use href="#a"/><use href="#a"/>')
        with patch.object(renderer, "MAX_SVG_EXPANSION", 5):
            with self.assertRaisesRegex(ValueError, "expansion"):
                self.render(self.image())
        self.svg("<g><g><g/></g></g>")
        with patch.object(renderer, "MAX_SVG_DEPTH", 2):
            with self.assertRaisesRegex(ValueError, "complexity"):
                self.render(self.image())
        with patch.object(renderer, "MAX_SVG_ELEMENTS", 2):
            with self.assertRaisesRegex(ValueError, "complexity"):
                self.render(self.image())

    def test_failed_cli_preserves_previous_view_and_success_has_counts(self):
        (self.root / "analyses").mkdir()
        (self.root / "AGENTS.md").write_text("# Test Wiki\n")
        source = self.root / "analyses" / "example.md"
        self.png()
        source.write_text(self.source + self.image("chart.png"))
        command = [
            sys.executable,
            str(SCRIPT),
            "analyses/example.md",
            "--wiki-url",
            "https://wiki.example.org",
        ]
        environment = {
            **os.environ,
            "KARPATHY_WIKI_PRINT_TEMPLATE": str(
                ROOT / "config/skills/wiki-analysis-save/print-template.html"
            ),
        }
        success = subprocess.run(command, cwd=self.root, env=environment, capture_output=True)
        self.assertEqual(success.returncode, 0, success.stderr)
        self.assertIn(b"1 PNG, 0 SVG", success.stdout)
        view = self.root / "assets/analyses/example.html"
        previous = view.read_bytes()
        source.write_text(self.source + self.image())
        failed = subprocess.run(command, cwd=self.root, env=environment, capture_output=True)
        self.assertNotEqual(failed.returncode, 0)
        self.assertNotIn(b"Traceback", failed.stderr)
        self.assertEqual(view.read_bytes(), previous)
        self.assertFalse(list(view.parent.glob(".render-analysis-*")))

    def test_atomic_output_rejects_symlink_parents_and_cleans_failed_writes(self):
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        view = renderer.write_view("previous", "example")
        with patch.object(renderer.os, "replace", side_effect=OSError("synthetic failure")):
            with self.assertRaises(OSError):
                renderer.write_view("next", "example")
        self.assertEqual(view.read_text(), "previous")
        self.assertFalse(list(view.parent.glob(".render-analysis-*")))
        view.unlink()
        view.symlink_to(self.root / "unrelated")
        renderer.write_view("safe replacement", "example")
        self.assertFalse(view.is_symlink())
        self.assertFalse((self.root / "unrelated").exists())
        self.assets.rmdir()
        view.unlink()
        view.parent.rmdir()
        view.parent.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            renderer.write_view("outside", "example")

    def test_svg_cached_reference_depth_and_aliases_are_bounded(self):
        content = '<path id="n0" d="M0 0"/>'
        content += "".join(f'<use id="n{i}" href="#n{i - 1}"/>' for i in range(1, 70))
        self.svg("<defs>" + content + '</defs><use href="#n69"/>')
        with self.assertRaisesRegex(ValueError, "deeply expanded"):
            self.render(self.image())
        self.svg(
            '<path id="a"/><use xmlns:xlink="http://www.w3.org/1999/xlink" '
            'href="#a" xlink:href="#a"/>'
        )
        with self.assertRaisesRegex(ValueError, "ambiguous"):
            self.render(self.image())

    def test_svg_geometry_requires_complete_commands_and_arguments(self):
        invalid = (
            '<path d="M0 0 L"/>',
            '<path d="M0 0 L1"/>',
            '<path d="L0 0"/>',
            '<path d="M0,,0"/>',
            '<path d="M,0 0"/>',
            '<path d="M0 0,"/>',
            '<path d="M0 0 Z 1 2"/>',
            '<path d="M0 0 A1 1 0 2 0 1 1"/>',
            '<path d="M0 0 A-1 1 0 0 0 1 1"/>',
            '<path d="M0 0 A1 1 0 0.0 0 1 1"/>',
            '<g transform="translate(1 2 3)"/>',
            '<g transform="scale()"/>',
            '<g transform="rotate(1 2)"/>',
            '<g transform="matrix(1 2 3)"/>',
            '<g transform="skewX(1 2)"/>',
            '<g transform="translate(1),"/>',
            '<g transform="scale(1,,2)"/>',
            '<g transform="scale(1); rotate(2)"/>',
            '<g transform="unknown(1)"/>',
            '<polyline points="0 0 1"/>',
            '<polygon points="0,,0 1,1"/>',
            '<rect x="1 2"/>',
            '<path stroke-linecap="unknown" d="M0 0"/>',
            '<text text-anchor="bogus"/>',
            '<svg preserveAspectRatio="bogus"/>',
            '<rect opacity="-1"/>',
            '<path stroke-dasharray="-1 2" d="M0 0"/>',
            '<circle r="-1"/>',
        )
        for content in invalid:
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.svg(content)
                self.render(self.image())
        for path in (
            "M0 0 10 20 L30 40 50 60 Z",
            "m1e-2-.5h2v3c1 2 3 4 5 6s1 2 3 4q1 2 3 4t5 6z",
            "M0 0 A10 20 30 0 1 40 50 a5 5 0 1 0 -5 -5",
            "M.5.5L1-2M3,4",
        ):
            with self.subTest(path=path):
                self.svg(f'<path d="{path}"/>')
                self.assertEqual(len(ImageTags(self.render(self.image())).images), 1)
        self.svg(
            '<g transform="matrix(1 0 0 1 0 0), translate(1, 2) scale(2) rotate(45 0 0) '
            'skewX(10) skewY(10)"><text x="0 10" y="20" font-weight="bold">Test</text></g>'
        )
        self.assertEqual(len(ImageTags(self.render(self.image())).images), 1)

    def test_svg_units_match_attribute_value_types(self):
        for name in ("opacity", "fill-opacity", "stroke-opacity", "stop-opacity", "offset"):
            tag = "stop" if name in {"stop-opacity", "offset"} else "rect"
            for invalid in ("0.5px", "0.5em", "1pt", "-0.1", "101%", "2"):
                with self.subTest(name=name, value=invalid), self.assertRaises(ValueError):
                    self.svg(f'<{tag} {name}="{invalid}"/>')
                    self.render(self.image())
            for valid in ("0", "0.5", "1", "50%", "100%"):
                with self.subTest(name=name, value=valid):
                    self.svg(f'<{tag} {name}="{valid}"/>')
                    self.assertEqual(len(ImageTags(self.render(self.image())).images), 1)
        for attributes in (
            'pathLength="1px"',
            'stroke-miterlimit="2em"',
            'stroke-miterlimit="0.5"',
            'pathLength="-1"',
        ):
            with self.subTest(attributes=attributes), self.assertRaises(ValueError):
                self.svg(f'<path d="M0 0 L1 1" {attributes}/>')
                self.render(self.image())
        self.svg('<path d="M0 0 L1 1" stroke-width="2px" stroke-miterlimit="2" pathLength="10"/>')
        self.assertEqual(len(ImageTags(self.render(self.image())).images), 1)
        self.svg('<rect style="opacity: 0.5px"/>')
        with self.assertRaises(ValueError):
            self.render(self.image())


class RenderAnalysisTests(unittest.TestCase):
    def test_print_view_preserves_structure_and_sanitizes_content(self):
        source = """# Analyse & <Test>
Erstellt: 2026-09-27

## Befunde

Ein **wichtiger** Befund [1] ([Studie](https://example.org/?a=1&b=2))
und [[concepts/evidenz|Evidenz]] sowie [[entities/person]].

| Quelle | Ergebnis |
| --- | --- |
| [1] | Positiv |

```html
<script>harmless code</script>
```

<script>alert(1)</script><a href="javascript:alert(1)">Unsafe</a>
[Bad](javascript:alert(1))
"""
        result = renderer.render(
            source, TEMPLATE, "Wiki <Test>", "https://wiki.example.org", "beispiel"
        )
        self.assertIn("<title>Analyse &amp; &lt;Test&gt;</title>", result)
        self.assertEqual(result.count("<h1>"), 1)
        self.assertIn("<h2>Befunde</h2>", result)
        self.assertIn("<strong>wichtiger</strong>", result)
        self.assertIn('href="https://example.org/?a=1&amp;b=2"', result)
        self.assertIn('href="https://wiki.example.org/concepts/evidenz"', result)
        self.assertIn('href="https://wiki.example.org/entities/person"', result)
        self.assertIn("<table>", result)
        self.assertIn("&lt;script&gt;harmless code&lt;/script&gt;", result)
        self.assertNotIn("<script>", result)
        self.assertNotIn('href="javascript:', result)
        self.assertIn("Wiki &lt;Test&gt;", result)
        self.assertIn("window.print()", result)
        self.assertIn("size: A4", result)
        self.assertIn('href="https://wiki.example.org/analyses/beispiel"', result)

    def test_rejects_missing_metadata_and_invalid_url(self):
        for source in (
            "# Title\nBody",
            "Title\nErstellt: 2026-09-27",
            "# Title\nErstellt: 2026-99-99",
        ):
            with self.assertRaises(ValueError):
                renderer.render(source, TEMPLATE, "Wiki", "https://wiki.example.org", "test")
        with self.assertRaises(ValueError):
            renderer.render(
                "# Title\nErstellt: 2026-09-27", TEMPLATE, "Wiki", "javascript:alert(1)", "test"
            )

    def test_cli_regenerates_same_view_after_update(self):
        with tempfile.TemporaryDirectory() as temp:
            wiki = Path(temp)
            (wiki / "analyses").mkdir()
            (wiki / "AGENTS.md").write_text("# Test Wiki\n")
            source = wiki / "analyses" / "example.md"
            source.write_text("# Title\nErstellt: 2026-09-27\n\nFirst version\n")
            # The installed script reads its template from the image unless
            # KARPATHY_WIKI_PRINT_TEMPLATE points at one.
            command = [
                sys.executable,
                str(SCRIPT),
                "analyses/example.md",
                "--wiki-url",
                "https://wiki.example.org",
            ]
            environment = {
                **os.environ,
                "KARPATHY_WIKI_PRINT_TEMPLATE": str(
                    ROOT / "config/skills/wiki-analysis-save/print-template.html"
                ),
            }
            subprocess.run(command, cwd=wiki, check=True, capture_output=True, env=environment)
            output = wiki / "assets" / "analyses" / "example.html"
            self.assertIn("First version", output.read_text())
            source.write_text("# Title\nErstellt: 2026-09-27\n\nUpdated version\n")
            subprocess.run(command, cwd=wiki, check=True, capture_output=True, env=environment)
            self.assertIn("Updated version", output.read_text())
            self.assertNotIn("First version", output.read_text())
            invalid = subprocess.run(
                command[:2] + ["analyses/../AGENTS.md", *command[3:]],
                cwd=wiki,
                capture_output=True,
                env=environment,
            )
            self.assertNotEqual(invalid.returncode, 0)
            missing_template = subprocess.run(
                command,
                cwd=wiki,
                capture_output=True,
                env={**os.environ, "KARPATHY_WIKI_PRINT_TEMPLATE": str(wiki / "absent.html")},
            )
            self.assertNotEqual(missing_template.returncode, 0)


if __name__ == "__main__":
    unittest.main()
