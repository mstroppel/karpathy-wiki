import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from karpathy_wiki_ingest import health as healthcheck
from karpathy_wiki_ingest.shared import TargetedAnonymizer
from karpathy_wiki_ingest_webdav import (
    install_stop_handler,
    interval_seconds,
    run,
    sanitize_once,
    settings,
    synchronize,
)


class WebdavTests(unittest.TestCase):
    def test_sanitizes_text_and_quarantines_binary_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incoming = root / "incoming"
            sanitized = root / "sanitized"
            quarantine = root / "quarantine"
            (incoming / "nested").mkdir(parents=True)
            (incoming / "nested/source.txt").write_text("Hallo Max Mustermann", encoding="utf-8")
            (incoming / "private.pdf").write_bytes(b"%PDF\xff")
            anonymizer = TargetedAnonymizer.from_config(
                {"people": [{"replacement": "[ICH]", "values": ["Max Mustermann"]}]}
            )

            self.assertEqual(sanitize_once(incoming, sanitized, quarantine, anonymizer), (1, 1))
            self.assertEqual((sanitized / "nested/source.txt").read_text(), "Hallo [ICH]")
            self.assertFalse((sanitized / "private.pdf").exists())
            self.assertIn("UnicodeDecodeError", (quarantine / "private.pdf.error").read_text())

    def test_removed_upstream_files_are_dropped_from_sanitized_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incoming = root / "incoming"
            sanitized = root / "sanitized"
            quarantine = root / "quarantine"
            incoming.mkdir()
            (incoming / "keep.txt").write_text("Hallo Max Mustermann", encoding="utf-8")
            sanitized.mkdir()
            (sanitized / "gone.txt").write_text("veraltet", encoding="utf-8")
            anonymizer = TargetedAnonymizer.from_config(
                {"people": [{"replacement": "[ICH]", "values": ["Max Mustermann"]}]}
            )

            changed, failed = sanitize_once(incoming, sanitized, quarantine, anonymizer)

            self.assertEqual((changed, failed), (1, 0))
            self.assertTrue((sanitized / "keep.txt").is_file())
            self.assertFalse((sanitized / "gone.txt").exists())


