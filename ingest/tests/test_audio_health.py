"""Model-free startup, long work, deadline, dependency and restart proofs."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from karpathy_wiki_ingest_audio import health, publisher, speech_client
from karpathy_wiki_ingest_audio.connector import probe, synchronize
from karpathy_wiki_ingest_audio.speech_client import await_result
from karpathy_wiki_speech import worker
from karpathy_wiki_speech.liveness import Liveness, read_healthy
from karpathy_wiki_speech.types import TranscriptionOptions


class LivenessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.now = 1000
        self.wall = patch("karpathy_wiki_speech.liveness.time.time", side_effect=lambda: self.now)
        self.mono = patch(
            "karpathy_wiki_speech.liveness.time.monotonic", side_effect=lambda: self.now
        )
        self.wall.start()
        self.mono.start()
        self.addCleanup(self.wall.stop)
        self.addCleanup(self.mono.stop)
        self.path = self.root / "tmp/health.json"
        self.status = Liveness(self.path)
        self.addCleanup(self.status.close)

    def test_fresh_tmpfs_startup_is_unready_not_a_success(self):
        self.assertEqual(json.loads(self.path.read_text())["phase"], "startup")
        self.assertIsNone(json.loads(self.path.read_text())["last_success_at"])
        with self.assertRaises(RuntimeError):
            read_healthy(self.path)

    def test_active_longer_than_launcher_wait_without_finishing_backlog(self):
        self.status.enter("speech_wait", 43200, ready=True)
        for elapsed in (10, 601, 1201, 2400):
            self.now = 1000 + elapsed
            self.status.write()
            result = read_healthy(self.path)
            self.assertEqual(result["phase"], "speech_wait")
            self.assertIsNone(result["last_success_at"])
            self.assertEqual(result["deadline_at"], 44200)
        self.status.enter("publication", 600)
        self.now += 300
        self.status.write()
        read_healthy(self.path)
        self.status.finish(0, 3720)
        self.assertEqual(read_healthy(self.path)["last_success_at"], self.now)

    def test_timer_alone_cannot_keep_a_stalled_phase_healthy(self):
        self.status.enter("synchronizing", 3600, ready=True)
        self.now += 3601
        self.status.write()
        with self.assertRaises(RuntimeError):
            read_healthy(self.path)
        self.assertEqual(json.loads(self.path.read_text())["phase"], "stalled")
        self.status.enter("publication", 600)
        with self.assertRaises(RuntimeError):
            read_healthy(self.path)

    def test_dead_process_stales_even_with_a_long_phase_deadline(self):
        self.status.enter("speech_wait", 43200, ready=True)
        self.status.close()
        self.now += 121
        with self.assertRaises(RuntimeError):
            read_healthy(self.path)

    def test_idle_is_bounded_and_failure_remains_during_retry(self):
        self.status.finish(1, 3720)
        self.status.enter("synchronizing", 3600)
        with self.assertRaises(RuntimeError):
            read_healthy(self.path)
        self.status.finish(0, 3720)
        read_healthy(self.path)
        self.now += 3721
        self.status.write()
        with self.assertRaises(RuntimeError):
            read_healthy(self.path)

    def test_restart_overwrites_a_previous_ready_record(self):
        self.status.finish(0, 3720)
        self.status.close()
        restarted = Liveness(self.path)
        self.addCleanup(restarted.close)
        with self.assertRaises(RuntimeError):
            read_healthy(self.path)
        self.assertIsNone(json.loads(self.path.read_text())["last_success_at"])

    def test_reader_rejects_missing_invalid_and_future_records_content_free(self):
        for content in ("[]", "{", '{"ready":true,"failed":0,"phase":"x"}'):
            self.path.write_text(content)
            with self.assertRaisesRegex(RuntimeError, "service is unready"):
                read_healthy(self.path)
        self.status.enter("publication", 600, ready=True)
        for value in (2000, "not a number", float("nan")):
            payload = json.loads(self.path.read_text())
            payload["checked_at"] = value
            self.path.write_text(json.dumps(payload))
            with self.assertRaises(RuntimeError):
                read_healthy(self.path)
        self.path.unlink()
        with self.assertRaises(RuntimeError):
            read_healthy(self.path)

    def test_audio_checker_requires_both_live_services(self):
        worker_status = Liveness(self.root / "speech/worker-health.json")
        self.addCleanup(worker_status.close)
        self.status.enter("speech_wait", 43200, ready=True)
        worker_status.enter("transcribing", 43200, ready=True)
        with patch.dict(
            os.environ, {"HEALTH_PATH": str(self.path), "SPEECH_ROOT": str(self.root / "speech")}
        ):
            health.main()
            worker_status.fail()
            with self.assertRaises(SystemExit):
                health.main()

    def test_worker_checker_rejects_stale_and_stalled_health(self):
        self.status.enter("transcribing", 43200, ready=True)
        worker.health_check(self.path)
        self.now += 121
        with self.assertRaises(SystemExit):
            worker.health_check(self.path)
        self.now += 43200
        self.status.write()
        with self.assertRaises(SystemExit):
            worker.health_check(self.path)

    def test_real_worker_dispatch_keeps_long_fake_transcription_active(self):
        requests = self.root / "requests"
        requests.mkdir()
        (requests / ("a" * 64 + "-" + "b" * 64 + ".json")).write_text("{}")

        def transcribe(*args):
            self.now += 1801
            self.status.write()
            payload = read_healthy(self.path)
            self.assertEqual(payload["phase"], "transcribing")
            self.assertIsNone(payload["last_success_at"])
            return 1

        with (
            patch.object(worker, "parse_request", return_value={"synthetic": True}),
            patch.object(worker, "process_request", side_effect=transcribe),
        ):
            self.assertEqual(
                worker.run_cycle(
                    self.root,
                    Mock(),
                    TranscriptionOptions(),
                    "worker",
                    Mock(),
                    status=self.status,
                    processing_timeout=43200,
                ),
                (1, 0),
            )
        self.assertEqual(list(requests.glob("*.json")), [])

    def test_worker_wait_is_bounded_and_allows_initialization(self):
        with (
            patch.object(speech_client, "read_healthy", side_effect=[RuntimeError(), {}]),
            patch.object(speech_client.time, "sleep"),
        ):
            speech_client.wait_for_worker(self.root)
        with (
            patch.object(speech_client, "read_healthy", side_effect=RuntimeError()),
            patch.object(speech_client.time, "monotonic", side_effect=[0, 121]),
            self.assertRaisesRegex(RuntimeError, "did not become ready"),
        ):
            speech_client.wait_for_worker(self.root)

    def test_speech_wait_detects_a_dead_dependency_before_recording_timeout(self):
        with self.assertRaisesRegex(RuntimeError, "service is unready"):
            await_result(self.root, "a" * 64, TranscriptionOptions(), 43200)

    def test_invalid_timeout_configuration_is_rejected(self):
        for name in ("SPEECH_TIMEOUT_SEC", "AUDIO_SYNC_TIMEOUT_SEC", "AUDIO_PHASE_TIMEOUT_SEC"):
            with (
                self.subTest(name=name),
                patch.dict(os.environ, {"REDACTIONS_FILE": "/redactions", name: "0"}),
                self.assertRaisesRegex(ValueError, "must be positive"),
            ):
                publisher.Settings.from_env()

    def test_remote_probe_is_bounded_and_never_exposes_listing(self):
        with patch("karpathy_wiki_ingest_audio.connector.subprocess.run") as run:
            probe("Recordings")
        self.assertEqual(run.call_args.kwargs["timeout"], 60)
        self.assertEqual(run.call_args.kwargs["stdout"], -3)
        self.assertEqual(run.call_args.kwargs["stderr"], -3)

    def test_sync_execution_is_bounded_not_just_its_health_record(self):
        with patch("karpathy_wiki_ingest_audio.connector.subprocess.run") as run:
            synchronize(self.root, "Recordings", timeout_seconds=123)
        self.assertEqual(run.call_args.kwargs["timeout"], 123)
        with (
            patch(
                "karpathy_wiki_ingest_audio.connector.subprocess.run",
                side_effect=subprocess.TimeoutExpired("rclone", 123),
            ),
            self.assertRaises(subprocess.TimeoutExpired),
        ):
            synchronize(self.root, "Recordings", timeout_seconds=123)

    def test_startup_grace_covers_bounded_dependency_and_probe_checks(self):
        compose = Path(__file__).resolve().parents[2] / "compose.yaml"
        audio = (
            compose.read_text().split("\n  audio-ingest:", 1)[1].split("\n  audio-speech:", 1)[0]
        )
        self.assertIn("start_period: 4m", audio)
        self.assertGreater(4 * 60, 120 + 60)


class StartupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.settings = publisher.Settings(
            incoming=root / "incoming",
            sanitized=root / "sources",
            quarantine=root / "quarantine",
            mapping=root / "mapping",
            speech_root=root / "speech",
            speech_timeout=43200,
            interval=3600,
            redactions=root / "redactions",
            health_path=root / "health.json",
            state_path=root / "state.sqlite3",
        )
        self.settings.redactions.write_text(
            '{"people":[{"values":["Synthetic Person"],"replacement":"[PERSON]"}]}'
        )

    def test_broken_state_stays_unhealthy_during_long_work(self):
        def sync(*args, **kwargs):
            with self.assertRaises(RuntimeError):
                read_healthy(self.settings.health_path)

        with (
            patch.object(publisher.Settings, "from_env", return_value=self.settings),
            patch.object(
                publisher, "install_stop_handler", return_value=Mock(is_set=lambda: False)
            ),
            patch.object(publisher.StateStore, "open", side_effect=OSError("private")),
            patch.object(publisher, "probe"),
            patch.object(publisher, "wait_for_worker"),
            patch.object(publisher, "synchronize", side_effect=sync) as synchronization,
            patch.object(publisher, "process_cycle", return_value=(0, 0, False)),
            patch.dict(os.environ, {"WEBDAV_PATH": "Recordings"}),
            self.assertLogs(publisher.LOG),
            self.assertRaises(SystemExit),
        ):
            publisher.run(once=True)
        self.assertEqual(json.loads(self.settings.health_path.read_text())["failed"], 1)
        synchronization.assert_called_once()

    def test_sync_timeout_records_failure_and_retries_without_losing_state(self):
        stop = Mock()
        stop.is_set.return_value = False
        observed = []

        def wait(interval):
            self.assertEqual(interval, 3600)
            observed.append(json.loads(self.settings.health_path.read_text())["failed"])
            return len(observed) == 2

        stop.wait.side_effect = wait
        with (
            patch.object(publisher.Settings, "from_env", return_value=self.settings),
            patch.object(publisher, "install_stop_handler", return_value=stop),
            patch.object(publisher, "probe"),
            patch.object(publisher, "wait_for_worker"),
            patch.object(
                publisher,
                "synchronize",
                side_effect=[subprocess.TimeoutExpired("rclone", 3600), None],
            ) as sync,
            patch.object(publisher, "process_cycle", return_value=(0, 0, False)) as process,
            patch.dict(os.environ, {"WEBDAV_PATH": "Recordings"}),
            self.assertLogs(publisher.LOG) as logs,
        ):
            publisher.run(once=False)
        self.assertEqual(observed, [1, 0], logs.output)
        self.assertEqual(sync.call_count, 2)
        self.assertEqual(sync.call_args.kwargs["timeout_seconds"], 3600)
        process.assert_called_once()
        self.assertIsNotNone(self.settings.state_path)
        assert self.settings.state_path is not None
        self.assertTrue(self.settings.state_path.is_file())
