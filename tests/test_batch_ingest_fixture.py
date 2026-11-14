"""The synthetic model must not replay actions on child completion notices."""

import runpy
import unittest
from pathlib import Path

action_history = runpy.run_path(
    str(Path(__file__).parent / "integration" / "batch_ingest_runtime.py")
)["action_history"]


class BatchFixtureTests(unittest.TestCase):
    def test_child_notification_keeps_completed_tools(self):
        tool = {"role": "tool", "content": "partial batch result"}
        for content in ("<subagent>Completed</subagent>", [{"type": "text", "text": "Completed"}]):
            with self.subTest(content=content):
                messages = [
                    {"role": "user", "content": "Run synthetic action"},
                    tool,
                    {"role": "user", "content": content},
                ]
                self.assertEqual(action_history(messages), [tool])
                report = {"role": "tool", "content": "report text"}
                messages.extend([report, {"role": "user", "content": content}])
                self.assertEqual(action_history(messages), [tool, report])

    def test_explicit_followup_starts_fresh_action(self):
        for content in (
            "Run synthetic action",
            [{"type": "text", "text": "Run synthetic action"}],
        ):
            with self.subTest(content=content):
                messages = [
                    {"role": "user", "content": "Run synthetic action"},
                    {"role": "tool", "content": "previous action"},
                    {"role": "user", "content": content},
                ]
                self.assertEqual(action_history(messages), [])
