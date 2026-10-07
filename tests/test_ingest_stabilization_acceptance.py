"""Model-free regressions for disposable acceptance safety and assertions."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from acceptance import ingest_stabilization as acceptance


def session(role, operations=(), peak=0):
    return {
        "info": {"agent": role},
        "messages": [
            {
                "tokens": {"input": peak - 100, "cache": {"read": 100}},
                "content": [
                    {
                        "type": "tool",
                        "name": "wiki_ingest_transaction",
                        "state": {"input": {"operation": operation}},
                    }
                    for operation in operations
                ],
            }
        ],
    }


def exports():
    return {
        "parent": session("wiki-ingest-orchestrator", peak=13259),
        "interrupted": session("wiki-ingest", ("prepare",), peak=10680),
        "recovery": session("wiki-ingest", ("resume",), peak=9000),
        **{
            f"worker-{index}": session("wiki-ingest", ("prepare",), peak=10000)
            for index in range(3)
        },
    }


class SessionMetricsTests(unittest.TestCase):
    def test_fresh_workers_include_interrupted_and_recovery_sessions(self):
        metrics = acceptance.session_metrics(exports())
        self.assertEqual(metrics["prepare_calls"], 4)
        self.assertEqual(metrics["resume_calls"], 1)
        self.assertEqual(metrics["prepare_calls_by_worker_session"]["interrupted"], 1)
        self.assertEqual(metrics["prepare_calls_by_worker_session"]["recovery"], 0)
        self.assertEqual(metrics["peak_input_plus_cache_by_agent"]["wiki-ingest"], 10680)

    def test_one_worker_preparing_four_sources_is_rejected(self):
        values = {
            "parent": session("wiki-ingest-orchestrator", peak=10000),
            "reused": session("wiki-ingest", ("prepare",) * 4 + ("resume",), peak=10000),
        }
        with self.assertRaisesRegex(AssertionError, "more than one source"):
            acceptance.session_metrics(values)

    def test_both_agent_context_limits_include_cache_and_accept_exact_boundary(self):
        for key in ("parent", "interrupted"):
            with self.subTest(session=key):
                values = exports()
                values[key]["messages"][0]["tokens"] = {
                    "input": 31900,
                    "cache": {"read": 100},
                }
                acceptance.session_metrics(values)
                values[key]["messages"][0]["tokens"]["cache"]["read"] = 101
                with self.assertRaisesRegex(AssertionError, "exceeded context budget"):
                    acceptance.session_metrics(values)

    def test_orchestrator_cannot_prepare_sources(self):
        values = exports()
        values["parent"] = session("wiki-ingest-orchestrator", ("prepare",), peak=10000)
        with self.assertRaisesRegex(AssertionError, "belong to a worker"):
            acceptance.session_metrics(values)


class CleanupTests(unittest.TestCase):
    def test_sigterm_timeout_kills_and_reaps_before_container_removal(self):
        process = Mock()
        process.wait.side_effect = [subprocess.TimeoutExpired("model", 30), 0]
        order = Mock()
        order.attach_mock(process, "process")
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(acceptance.subprocess, "run") as remove,
        ):
            order.attach_mock(remove, "remove")
            acceptance.cleanup("synthetic-container", process, Path(directory), failed=False)
        self.assertEqual(
            order.mock_calls,
            [
                call.process.terminate(),
                call.process.wait(timeout=30),
                call.process.kill(),
                call.process.wait(),
                call.remove(
                    ["docker", "rm", "--force", "synthetic-container"],
                    check=False,
                    capture_output=True,
                ),
            ],
        )

    def test_failed_evidence_export_still_removes_container(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                acceptance,
                "preserve_failure_evidence",
                side_effect=OSError("Synthetic export error"),
            ),
            patch.object(acceptance.subprocess, "run") as remove,
        ):
            with self.assertRaisesRegex(OSError, "Synthetic export error"):
                acceptance.cleanup("synthetic-container", None, Path(directory), failed=True)
            remove.assert_called_once_with(
                ["docker", "rm", "--force", "synthetic-container"],
                check=False,
                capture_output=True,
            )


if __name__ == "__main__":
    unittest.main()
