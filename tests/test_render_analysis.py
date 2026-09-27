import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "opencode" / "render_analysis.py"
TEMPLATE = (ROOT / "config" / "skills" / "wiki-analysis-save" / "print-template.html").read_text()
SPEC = importlib.util.spec_from_file_location("render_analysis", SCRIPT)
assert SPEC and SPEC.loader
renderer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(renderer)


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
            # The installed script reads its template from the image. Exercise the
            # CLI with a temporary copy pointing to the repository's template.
            runner = wiki / "renderer.py"
            runner.write_text(
                SCRIPT.read_text().replace(
                    'Path("/etc/opencode/skills/wiki-analysis-save/print-template.html")',
                    f"Path({str(ROOT / 'config/skills/wiki-analysis-save/print-template.html')!r})",
                )
            )
            command = [
                sys.executable,
                str(runner),
                "analyses/example.md",
                "--wiki-url",
                "https://wiki.example.org",
            ]
            subprocess.run(command, cwd=wiki, check=True, capture_output=True)
            output = wiki / "assets" / "analyses" / "example.html"
            self.assertIn("First version", output.read_text())
            source.write_text("# Title\nErstellt: 2026-09-27\n\nUpdated version\n")
            subprocess.run(command, cwd=wiki, check=True, capture_output=True)
            self.assertIn("Updated version", output.read_text())
            self.assertNotIn("First version", output.read_text())
            invalid = subprocess.run(
                command[:2] + ["analyses/../AGENTS.md", *command[3:]], cwd=wiki, capture_output=True
            )
            self.assertNotEqual(invalid.returncode, 0)


if __name__ == "__main__":
    unittest.main()