class IntervalTests(unittest.TestCase):
    def test_parses_plain_seconds_and_durations(self):
        self.assertEqual(interval_seconds("90"), 90)
        self.assertEqual(interval_seconds("45s"), 45)
        self.assertEqual(interval_seconds("15m"), 900)
        self.assertEqual(interval_seconds("1h"), 3600)
        self.assertEqual(interval_seconds("1.5m"), 90)
        self.assertEqual(interval_seconds("0.5h"), 1800)

    def test_rejects_non_positive_and_unparseable_values(self):
        for value in ("0", "-5m", "0s", "", "abc", "15x", "0.5", "0.5s", "90.5", "1.5s"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    interval_seconds(value)


class SettingsTests(unittest.TestCase):
    def test_defaults_and_environment_overrides(self):
        base = {
            "REDACTIONS_FILE": "/redactions.json",
        }
        with mock.patch.dict(os.environ, base, clear=True):
            incoming, sanitized, quarantine, interval, redactions, health = settings()
        self.assertEqual(incoming, Path("/data/incoming/webdav"))
        self.assertEqual(sanitized, Path("/data/sanitized/webdav"))
        self.assertEqual(quarantine, Path("/data/quarantine/webdav"))
        self.assertEqual(interval, 900)
        self.assertEqual(redactions, Path("/redactions.json"))
        self.assertEqual(health, Path("/tmp/health.json"))

    def test_environment_overrides(self):
        environment = {
            "WEBDAV_INCOMING_ROOT": "/i",
            "WEBDAV_SANITIZED_ROOT": "/s",
            "WEBDAV_QUARANTINE_ROOT": "/q",
            "WEBDAV_SYNC_INTERVAL": "2m",
            "REDACTIONS_FILE": "/r.json",
            "HEALTH_PATH": "/h.json",
        }
        with mock.patch.dict(os.environ, environment, clear=True):
            settings_tuple = settings()
        self.assertEqual(
            settings_tuple,
            (Path("/i"), Path("/s"), Path("/q"), 120, Path("/r.json"), Path("/h.json")),
        )

    def test_missing_redactions_file_is_rejected(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "REDACTIONS_FILE is required"):
                settings()


class SynchronizeTests(unittest.TestCase):
    def test_rclone_command_contains_retries(self):
        with mock.patch("karpathy_wiki_ingest_webdav.subprocess.run") as run_mock:
            synchronize(Path("/incoming"), "Wiki Sources")
        command = run_mock.call_args.args[0]
        self.assertEqual(command[0], "rclone")
        self.assertIn("webdav:Wiki Sources", command)
        self.assertIn(str(Path("/incoming")), command)
        self.assertIn("--create-empty-src-dirs", command)
        self.assertEqual(command[command.index("--retries") + 1], "3")
        self.assertEqual(command[command.index("--low-level-retries") + 1], "10")

    def test_rclone_failure_raises(self):
        with mock.patch(
            "karpathy_wiki_ingest_webdav.subprocess.run",
            side_effect=subprocess.CalledProcessError(5, "rclone"),
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                synchronize(Path("/incoming"), "Wiki Sources")


class RunLoopTests(unittest.TestCase):
    def settings(self, root):
        return {
            "WEBDAV_PATH": "Wiki Sources",
            "WEBDAV_INCOMING_ROOT": str(root / "incoming"),
            "WEBDAV_SANITIZED_ROOT": str(root / "sanitized"),
            "WEBDAV_QUARANTINE_ROOT": str(root / "quarantine"),
            "WEBDAV_SYNC_INTERVAL": "1s",
            "REDACTIONS_FILE": str(root / "redactions.json"),
            "HEALTH_PATH": str(root / "health.json"),
        }

    def anonymizer_configuration(self, root):
        path = root / "redactions.json"
        path.write_text(
            json.dumps({"people": [{"replacement": "[ICH]", "values": ["Max Mustermann"]}]}),
            encoding="utf-8",
        )

    def test_once_succeeds_without_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.anonymizer_configuration(root)
            with (
                mock.patch.dict(os.environ, self.settings(root), clear=True),
                mock.patch("karpathy_wiki_ingest_webdav.synchronize"),
            ):
                run(once=True)
            health = json.loads((root / "health.json").read_text())
            self.assertEqual(health["failed"], 0)

    def test_once_reports_rclone_failure_as_exit_code(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.anonymizer_configuration(root)
            error = subprocess.CalledProcessError(5, "rclone")
            with (
                mock.patch.dict(os.environ, self.settings(root), clear=True),
                mock.patch(
                    "karpathy_wiki_ingest_webdav.synchronize",
                    side_effect=error,
                ),
            ):
                with self.assertRaises(SystemExit) as raised:
                    run(once=True)
            self.assertEqual(raised.exception.code, 1)
            health = json.loads((root / "health.json").read_text())
            self.assertEqual(health["failed"], 1)

    def test_daemon_retries_after_rclone_failure_and_writes_health(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.anonymizer_configuration(root)
            stop_event = threading.Event()
            attempts = []

            def first_failure_then_success(*_args):
                attempts.append(True)
                if len(attempts) == 1:
                    # The first cycle fails; the daemon must retry on the
                    # next interval instead of stopping.
                    raise subprocess.CalledProcessError(5, "rclone")
                # The retry succeeded; shut the daemon down afterwards.
                stop_event.set()

            with (
                mock.patch.dict(os.environ, self.settings(root), clear=True),
                mock.patch(
                    "karpathy_wiki_ingest_webdav.install_stop_handler",
                    return_value=stop_event,
                ),
                mock.patch(
                    "karpathy_wiki_ingest_webdav.synchronize",
                    side_effect=first_failure_then_success,
                ),
                mock.patch(
                    "karpathy_wiki_ingest_webdav.sanitize_once",
                    return_value=(0, 0),
                ) as sanitize,
            ):
                run(once=False)
            self.assertEqual(len(attempts), 2)
            sanitize.assert_called_once()
            health = json.loads((root / "health.json").read_text())
            self.assertEqual(health["failed"], 0)

    def test_stop_event_interrupts_interval_wait(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.anonymizer_configuration(root)
            stop_event = threading.Event()

            def one_successful_cycle(*_args):
                stop_event.set()

            with (
                mock.patch.dict(os.environ, self.settings(root), clear=True),
                mock.patch(
                    "karpathy_wiki_ingest_webdav.install_stop_handler",
                    return_value=stop_event,
                ),
                mock.patch(
                    "karpathy_wiki_ingest_webdav.synchronize",
                    side_effect=one_successful_cycle,
                ) as synchronize_mock,
            ):
                run(once=False)
            synchronize_mock.assert_called_once()
            self.assertEqual(json.loads((root / "health.json").read_text())["failed"], 0)

    def test_missing_webdav_path_configuration_fails_without_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.anonymizer_configuration(root)
            environment = self.settings(root)
            environment.pop("WEBDAV_PATH", None)
            with mock.patch.dict(os.environ, environment, clear=True):
                with self.assertRaises(SystemExit) as raised:
                    run(once=True)
            self.assertEqual(raised.exception.code, 1)
            health = json.loads((root / "health.json").read_text())
            self.assertEqual(health["failed"], 1)
            health = json.loads((root / "health.json").read_text())
            self.assertEqual(health["failed"], 1)


class StopHandlerTests(unittest.TestCase):
    def test_sigterm_sets_stop_event(self):
        stop_event = install_stop_handler()
        os.kill(os.getpid(), __import__("signal").SIGTERM)
        self.assertTrue(stop_event.wait(1))


class HealthcheckModuleTests(unittest.TestCase):
    def test_webdav_interval_is_parsed_from_duration(self):
        with mock.patch.dict(
            os.environ,
            {"WEBDAV_SYNC_INTERVAL": "15m"},
            clear=True,
        ):
            self.assertEqual(healthcheck.interval(), 900)

    def test_paperless_interval_takes_precedence(self):
        with mock.patch.dict(
            os.environ,
            {"SYNC_INTERVAL_SECONDS": "300", "WEBDAV_SYNC_INTERVAL": "15m"},
            clear=True,
        ):
            self.assertEqual(healthcheck.interval(), 300)
