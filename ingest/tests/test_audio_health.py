"""Model-free startup, long work, deadline, dependency and restart proofs."""

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock, patch

from karpathy_wiki_ingest.state import StateStore
from karpathy_wiki_ingest_audio import health, publisher, speech_client
from karpathy_wiki_ingest_audio.connector import probe, synchronize
from karpathy_wiki_ingest_audio.speech_client import await_result
from karpathy_wiki_speech import worker
from karpathy_wiki_speech.liveness import Liveness, read_healthy, read_live
from karpathy_wiki_speech.types import DEFAULT_LIMITS, TranscriptionOptions, file_revision


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
            patch.object(speech_client, "read_live", side_effect=[RuntimeError(), {}]),
            patch.object(speech_client.time, "sleep"),
        ):
            speech_client.wait_for_worker(self.root)
        with (
            patch.object(speech_client, "read_live", side_effect=RuntimeError()),
            patch.object(speech_client.time, "monotonic", side_effect=[0, 121]),
            self.assertRaisesRegex(RuntimeError, "did not become ready"),
        ):
            speech_client.wait_for_worker(self.root)

    def test_speech_wait_detects_a_dead_dependency_before_recording_timeout(self):
        with self.assertRaisesRegex(RuntimeError, "service is unready"):
            await_result(self.root, "a" * 64, TranscriptionOptions(), 43200)

    def test_failed_request_does_not_fail_an_unrelated_live_request(self):
        options = TranscriptionOptions()
        failed_hash, valid_hash = "a" * 64, "b" * 64
        worker_status = Liveness(self.root / "worker-health.json")
        self.addCleanup(worker_status.close)
        worker_status.enter("idle", 125, ready=True)
        (self.root / "worker-identity.json").write_text(json.dumps({"identity": "a" * 64}))
        speech_client.ensure_requested(
            self.root, failed_hash, options, f"recordings/{failed_hash}.wav"
        )
        with self.assertLogs(worker.LOG):
            self.assertEqual(
                worker.run_cycle(
                    self.root,
                    worker.FakeBackend(),
                    options,
                    "a" * 64,
                    DEFAULT_LIMITS,
                    decode=False,
                    status=worker_status,
                ),
                (0, 1),
            )
        speech_client.wait_for_worker(self.root)
        with self.assertRaises(speech_client.SpeechProcessingError):
            await_result(self.root, failed_hash, options, 43200)

        source = self.root / "valid.wav"
        source.write_bytes(b"synthetic audio")
        valid_hash = file_revision(str(source))
        reference = speech_client.stage_recording(self.root, source, valid_hash, "wav", 1024)
        speech_client.ensure_requested(self.root, valid_hash, options, reference)

        def progress(_seconds):
            read_live(worker_status.path)
            with self.assertRaises(RuntimeError):
                read_healthy(worker_status.path)
            self.assertEqual(
                worker.run_cycle(
                    self.root,
                    worker.FakeBackend(),
                    options,
                    "a" * 64,
                    DEFAULT_LIMITS,
                    decode=False,
                    status=worker_status,
                ),
                (1, 0),
            )

        with patch.object(speech_client.time, "sleep", side_effect=progress) as sleep:
            self.assertIsNotNone(await_result(self.root, valid_hash, options, 43200))
        sleep.assert_called_once()
        self.now += 121
        with self.assertRaises(RuntimeError):
            read_live(worker_status.path)
        worker_status.enter("transcribing", 1)
        self.now += 2
        worker_status.write()
        with self.assertRaises(RuntimeError):
            read_live(worker_status.path)

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

    def test_sync_includes_only_discoverable_audio(self):
        with patch("karpathy_wiki_ingest_audio.connector.subprocess.run") as run:
            synchronize(self.root, "Recordings")
        command = run.call_args.args[0]
        filters = [command[index + 1] for index, flag in enumerate(command) if flag == "--filter"]
        self.assertEqual(
            set(filters[:-1]),
            {"+ {{[^/]*[^/.][^/]*}}" + suffix for suffix in DEFAULT_LIMITS.allowed_extensions},
        )
        self.assertEqual(filters[-1], "- *")
        self.assertIn("--ignore-case", command)
        self.assertNotIn("--delete-excluded", command)
        self.assertEqual(command[command.index("--retries") + 1], "3")
        self.assertEqual(command[command.index("--low-level-retries") + 1], "10")
        self.assertTrue(run.call_args.kwargs["check"])

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
        self.settings.speech_root.mkdir()
        (self.settings.speech_root / "worker-identity.json").write_text(
            json.dumps({"identity": "a" * 64})
        )

    def test_fresh_store_does_not_scan_backlog_before_readiness(self):
        assert self.settings.state_path is not None
        store = StateStore.open(self.settings.state_path)
        self.addCleanup(store.close)
        with patch.object(publisher, "audio_inventory") as inventory:
            publisher.restore_current_failure(store, self.settings, Mock(), Mock(), Mock(), Mock())
        inventory.assert_not_called()

    def test_unreadable_failure_state_stays_unhealthy_during_sync(self):
        def sync(*args, **kwargs):
            with self.assertRaises(RuntimeError):
                read_healthy(self.settings.health_path)

        with (
            patch.object(publisher.Settings, "from_env", return_value=self.settings),
            patch.object(
                publisher, "install_stop_handler", return_value=Mock(is_set=lambda: False)
            ),
            patch.object(
                publisher.StateStore,
                "job_by_idempotency_key",
                side_effect=sqlite3.OperationalError(),
            ),
            patch.object(publisher.StateStore, "metrics", return_value={"failed_jobs": 1}),
            patch.object(publisher, "probe"),
            patch.object(publisher, "wait_for_worker"),
            patch.object(publisher, "synchronize", side_effect=sync) as synchronization,
            patch.object(publisher, "process_cycle", return_value=(0, 0, False)),
            patch.dict(os.environ, {"WEBDAV_PATH": "Recordings"}),
            self.assertLogs(publisher.LOG),
            self.assertRaises(SystemExit),
        ):
            publisher.run(once=True)
        synchronization.assert_called_once()

    def test_restart_restores_only_current_unresolved_failures_before_sync(self):
        anonymizer = publisher.TargetedAnonymizer.from_file(self.settings.redactions)
        identity = publisher.requested_options_identity(
            publisher.speech_options(), self.settings.speech_root
        )
        key = publisher.cycle_idempotency_key(anonymizer.fingerprint, {}, identity)
        assert self.settings.state_path is not None
        for state in ("dead", "pending", "leased", "succeeded", "superseded", "historical"):
            with self.subTest(state=state):
                store = StateStore.open(self.settings.state_path)
                job, _ = store.enqueue("ingest", {"provider": "audio"}, idempotency_key=key)
                if job.state in ("dead", "succeeded", "superseded"):
                    job = store.rearm(job.id)
                lease = store.claim(job.id, "test")
                assert lease is not None
                if state == "succeeded":
                    store.complete(lease.id)
                else:
                    store.fail(lease.id, "quarantined:1", max_attempts=1 if state == "dead" else 5)
                    if state == "superseded":
                        store.supersede(job.id)
                    elif state == "leased":
                        store.claim(job.id, "retry", now=int(publisher.time.time()) + 61)
                    elif state == "historical":
                        # Same local inputs, but failure belongs to an old speech identity.
                        (self.settings.speech_root / "worker-identity.json").write_text(
                            json.dumps({"identity": "b" * 64})
                        )
                store.close()

                def sync(*args, state=state, **kwargs):
                    if state in ("dead", "pending", "leased"):
                        with self.assertRaises(RuntimeError):
                            read_healthy(self.settings.health_path)
                    else:
                        read_healthy(self.settings.health_path)

                with (
                    patch.object(publisher.Settings, "from_env", return_value=self.settings),
                    patch.object(
                        publisher, "install_stop_handler", return_value=Mock(is_set=lambda: False)
                    ),
                    patch.object(publisher, "probe"),
                    patch.object(publisher, "wait_for_worker"),
                    patch.object(publisher, "synchronize", side_effect=sync) as synchronization,
                    patch.object(
                        publisher,
                        "process_cycle",
                        side_effect=publisher.process_cycle
                        if state in ("dead", "pending", "leased")
                        else lambda *args, **kwargs: (0, 0, False),
                    ),
                    patch.dict(os.environ, {"WEBDAV_PATH": "Recordings"}),
                    self.assertRaises(SystemExit)
                    if state in ("dead", "pending", "leased")
                    else nullcontext(),
                ):
                    publisher.run(once=True)
                synchronization.assert_called_once()
                if state in ("dead", "pending", "leased"):
                    with self.assertRaises(RuntimeError):
                        read_healthy(self.settings.health_path)
                else:
                    read_healthy(self.settings.health_path)
                # Each subtest starts with an independent durable store.
                self.settings.state_path.unlink()

    def test_rclone_errors_do_not_log_private_arguments_or_tracebacks(self):
        command = ["rclone", "sync", "webdav:PRIVATE_REMOTE", "/PRIVATE_LOCAL"]
        for operation in ("probe", "synchronize"):
            for error in (
                subprocess.CalledProcessError(1, command, stderr="PRIVATE_STDERR"),
                subprocess.TimeoutExpired(command, 60, stderr="PRIVATE_STDERR"),
            ):
                with (
                    self.subTest(operation=operation, error=type(error).__name__),
                    patch.object(publisher.Settings, "from_env", return_value=self.settings),
                    patch.object(
                        publisher, "install_stop_handler", return_value=Mock(is_set=lambda: False)
                    ),
                    patch.object(publisher, "wait_for_worker"),
                    patch.object(publisher, "probe"),
                    patch.object(publisher, "synchronize"),
                    patch.object(publisher, operation, side_effect=error),
                    patch.dict(os.environ, {"WEBDAV_PATH": "PRIVATE_REMOTE"}),
                    self.assertLogs(publisher.LOG) as logs,
                    self.assertRaises(SystemExit),
                ):
                    publisher.run(once=True)
                self.assertIn(type(error).__name__, "\n".join(logs.output))
                self.assertNotIn("PRIVATE_", "\n".join(logs.output))
                self.assertTrue(all(record.exc_info is None for record in logs.records))

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
