"""Model-free startup, long work, deadline, dependency and restart proofs."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from karpathy_wiki_ingest_audio import health, publisher, speech_client
from karpathy_wiki_ingest_audio.connector import probe
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


class StartupTests(unittest.TestCase):
    def test_broken_state_stays_unhealthy_during_long_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            settings = publisher.Settings(
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
            settings.redactions.write_text('{"people":[]}')

            def sync(*args):
                with self.assertRaises(RuntimeError):
                    read_healthy(settings.health_path)

            with (
                patch.object(publisher.Settings, "from_env", return_value=settings),
                patch.object(
                    publisher, "install_stop_handler", return_value=Mock(is_set=lambda: False)
                ),
                patch.object(publisher.StateStore, "open", side_effect=OSError("private")),
                patch.object(publisher, "probe"),
                patch.object(publisher, "wait_for_worker"),
                patch.object(publisher, "synchronize", side_effect=sync),
                patch.object(publisher, "process_cycle", return_value=(0, 0, False)),
                patch.dict(os.environ, {"WEBDAV_PATH": "Recordings"}),
                self.assertLogs(publisher.LOG),
                self.assertRaises(SystemExit),
            ):
                publisher.run(once=True)
            self.assertEqual(json.loads(settings.health_path.read_text())["failed"], 1)
