"""Synthetic tag-only regressions for release numbering and CLI failures."""

import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "next-prerelease.py"
next_prerelease = runpy.run_path(str(SCRIPT))["next_prerelease"]


class NextPrereleaseTests(unittest.TestCase):
    def test_starts_at_zero_without_matching_tags(self):
        self.assertEqual(next_prerelease("0.2.2", []), "0.2.2-pre.0")

    def test_advances_past_the_old_limit_and_larger_counters(self):
        for count in (99, 100, 999, 1000):
            with self.subTest(count=count):
                tags = [f"v0.2.2-pre.{number}" for number in range(count + 1)]
                self.assertEqual(next_prerelease("0.2.2", tags), f"0.2.2-pre.{count + 1}")

    def test_uses_numeric_maximum_without_reusing_gaps_or_unreleased_tags(self):
        tags = ["v0.2.2-pre.9", "v0.2.2-pre.100", "v0.2.2-pre.11"]
        self.assertEqual(next_prerelease("0.2.2", tags), "0.2.2-pre.101")

    def test_ignores_other_bases_and_nonmatching_suffixes(self):
        tags = [
            "v0.2.3-pre.1000",
            "v0.2.2",
            "v0.2.2-pre.01",
            "v0.2.2-pre.100.extra",
            "v0.2.2-pre.-1",
            "v0.2.2-pre.2",
        ]
        self.assertEqual(next_prerelease("0.2.2", tags), "0.2.2-pre.3")

    def test_invalid_bases_are_rejected(self):
        for base in ("", "0.2", "v0.2.2", "0.2.2-pre.100", "0x2x2", "0.2.2\n"):
            with self.subTest(base=base), self.assertRaises(ValueError):
                next_prerelease(base, [])

    def test_cli_uses_tags_including_those_without_a_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            subprocess.run(["git", "init", "-q", temporary], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    temporary,
                    "-c",
                    "user.name=Synthetic",
                    "-c",
                    "user.email=test@example.invalid",
                    "commit",
                    "--allow-empty",
                    "-qm",
                    "fixture",
                ],
                check=True,
            )
            for tag in ("v0.2.2-pre.9", "v0.2.2-pre.99", "v0.2.2-pre.100"):
                subprocess.run(["git", "-C", temporary, "tag", tag], check=True)
            result = subprocess.run(
                [sys.executable, str(SCRIPT), "0.2.2"],
                cwd=temporary,
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.stdout, "0.2.2-pre.101\n")

    def test_cli_fails_closed_without_git_metadata_or_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            for args in ([], ["0.2.2"]):
                result = subprocess.run(
                    [sys.executable, str(SCRIPT), *args],
                    cwd=temporary,
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")


if __name__ == "__main__":
    unittest.main()
