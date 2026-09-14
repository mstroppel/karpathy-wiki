import base64
import os
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ["TZ"] = "UTC"

import export_sessions


class IntervalSecondsTests(unittest.TestCase):
    def test_parses_supported_units_and_whitespace(self):
        self.assertEqual(export_sessions.interval_seconds(" 15m "), 900)
        self.assertEqual(export_sessions.interval_seconds("2h"), 7200)
        self.assertEqual(export_sessions.interval_seconds("3d"), 259200)
        self.assertEqual(export_sessions.interval_seconds("45"), 45)

    def test_rejects_invalid_interval(self):
        for value in ("", "1.5h", "ten minutes", "-2m", "1M"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                export_sessions.interval_seconds(value)


class PublicUrlTests(unittest.TestCase):
    def test_normalizes_valid_http_url(self):
        self.assertEqual(
            export_sessions.validate_public_url(" https://chat.example.com/base/ "),
            "https://chat.example.com/base",
        )

    def test_rejects_missing_or_unsafe_url(self):
        invalid = (
            "",
            "chat.example.com",
            "ftp://chat.example.com",
            "https://user:secret@chat.example.com",
            "https://chat.example.com?token=secret",
            "https://chat.example.com/#fragment",
        )
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                export_sessions.validate_public_url(value)

    def test_session_url_encodes_server_and_session_id(self):
        public_url = "https://chat.example.com"
        encoded_server = base64.urlsafe_b64encode(public_url.encode()).decode().rstrip("=")
        self.assertEqual(
            export_sessions.session_url("session/with spaces", public_url),
            f"{public_url}/server/{encoded_server}/session/session%2Fwith%20spaces",
        )


class SafeTitleTests(unittest.TestCase):
    def test_replaces_path_characters_and_normalizes_whitespace(self):
        self.assertEqual(export_sessions.safe_title('  Plan: a/b?  "yes"  '), "Plan - a - b - - yes -")

    def test_uses_generic_fallback_and_respects_utf8_limit(self):
        self.assertEqual(export_sessions.safe_title("..."), "Untitled Session")
        title = export_sessions.safe_title("ä" * 120)
        self.assertLessEqual(len(title.encode("utf-8")), 180)
        self.assertTrue(title)


class SelectSessionsTests(unittest.TestCase):
    def setUp(self):
        self.sessions = [
            {"id": "grandchild", "parentID": "child", "time": {"created": 30}},
            {"id": "other", "time": {"created": 5}},
            {"id": "child", "parentID": "root", "time": {"created": 20}},
            {"id": "root", "time": {"created": 10}},
        ]

    def test_default_selection_contains_only_root_sessions(self):
        selected = export_sessions.select_sessions(self.sessions)
        self.assertEqual([session["id"] for session in selected], ["other", "root"])

    def test_explicit_selection_recursively_contains_all_descendants(self):
        selected = export_sessions.select_sessions(self.sessions, "root")
        self.assertEqual(
            [session["id"] for session in selected],
            ["root", "child", "grandchild"],
        )

    def test_recursive_selection_tolerates_cycles(self):
        sessions = [
            {"id": "root", "parentID": "child", "time": {"created": 1}},
            {"id": "child", "parentID": "root", "time": {"created": 2}},
        ]
        selected = export_sessions.select_sessions(sessions, "root")
        self.assertEqual([session["id"] for session in selected], ["root", "child"])

    def test_unknown_root_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Session not found: missing"):
            export_sessions.select_sessions(self.sessions, "missing")


if __name__ == "__main__":
    unittest.main()
