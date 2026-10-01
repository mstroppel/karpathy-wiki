"""Audio ingest tests: redaction, identity, publication, durable cycles.

A scripted speech backend runs inside an in-process fake worker, so the
provider/worker handoff, cache replay, and limits are all exercised
deterministically without codecs or model downloads.
"""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from karpathy_wiki_ingest.manifest import validate_manifest
from karpathy_wiki_ingest.shared import TargetedAnonymizer
from karpathy_wiki_ingest.state import JOB_SUCCEEDED, StateStore
from karpathy_wiki_ingest_audio import Settings
from karpathy_wiki_ingest_audio.identity import load_mapping, resolve_identity
from karpathy_wiki_ingest_audio.publisher import (
    ACTIVE_SYMLINK,
    SOURCE_NAME,
    audio_inventory,
    cycle_idempotency_key,
    process_cycle,
    title_of,
)
from karpathy_wiki_ingest_audio.redaction import redact_transcript, segment_texts
from karpathy_wiki_ingest_audio.render import format_timestamp, sanitize_title
from karpathy_wiki_speech import worker as speech_worker
from karpathy_wiki_speech.backends import FakeBackend
from karpathy_wiki_speech.types import (
    DEFAULT_LIMITS,
    DecodedAudio,
    LimitExceededError,
    Segment,
    TranscriptionLimits,
    TranscriptionOptions,
    TranscriptionResult,
)

ANONYMIZER_CONFIGURATION = {"people": [{"replacement": "[ICH]", "values": ["Max Mustermann"]}]}

DEFAULT_RECORDING = b"fake-pcm-audio"


def anonymizer() -> TargetedAnonymizer:
    return TargetedAnonymizer.from_config(ANONYMIZER_CONFIGURATION)


def audio_digest(payload: bytes = DEFAULT_RECORDING) -> str:
    return hashlib.sha256(payload).hexdigest()


def decoded(duration_seconds: float = 2.0, input_bytes: int = 128) -> DecodedAudio:
    return DecodedAudio(
        duration_seconds=duration_seconds, input_bytes=input_bytes, format_name="mp3"
    )


def write_recording(incoming: Path, relative: str, payload: bytes = DEFAULT_RECORDING) -> None:
    path = incoming / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)


class ScriptedBackend:
    """Deterministic backend whose segments are configured per file stem."""

    def __init__(self, transcripts: dict[str, list[Segment]] | None = None) -> None:
        self.transcripts: dict[str, list[Segment]] = dict(transcripts or {})

    def describe(self, options: TranscriptionOptions) -> dict[str, object]:
        return {
            "backend": "scripted",
            "model": "scripted-1",
            "options": describe_options(options),
        }

    def transcribe(
        self, path: str, audio: DecodedAudio, options: TranscriptionOptions
    ) -> TranscriptionResult:
        stem = Path(path).stem
        base = self.transcripts.get(
            stem, [Segment(0, int(audio.duration_seconds * 1000), f"{stem} Talking Point")]
        )
        segments = [
            Segment(
                start_ms=base[index].start_ms,
                end_ms=base[index].end_ms,
                text=base[index].text,
                # Anonymous speaker turns only when diarization is requested.
                speaker_id=index % 2 if options.diarize else base[index].speaker_id,
            )
            for index in range(len(base))
        ]
        return TranscriptionResult(
            segments=segments,
            language=options.language or "de",
            backend="scripted",
            model="scripted-1",
            options=describe_options(options),
        )


def describe_options(options: TranscriptionOptions) -> dict[str, object]:
    return {
        "language": options.language,
        "model": options.model,
        "compute_type": options.compute_type,
        "device": options.device,
        "diarize": options.diarize,
    }


class RedactionTests(unittest.TestCase):
    def test_structured_name_overlap_is_replaced_once(self):
        target = TargetedAnonymizer.from_config(
            {"people": [{"first_name": "Anton", "last_name": "Hotz", "replacement": "[PERSON_1]"}]}
        )
        segments, counts = redact_transcript(
            [Segment(0, 1000, "Anton"), Segment(1000, 2000, "Hotz")], target
        )
        expected, expected_counts = target.anonymize("Anton Hotz")
        self.assertEqual([s.text for s in segments], [expected, ""])
        self.assertEqual(counts, expected_counts)

    def test_multiple_matches_in_one_segment_keep_each_replacement(self):
        redacted, counts = self.redact([Segment(0, 1000, "Max Mustermann und Max Mustermann")])
        self.assertEqual(redacted[0].text, "[ICH] und [ICH]")
        self.assertEqual(counts, {"PERSON": 2})

    def test_literal_takes_precedence_over_overlapping_phone(self):
        target = TargetedAnonymizer.from_config(
            {
                "people": [{"replacement": "[CONTACT]", "values": ["Call 0170 1234567"]}],
                "phones": [{"replacement": "[PHONE]", "values": ["0170 1234567"]}],
            }
        )
        redacted, counts = redact_transcript(
            [Segment(0, 1000, "Call 0170"), Segment(1000, 2000, "1234567")], target
        )
        self.assertEqual([s.text for s in redacted], ["[CONTACT]", ""])
        self.assertEqual(counts, {"PERSON": 1})

    def redact(self, segments, anonymizer_instance=None):
        return redact_transcript(segments, anonymizer_instance or anonymizer())

    def test_literal_inside_one_segment_is_replaced_with_timing_kept(self):
        segments = [Segment(0, 4000, "Hallo Max Mustermann")]
        redacted, counts = self.redact(segments)
        self.assertEqual(redacted[0].text, "Hallo [ICH]")
        self.assertEqual((redacted[0].start_ms, redacted[0].end_ms), (0, 4000))
        self.assertEqual(counts, {"PERSON": 1})

    def test_multiword_literal_split_across_segments_is_removed_once(self):
        segments = [
            Segment(0, 2000, "Ich treffe Max"),
            Segment(2000, 4000, "Mustermann im Büro"),
        ]
        redacted, counts = self.redact(segments)
        self.assertEqual([segment.start_ms for segment in redacted], [0, 2000])
        self.assertEqual([segment.end_ms for segment in redacted], [2000, 4000])
        self.assertEqual(" ".join(segment_texts(redacted)), "Ich treffe [ICH] im Büro")
        self.assertEqual(counts, {"PERSON": 1})

    def test_phone_split_across_segments_is_replaced_once(self):
        target = TargetedAnonymizer.from_config(
            {"phones": [{"replacement": "[TELEFON]", "values": ["+49 170 1234567"]}]}
        )
        segments = [
            Segment(0, 2000, "Ruf mich unter 0170 / 123"),
            Segment(2000, 4000, "45 67 an"),
        ]
        redacted, _ = redact_transcript(segments, target)
        joined = " ".join(" ".join(text.split()) for text in segment_texts(redacted))
        self.assertNotIn("0170", joined)
        self.assertNotIn("123", joined)
        self.assertIn("[TELEFON]", joined)
        self.assertEqual(joined.count("[TELEFON]"), 1)

    def test_transcript_without_configured_literals_stays_untouched(self):
        segments = [Segment(0, 2000, "unchanged text")]
        redacted, counts = self.redact(segments)
        self.assertEqual(redacted[0].text, "unchanged text")
        self.assertEqual(counts, {})

    def test_repeated_literal_is_replaced_in_every_segment(self):
        segments = [
            Segment(0, 2000, "Max Mustermann sagt"),
            Segment(2000, 4000, "und Max Mustermann wieder"),
        ]
        redacted, counts = self.redact(segments)
        self.assertEqual(redacted[0].text, "[ICH] sagt")
        self.assertEqual(redacted[1].text, "und [ICH] wieder")
        self.assertEqual(counts, {"PERSON": 2})

    def test_literal_spanning_three_segments_has_all_parts_removed(self):
        segments = [
            Segment(0, 1000, "Antwort von"),
            Segment(1000, 2000, "Max"),
            Segment(2000, 3000, "Mustermann"),
        ]
        redacted, counts = self.redact(segments)
        self.assertEqual([text for text in segment_texts(redacted)], ["Antwort von", "[ICH]", ""])
        # Timings of fully covered segments are preserved (no duplication).
        self.assertEqual(
            [(segment.start_ms, segment.end_ms, segment.speaker_id) for segment in segments],
            [(0, 1000, None), (1000, 2000, None), (2000, 3000, None)],
        )
        self.assertEqual(counts, {"PERSON": 1})


class RenderTests(unittest.TestCase):
    def test_format_timestamp_zero_and_larger(self):
        self.assertEqual(format_timestamp(0), "00:00:00")
        self.assertEqual(format_timestamp(83000), "00:01:23")
        self.assertEqual(format_timestamp(3661000), "01:01:01")

    def test_title_is_collapsed_length_capped_and_extension_free(self):
        self.assertEqual(sanitize_title("  a   b\n c ", "Fehl"), "a b c")
        self.assertEqual(sanitize_title("   ", "Fehl"), "Fehl")
        self.assertEqual(len(sanitize_title("x" * 500, "Fehl")), 120)
        self.assertEqual(title_of("Meetings/meeting-notes.mp3"), "meeting-notes")


class WorkerCacheTests(unittest.TestCase):
    """The fake worker: replay, worker identity, and content-free failures."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def run_worker(self, transcripts: dict[str, list[Segment]] | None = None):
        backend = ScriptedBackend(transcripts)
        pipeline = TranscriptionOptions(model="scripted-1")
        tag = speech_worker.worker_identity(backend, pipeline)
        return speech_worker.run_cycle(
            self.root, backend, pipeline, tag, DEFAULT_LIMITS, decode=False
        )

    def queue_request(self, payload: bytes = DEFAULT_RECORDING) -> str:
        from karpathy_wiki_speech.cache import options_key, request_id, request_path

        digest = hashlib.sha256(payload).hexdigest()
        options = {"language": None, "diarize": False}
        key = options_key(options)
        identifier = request_id(digest, key)
        staging = self.root / "recordings" / digest[:2]
        staging.mkdir(parents=True, exist_ok=True)
        (staging / f"{digest}.mp3").write_bytes(payload)
        (self.root / "requests").mkdir(parents=True, exist_ok=True)
        atomic_request = request_path(self.root, identifier)
        atomic_request.write_text(
            json.dumps(
                {
                    "audio": f"recordings/{digest[:2]}/{digest}.mp3",
                    "audio_sha256": digest,
                    "options": options,
                }
            ),
            encoding="utf-8",
        )
        return identifier

    def test_completed_request_lands_in_the_cache_and_request_is_removed(self):
        identifier = self.queue_request()
        digest = audio_digest()
        transcripts = {
            digest: [
                Segment(0, 1000, "Ich treffe Max"),
                Segment(1000, 2000, "Mustermann im Büro"),
            ]
        }
        completed, failed = self.run_worker(transcripts)
        self.assertEqual((completed, failed), (1, 0))
        self.assertFalse((self.root / "requests" / f"{identifier}.json").exists())
        from karpathy_wiki_ingest_audio.speech_client import load_result

        result = load_result(self.root, digest, TranscriptionOptions())
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.backend, "scripted")
        self.assertIn("Max", result.segments[0].text)

    def test_worker_runs_without_ingest_core_installed(self):
        self.queue_request()
        source = Path(speech_worker.__file__).resolve().parents[1]
        script = (
            "import sys, runpy; "
            f"sys.path.insert(0, {str(source)!r}); "
            "sys.argv = ['karpathy_wiki_speech', '--once']; "
            "runpy.run_module('karpathy_wiki_speech', run_name='__main__')"
        )
        subprocess.run(
            [sys.executable, "-I", "-S", "-c", script],
            env={**os.environ, "SPEECH_ROOT": str(self.root), "SPEECH_BACKEND": "fake"},
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(json.loads((self.root / "worker-health.json").read_text())["failed"], 0)
        self.assertTrue(list((self.root / "cache").glob("*.results.json")))

    def test_second_cycle_replays_from_cache(self):
        digest = audio_digest()
        transcripts = {digest: [Segment(0, 1000, "One"), Segment(1000, 2000, "Two")]}
        self.queue_request()
        self.run_worker(transcripts)
        identifier = self.queue_request()
        completed, failed = self.run_worker(transcripts)
        self.assertEqual((completed, failed), (1, 0))
        self.assertFalse((self.root / "requests" / f"{identifier}.json").exists())

    def test_empty_transcript_completes_provider_handoff(self):
        from karpathy_wiki_ingest_audio.speech_client import await_result

        self.queue_request()
        self.assertEqual(self.run_worker({audio_digest(): []}), (1, 0))
        result = await_result(self.root, audio_digest(), TranscriptionOptions(), 0)
        self.assertEqual(result.segments, [])

    def test_provider_requeues_after_idle_worker_configuration_change(self):
        from karpathy_wiki_ingest_audio.speech_client import ensure_requested, load_result

        identifier = self.queue_request()
        self.run_worker()
        backend = ScriptedBackend()
        pipeline = TranscriptionOptions(model="changed")
        tag = speech_worker.worker_identity(backend, pipeline)
        speech_worker.run_cycle(self.root, backend, pipeline, tag, DEFAULT_LIMITS, decode=False)
        self.assertIsNone(load_result(self.root, audio_digest(), TranscriptionOptions()))
        ensure_requested(
            self.root,
            audio_digest(),
            TranscriptionOptions(),
            f"recordings/{audio_digest()[:2]}/{audio_digest()}.mp3",
        )
        self.assertTrue((self.root / "requests" / f"{identifier}.json").exists())
        speech_worker.run_cycle(self.root, backend, pipeline, tag, DEFAULT_LIMITS, decode=False)
        self.assertIsNotNone(load_result(self.root, audio_digest(), TranscriptionOptions()))

    def test_worker_failure_report_is_content_free(self):
        identifier = self.queue_request()
        tiny = TranscriptionLimits(
            max_bytes=4,
            max_duration_seconds=60,
            allowed_extensions=DEFAULT_LIMITS.allowed_extensions,
        )
        backend = ScriptedBackend()
        pipeline = TranscriptionOptions(model="scripted-1")
        tag = speech_worker.worker_identity(backend, pipeline)
        completed, failed = speech_worker.run_cycle(
            self.root, backend, pipeline, tag, tiny, decode=False
        )
        self.assertEqual((completed, failed), (0, 1))
        from karpathy_wiki_ingest_audio.speech_client import load_failure

        failure = load_failure(self.root, audio_digest(), TranscriptionOptions())
        self.assertEqual(failure, "speech:processing:LimitExceededError")
        self.assertFalse((self.root / "requests" / f"{identifier}.json").exists())

    def test_malformed_reference_is_dropped(self):
        identifier = self.queue_request()
        request_path = self.root / "requests" / f"{identifier}.json"
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        payload["audio"] = "recordings/../../escape.mp3"
        request_path.write_text(json.dumps(payload), encoding="utf-8")
        completed, failed = self.run_worker()
        self.assertEqual((completed, failed), (0, 1))
        self.assertFalse((self.root / "outside").exists())

    def test_worker_identity_change_supersedes_cached_results(self):
        digest = audio_digest()
        transcripts = {digest: [Segment(0, 1000, "Original")]}
        self.queue_request()
        self.run_worker(transcripts)
        from karpathy_wiki_ingest_audio.speech_client import load_result

        first = load_result(self.root, digest, TranscriptionOptions())
        assert first is not None
        self.queue_request()
        other = ScriptedBackend({digest: [Segment(0, 1000, "Original")]})
        other_pipeline = TranscriptionOptions(model="scripted-2")
        tag = speech_worker.worker_identity(other, other_pipeline)
        speech_worker.run_cycle(self.root, other, other_pipeline, tag, DEFAULT_LIMITS, decode=False)
        second = load_result(self.root, digest, TranscriptionOptions())
        assert second is not None
        # The newest entry for the same requested options carries the new
        # worker configuration.
        self.assertEqual(second.options["model"], "scripted-2")


class RedactionProjectionTests(unittest.TestCase):
    def test_segments_without_diarization_have_no_speaker_labels(self):
        segments = [Segment(0, 2000, "plain")]
        redacted, _ = redact_transcript(segments, anonymizer())
        self.assertIsNone(redacted[0].speaker_id)


class LimitTests(unittest.TestCase):
    def test_unsupported_extension_is_rejected_content_free(self):
        with self.assertRaisesRegex(LimitExceededError, "extension"):
            DEFAULT_LIMITS.validate_path("notes.md")

    def test_bounds_must_be_positive(self):
        with self.assertRaisesRegex(LimitExceededError, "max_bytes"):
            TranscriptionLimits(
                max_bytes=0,
                max_duration_seconds=60,
                allowed_extensions=DEFAULT_LIMITS.allowed_extensions,
            ).validate_path("tiny.wav")

    def test_oversize_file_rejected_before_decoding(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "too-big.mp3"
            path.write_bytes(b"nonempty")
            limits = TranscriptionLimits(
                max_bytes=4,
                max_duration_seconds=60,
                allowed_extensions=DEFAULT_LIMITS.allowed_extensions,
            )
            with self.assertRaisesRegex(LimitExceededError, "size"):
                from karpathy_wiki_speech.types import accept_audio

                accept_audio(str(path), limits)


class InventoryTests(unittest.TestCase):
    def test_only_supported_audio_extensions_are_discovered(self):
        with tempfile.TemporaryDirectory() as directory:
            incoming = Path(directory)
            write_recording(incoming, "Meetings/kickoff.mp3")
            write_recording(incoming, "Meetings/notes.md", b"")
            write_recording(incoming, "Meetings/huge.production", b"")
            inventory = audio_inventory(incoming, DEFAULT_LIMITS)
            self.assertEqual(list(inventory), ["Meetings/kickoff.mp3"])
            self.assertRegex(inventory["Meetings/kickoff.mp3"], r"^[0-9a-f]{64}$")


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.mapping_dir = Path(self.directory.name) / "identity"

    def test_identity_is_persistent_and_opaque(self):
        first = resolve_identity(self.mapping_dir, "a.mp3")
        self.assertEqual(first, resolve_identity(self.mapping_dir, "a.mp3"))
        self.assertEqual(load_mapping(self.mapping_dir).get("a.mp3"), first)
        self.assertRegex(first, r"^[0-9a-f]{32}$")
        self.assertNotEqual(first, resolve_identity(self.mapping_dir, "b.mp3"))

    def test_identical_audio_at_two_paths_keeps_distinct_identities(self):
        payload = b"identical"
        write_recording(self.mapping_dir.parent, "deep/a.mp3", payload)
        write_recording(self.mapping_dir.parent, "other/b.mp3", payload)
        first = resolve_identity(self.mapping_dir, "deep/a.mp3")
        second = resolve_identity(self.mapping_dir, "other/b.mp3")
        self.assertNotEqual(first, second)


class SettingsTests(unittest.TestCase):
    def test_defaults_require_only_the_shared_redactions_secret(self):
        with mock.patch.dict(os.environ, {"REDACTIONS_FILE": "/redactions.json"}, clear=True):
            environment = Settings.from_env()
        self.assertEqual(environment.incoming, Path("/data/incoming/audio"))
        self.assertEqual(environment.sanitized, Path("/data/sanitized/audio"))
        self.assertEqual(environment.quarantine, Path("/data/quarantine/audio"))
        self.assertEqual(environment.mapping, Path("/data/state/audio-identity"))
        self.assertEqual(environment.speech_root, Path("/data/speech"))
        self.assertEqual(environment.speech_timeout, 43200)
        self.assertEqual(environment.interval, 3600)
        self.assertIsNone(environment.state_path)

    def test_missing_redactions_configuration_rejected(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "REDACTIONS_FILE is required"):
                Settings.from_env()

    def test_environment_overrides(self):
        with mock.patch.dict(
            os.environ,
            {
                "REDACTIONS_FILE": "/r.json",
                "AUDIO_INCOMING_ROOT": "/i",
                "AUDIO_SANITIZED_ROOT": "/s",
                "AUDIO_QUARANTINE_ROOT": "/q",
                "AUDIO_MAPPING_ROOT": "/m",
                "SPEECH_ROOT": "/sp",
                "SPEECH_TIMEOUT_SEC": "600",
                "AUDIO_SYNC_INTERVAL": "90s",
                "INGEST_STATE_PATH": "/state/ingest.sqlite3",
            },
            clear=True,
        ):
            environment = Settings.from_env()
        self.assertEqual(
            (
                environment.incoming,
                environment.sanitized,
                environment.quarantine,
                environment.mapping,
                environment.speech_root,
                environment.speech_timeout,
                environment.interval,
                environment.health_path,
                environment.state_path,
            ),
            (
                Path("/i"),
                Path("/s"),
                Path("/q"),
                Path("/m"),
                Path("/sp"),
                600,
                90,
                Path("/tmp/health.json"),
                Path("/state/ingest.sqlite3"),
            ),
        )


class CycleHarness(unittest.TestCase):
    """Cycles against the scripted backend inside an in-process fake worker."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.incoming = self.root / "incoming"
        self.sanitized = self.root / "sanitized"
        self.quarantine = self.root / "quarantine"
        self.mapping_dir = self.root / "state" / "identity"
        self.speech_root = self.root / "speech"
        self.incoming.mkdir()
        self.store = StateStore.open(self.root / "ingest.sqlite3")
        self.addCleanup(self.store.close)
        digest = audio_digest()
        self.transcripts: dict[str, list[Segment]] = {
            digest: [
                Segment(0, 1000, "Ich treffe Max"),
                Segment(1000, 2000, "Mustermann im Büro"),
            ]
        }
        self.pipeline = TranscriptionOptions(model="scripted-1")
        self.announce_worker()

        def instant_worker(
            speech_root: Path,
            audio_sha256: str,
            options: TranscriptionOptions,
            timeout_seconds: int,
            poll_seconds: int = 5,
        ) -> TranscriptionResult:
            backend = ScriptedBackend(self.transcripts)
            pipeline = self.pipeline
            worker_tag = speech_worker.worker_identity(backend, pipeline)
            speech_worker.run_cycle(
                speech_root, backend, pipeline, worker_tag, DEFAULT_LIMITS, decode=False
            )
            from karpathy_wiki_ingest_audio.speech_client import await_result

            return await_result(speech_root, audio_sha256, options, timeout_seconds, poll_seconds)

        worker = mock.patch(
            "karpathy_wiki_ingest_audio.publisher.await_result",
            side_effect=instant_worker,
        )
        worker.start()
        self.addCleanup(worker.stop)

    def announce_worker(self):
        backend = ScriptedBackend(self.transcripts)
        tag = speech_worker.worker_identity(backend, self.pipeline)
        speech_worker.run_cycle(
            self.speech_root, backend, self.pipeline, tag, DEFAULT_LIMITS, decode=False
        )

    def cycle(
        self,
        anonymizer_instance: TargetedAnonymizer | None = None,
        options: TranscriptionOptions | None = None,
        limits: TranscriptionLimits = DEFAULT_LIMITS,
        store: Any = "default",
        now: int | None = None,
    ) -> tuple[int, int, bool]:
        return process_cycle(
            self.incoming,
            self.sanitized,
            self.quarantine,
            self.mapping_dir,
            self.speech_root,
            anonymizer_instance or anonymizer(),
            options or TranscriptionOptions(),
            load_mapping(self.mapping_dir),
            limits,
            60,
            self.store if store == "default" else store,
            interval=60,
            now=now,
        )

    def active(self) -> Path:
        return self.sanitized / os.readlink(self.sanitized / ACTIVE_SYMLINK)

    def manifest(self) -> dict[str, Any]:
        return validate_manifest(
            json.loads((self.sanitized / "manifest.json").read_text(encoding="utf-8")),
            SOURCE_NAME,
        )

    def page(self) -> str:
        sources = sorted((self.active() / "recordings").glob("*.md"))
        self.assertTrue(sources, "no published recording")
        return sources[-1].read_text(encoding="utf-8")


class DaemonSchedulingTests(CycleHarness):
    def test_short_interval_waits_for_transcription_and_reuses_completed_work(self):
        from karpathy_wiki_ingest_audio import publisher

        redactions = self.root / "redactions.json"
        redactions.write_text(json.dumps(ANONYMIZER_CONFIGURATION), encoding="utf-8")
        clock = 1000
        sync_times: list[int] = []
        wait_times: list[int] = []
        stop_event = mock.Mock()
        stop_event.is_set.return_value = False
        instant_worker = publisher.await_result

        def synchronize(incoming, _remote):
            sync_times.append(clock)
            write_recording(incoming, "meeting.mp3")

        def slow_worker(*args, **kwargs):
            nonlocal clock
            # More than two sync intervals pass before the worker answers.
            clock += 125
            self.assertEqual(sync_times, [1000])
            self.assertEqual(wait_times, [])
            return instant_worker(*args, **kwargs)

        def wait(interval):
            nonlocal clock
            self.assertEqual(interval, 60)
            self.assertTrue((self.sanitized / ACTIVE_SYMLINK).is_symlink())
            wait_times.append(clock)
            clock += interval
            return len(wait_times) == 2

        stop_event.wait.side_effect = wait
        with (
            mock.patch.dict(
                os.environ,
                {
                    "REDACTIONS_FILE": str(redactions),
                    "AUDIO_INCOMING_ROOT": str(self.incoming),
                    "AUDIO_SANITIZED_ROOT": str(self.sanitized),
                    "AUDIO_QUARANTINE_ROOT": str(self.quarantine),
                    "AUDIO_MAPPING_ROOT": str(self.mapping_dir),
                    "SPEECH_ROOT": str(self.speech_root),
                    "HEALTH_PATH": str(self.root / "health.json"),
                    "AUDIO_SYNC_INTERVAL": "1m",
                    "INGEST_STATE_PATH": str(self.root / "ingest.sqlite3"),
                    "WEBDAV_PATH": "Recordings",
                },
                clear=True,
            ),
            mock.patch.object(publisher, "install_stop_handler", return_value=stop_event),
            mock.patch.object(publisher, "synchronize", side_effect=synchronize),
            mock.patch.object(publisher, "await_result", side_effect=slow_worker) as speech,
            mock.patch.object(publisher.time, "time", side_effect=lambda: clock),
        ):
            publisher.run(once=False)

        self.assertEqual(sync_times, [1000, 1185])
        self.assertEqual(wait_times, [1125, 1185])
        speech.assert_called_once()
        self.assertEqual(self.store.metrics()["jobs"][JOB_SUCCEEDED], 1)
        self.assertEqual(self.store.metrics()["source_generations"], 1)
        self.assertEqual(
            json.loads((self.root / "health.json").read_text(encoding="utf-8"))["failed"], 0
        )


class PublicationTests(CycleHarness):
    def test_fully_redacted_segment_keeps_timestamp_without_diarization(self):
        self.transcripts[audio_digest()] = [
            Segment(0, 1000, "Max"),
            Segment(1000, 2000, "Mustermann"),
        ]
        write_recording(self.incoming, "meeting.mp3")
        self.assertEqual(self.cycle(), (1, 0, False))
        self.assertIn("[00:00:01-00:00:02]\n", self.page())

    def test_publishes_redacted_recording_with_provenance(self):
        write_recording(self.incoming, "Meetings/kickoff.mp3")
        self.assertEqual(self.cycle(), (1, 0, False))

        manifest = self.manifest()
        self.assertEqual(manifest["source"], "audio")
        self.assertEqual(manifest["wiki_root"], "audio")
        self.assertEqual(len(manifest["items"]), 1)
        item = manifest["items"][0]
        self.assertEqual(item["claim"], {"audio_source_id": item["source_key"]})
        self.assertEqual(item["wiki_path"].split("/")[-2], f"recording-{item['source_key']}")
        published = Path(self.sanitized / item["source_path"]).read_text(encoding="utf-8")
        self.assertIn("[ICH]", published)
        self.assertNotIn("Max Mustermann", published)
        frontmatter = item["frontmatter"]
        self.assertEqual(frontmatter["source_adapter"], "audio")
        self.assertEqual(frontmatter["source_path"], "Meetings/kickoff.mp3")
        self.assertEqual(frontmatter["source_revision"], item["source_revision"])
        self.assertEqual(
            frontmatter,
            {
                "source_adapter": "audio",
                "source_path": "Meetings/kickoff.mp3",
                "source_revision": item["source_revision"],
            },
        )

    def test_machine_generated_and_provenance_are_recorded_in_the_document(self):
        write_recording(self.incoming, "Meetings/kickoff.mp3")
        self.cycle()
        published = self.page()
        self.assertIn("source_audio_sha256:", published)
        self.assertIn("machine_generated: true", published)
        self.assertIn('speech_backend: "scripted"', published)
        self.assertIn('source_origin: "Meetings/kickoff.mp3"', published)
        self.assertIn("[00:00:00-00:00:01]", published)
        self.assertIn("maschinengeneriert", published)

    def test_sensitive_recording_names_are_redacted_in_public_metadata(self):
        write_recording(self.incoming, "Meetings/Max Mustermann Besprechung.mp3")
        self.assertEqual(self.cycle(), (1, 0, False))
        manifest = json.dumps(self.manifest(), ensure_ascii=False)
        self.assertNotIn("Max Mustermann", manifest)
        self.assertIn("[ICH]", manifest)
        for path in self.sanitized.rglob("*"):
            if path.is_file():
                self.assertNotIn("Max Mustermann", path.read_text(), str(path))
        generation = self.active()
        self.assertEqual(self.cycle(), (0, 0, False))
        self.assertEqual(self.active(), generation)

    def test_multiword_literal_split_across_segments_never_publishes_it(self):
        write_recording(self.incoming, "Meetings/kickoff.mp3")
        self.assertEqual(self.cycle(), (1, 0, False))
        published = self.page()
        self.assertNotIn("Max", published)
        self.assertNotIn("Mustermann", published)
        self.assertEqual(published.count("[ICH]"), 1)
        self.assertIn("[00:00:00-00:00:01]", published)
        self.assertIn("[00:00:01-00:00:02]", published)

    def test_identical_content_is_republished_idempotently_by_hash(self):
        write_recording(self.incoming, "Meetings/kickoff.mp3")
        self.assertEqual(self.cycle(), (1, 0, False))
        first_generation = os.readlink(self.sanitized / ACTIVE_SYMLINK)
        self.assertEqual(self.cycle(), (0, 0, False))
        self.assertEqual(os.readlink(self.sanitized / ACTIVE_SYMLINK), first_generation)

    def test_speaker_labels_follow_the_diarize_option(self):
        write_recording(self.incoming, "Meetings/kickoff.mp3")
        self.assertEqual(self.cycle(options=TranscriptionOptions(diarize=True)), (1, 0, False))
        published = self.page()
        self.assertIn("Sprecher 1: ", published)
        self.assertIn("Sprecher 2: ", published)
        self.assertNotIn("Unbekannt", published)

    def test_segments_without_diarization_render_no_invented_labels(self):
        write_recording(self.incoming, "Meetings/kickoff.mp3")
        self.assertEqual(self.cycle(), (1, 0, False))
        published = self.page()
        self.assertFalse(
            any(line.startswith("[") and "Sprecher" in line for line in published.splitlines()),
            published,
        )
        self.assertNotIn("Unbekannt", published)


class IdentityStabilityTests(CycleHarness):
    def test_redaction_rotation_keeps_source_identity_and_republishes(self):
        write_recording(self.incoming, "Meetings/kickoff.mp3")
        self.cycle(anonymizer_instance=anonymizer())
        rotated = TargetedAnonymizer.from_config(
            {"people": [{"replacement": "[AUTOR]", "values": ["Max Mustermann"]}]}
        )
        self.assertEqual(self.cycle(anonymizer_instance=rotated), (1, 0, False))
        manifest = self.manifest()
        key = manifest["items"][0]["source_key"]
        published = self.page()
        self.assertIn("[AUTOR]", published)
        self.assertNotIn("[ICH]", published)
        self.assertEqual(load_mapping(self.mapping_dir)["Meetings/kickoff.mp3"], key)

    def test_renamed_recording_creates_a_new_source_and_revokes(self):
        write_recording(self.incoming, "Meetings/kickoff.mp3")
        self.cycle()
        old_key = self.manifest()["items"][0]["source_key"]
        payload = (self.incoming / "Meetings/kickoff.mp3").read_bytes()
        (self.incoming / "Meetings/kickoff.mp3").unlink()
        write_recording(self.incoming, "Meetings/kickoff-renamed.mp3", payload)
        self.assertEqual(self.cycle(), (1, 0, False))
        manifest = self.manifest()
        self.assertEqual(len(manifest["items"]), 1)
        self.assertNotEqual(manifest["items"][0]["source_key"], old_key)
        self.assertEqual(
            manifest["revoked"],
            [{"source_key": old_key, "claim": {"audio_source_id": old_key}}],
        )

    def test_identical_audio_at_two_paths_publishes_two_distinct_sources(self):
        payload = b"identical-recording"
        write_recording(self.incoming, "Meetings/a.mp3", payload)
        write_recording(self.incoming, "Other/b.mp3", payload)
        self.assertEqual(self.cycle(), (2, 0, False))
        keys = [item["source_key"] for item in self.manifest()["items"]]
        self.assertEqual(len(keys), 2)
        self.assertNotEqual(keys[0], keys[1])

    def test_upstream_removal_is_recorded_as_revocation(self):
        write_recording(self.incoming, "Meetings/kickoff.mp3")
        self.cycle()
        (self.incoming / "Meetings/kickoff.mp3").unlink()
        self.assertEqual(self.cycle(), (0, 0, False))
        manifest = self.manifest()
        self.assertEqual(manifest["items"], [])
        self.assertEqual(len(manifest["revoked"]), 1)

    def test_distinct_paths_that_sanitize_to_one_title_stay_distinct(self):
        write_recording(self.incoming, "Founders/Besprechung.mp3")
        write_recording(self.incoming, "Team/Besprechung.mp3")
        self.assertEqual(self.cycle(), (2, 0, False))
        claims = {item["claim"]["audio_source_id"] for item in self.manifest()["items"]}
        self.assertEqual(len(claims), 2)

    def test_revocations_survive_later_generations_until_source_returns(self):
        write_recording(self.incoming, "a.mp3")
        self.cycle()
        first_key = self.manifest()["items"][0]["source_key"]
        (self.incoming / "a.mp3").unlink()
        self.cycle()
        tombstone = {"source_key": first_key, "claim": {"audio_source_id": first_key}}
        self.assertEqual(self.manifest()["revoked"], [tombstone])
        write_recording(self.incoming, "b.mp3")
        self.cycle()
        self.assertEqual(self.manifest()["revoked"], [tombstone])
        second_key = self.manifest()["items"][0]["source_key"]
        (self.incoming / "b.mp3").unlink()
        write_recording(self.incoming, "c.mp3")
        self.cycle()
        self.assertEqual(
            {entry["source_key"] for entry in self.manifest()["revoked"]},
            {first_key, second_key},
        )
        write_recording(self.incoming, "a.mp3")
        self.cycle()
        self.assertIn(first_key, {entry["source_key"] for entry in self.manifest()["items"]})
        self.assertEqual(
            self.manifest()["revoked"],
            [{"source_key": second_key, "claim": {"audio_source_id": second_key}}],
        )


class FailureTests(CycleHarness):
    def test_oversized_recording_fails_content_free_and_keeps_previous_generation(self):
        write_recording(self.incoming, "Meetings/ok.mp3")
        self.assertEqual(self.cycle(), (1, 0, False))
        previous = os.readlink(self.sanitized / ACTIVE_SYMLINK)
        previous_manifest = self.manifest()
        write_recording(self.incoming, "Meetings/huge.mp3", b"x" * 4096)
        tiny = TranscriptionLimits(
            max_bytes=4,
            max_duration_seconds=60,
            allowed_extensions=DEFAULT_LIMITS.allowed_extensions,
        )
        self.assertEqual(self.cycle(limits=tiny), (0, 1, False))
        # All-or-nothing: the previous successful generation stays active.
        self.assertEqual(os.readlink(self.sanitized / ACTIVE_SYMLINK), previous)
        self.assertEqual(self.manifest(), previous_manifest)
        reports = sorted(self.quarantine.glob("*.error"))
        self.assertEqual(len(reports), 1)
        report = reports[0].read_text(encoding="utf-8")
        self.assertIn("error_type=LimitExceededError", report)
        self.assertIn("source_key=", report)
        self.assertNotIn("huge.mp3", report)

    def test_unsupported_extension_is_ignored_entirely(self):
        write_recording(self.incoming, "Meetings/notes.md", b"")
        self.assertEqual(self.cycle(), (0, 0, False))
        manifest = self.manifest()
        self.assertEqual(manifest["items"], [])
        self.assertEqual(manifest["errors"], [])


class WorkerLifecycleTests(unittest.TestCase):
    """The speech worker daemon: env selection, run loop, health, CLI flags."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def test_worker_backend_selection(self):
        with mock.patch.dict(os.environ, {"SPEECH_BACKEND": "fake"}, clear=True):
            self.assertIsInstance(speech_worker.worker_backend(), FakeBackend)
        with mock.patch.dict(os.environ, {"SPEECH_BACKEND": "nonsense"}, clear=True):
            with self.assertRaisesRegex(ValueError, "unsupported speech backend"):
                speech_worker.worker_backend()

    def test_run_once_processes_queue_and_writes_health(self):
        from karpathy_wiki_ingest_audio.speech_client import load_result

        digest = audio_digest()
        recordings = self.root / "recordings" / digest[:2]
        recordings.mkdir(parents=True)
        (recordings / f"{digest}.mp3").write_bytes(DEFAULT_RECORDING)
        (self.root / "requests").mkdir()
        options = {"language": None, "diarize": False}
        from karpathy_wiki_speech.cache import options_key, request_id

        identifier = request_id(digest, options_key(options))
        (self.root / "requests" / f"{identifier}.json").write_text(
            json.dumps(
                {
                    "audio": f"recordings/{digest[:2]}/{digest}.mp3",
                    "audio_sha256": digest,
                    "options": options,
                }
            ),
            encoding="utf-8",
        )
        with (
            mock.patch.dict(os.environ, {"SPEECH_BACKEND": "fake"}, clear=True),
            mock.patch.dict(os.environ, {"SPEECH_POLL_SECONDS": "1"}),
        ):
            speech_worker.run(self.root, once=True)
        self.assertTrue((self.root / "worker-health.json").is_file())
        self.assertIsNotNone(load_result(self.root, digest, TranscriptionOptions()))
        self.assertEqual(list((self.root / "requests").glob("*.json")), [])

    def test_health_check_flag_exits_nonzero_on_failure_record(self):
        from karpathy_wiki_ingest.shared import write_health

        healthy = self.root / "healthy.json"
        failing = self.root / "failing.json"
        missing = self.root / "missing.json"
        write_health(healthy, 0)
        write_health(failing, 3)
        speech_worker.health_check(healthy)  # must not raise
        with self.assertRaises(SystemExit):
            speech_worker.health_check(failing)
        with self.assertRaises(SystemExit):
            speech_worker.health_check(missing)

    def test_decode_without_av_reports_content_free_limit_error(self):
        from karpathy_wiki_speech.types import decode_audio

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tiny.mp3"
            path.write_bytes(b"nonempty")
            with mock.patch.dict(sys.modules, {"av": None}):
                with self.assertRaisesRegex(LimitExceededError, "decoded"):
                    decode_audio(str(path), DEFAULT_LIMITS)

    def test_worker_identity_is_stable_and_config_sensitive(self):
        backend = FakeBackend()
        base = speech_worker.worker_identity(backend, TranscriptionOptions())
        self.assertEqual(base, speech_worker.worker_identity(backend, TranscriptionOptions()))
        self.assertNotEqual(
            base, speech_worker.worker_identity(backend, TranscriptionOptions(model="other"))
        )


class SpeechClientTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.digest = audio_digest()

    def options(self, **overrides) -> TranscriptionOptions:
        return TranscriptionOptions(**overrides)

    def test_await_result_times_out_content_free(self):
        from karpathy_wiki_ingest_audio.speech_client import SpeechTimeoutError, await_result

        with self.assertRaisesRegex(SpeechTimeoutError, "speech worker"):
            await_result(self.root, self.digest, self.options(), timeout_seconds=0, poll_seconds=0)

    def test_failure_report_raises_content_free(self):
        from karpathy_wiki_ingest_audio.speech_client import (
            SpeechProcessingError,
            await_result,
        )
        from karpathy_wiki_speech.cache import failure_path, options_key

        key = options_key({"language": None, "diarize": False})
        destination = failure_path(self.root, self.digest, key)
        destination.parent.mkdir(parents=True)
        destination.write_text(json.dumps({"error": "speech:processing:OSError"}), encoding="utf-8")
        with self.assertRaisesRegex(SpeechProcessingError, "OSError"):
            await_result(
                self.root,
                self.digest,
                self.options(),
                timeout_seconds=0,
                poll_seconds=0,
            )

    def test_ensure_requested_is_idempotent(self):
        from karpathy_wiki_ingest_audio.speech_client import ensure_requested

        ensure_requested(self.root, self.digest, self.options(), "recordings/x/x.mp3")
        first = list((self.root / "requests").glob("*.json"))
        self.assertEqual(len(first), 1)
        ensure_requested(self.root, self.digest, self.options(), "recordings/x/x.mp3")
        self.assertEqual(list((self.root / "requests").glob("*.json")), first)

    def test_parse_result_payload_rejects_incomplete_results(self):
        from karpathy_wiki_speech.cache import parse_result_payload

        self.assertIsNone(parse_result_payload(None))
        self.assertIsNone(parse_result_payload({"backend": "b"}))
        self.assertIsNone(
            parse_result_payload(
                {
                    "backend": "b",
                    "model": "m",
                    "segments": [{"start_ms": "x", "end_ms": 1, "text": "t"}],
                }
            )
        )


class PublisherOptionsTests(unittest.TestCase):
    def test_speech_options_and_limits_parse_environment(self):
        from karpathy_wiki_ingest_audio.publisher import speech_limits, speech_options

        with mock.patch.dict(os.environ, {}, clear=True):
            options = speech_options()
            limits = speech_limits()
        self.assertEqual(options, TranscriptionOptions())
        self.assertEqual(limits.max_bytes, DEFAULT_LIMITS.max_bytes)

        with mock.patch.dict(
            os.environ,
            {
                "AUDIO_LANGUAGE": "de",
                "AUDIO_DIARIZE": "1",
                "AUDIO_MAX_BYTES": "42",
                "AUDIO_MAX_DURATION_SECONDS": "",
            },
            clear=True,
        ):
            options = speech_options()
            limits = speech_limits()
        self.assertEqual(options.language, "de")
        self.assertTrue(options.diarize)
        self.assertEqual(limits.max_bytes, 42)
        self.assertEqual(limits.max_duration_seconds, DEFAULT_LIMITS.max_duration_seconds)


class DurableCycleTests(CycleHarness):
    def test_worker_change_during_publication_keeps_previous_generation(self):
        from karpathy_wiki_ingest_audio import publisher

        write_recording(self.incoming, "meeting.mp3")
        self.cycle()
        previous = self.active()
        original = publisher.sanitize_into_generation

        def change_worker(*args, **kwargs):
            result = original(*args, **kwargs)
            self.pipeline = TranscriptionOptions(model="changed-during-publication")
            self.announce_worker()
            return result

        with mock.patch.object(publisher, "sanitize_into_generation", side_effect=change_worker):
            with self.assertRaisesRegex(RuntimeError, "worker changed"):
                self.cycle(options=TranscriptionOptions(language="en"))
        self.assertEqual(self.active(), previous)
        self.assertEqual(self.cycle(), (1, 0, False))

    def test_worker_change_republishes_unchanged_inventory(self):
        write_recording(self.incoming, "meeting.mp3")
        self.assertEqual(self.cycle(), (1, 0, False))
        first = self.active()
        self.pipeline = TranscriptionOptions(model="scripted-2")
        self.transcripts[audio_digest()] = [Segment(0, 1000, "Updated transcript")]
        self.announce_worker()
        self.assertEqual(self.cycle(), (1, 0, False))
        self.assertNotEqual(self.active(), first)
        self.assertIn("Updated transcript", self.page())
        self.assertEqual(self.cycle(), (0, 0, False))

    def test_cycle_is_a_durable_job_keyed_by_inventory_and_speech_identity(self):
        write_recording(self.incoming, "Meetings/kickoff.mp3")
        self.assertEqual(self.cycle(now=1000), (1, 0, False))
        inventory = audio_inventory(self.incoming, DEFAULT_LIMITS)
        from karpathy_wiki_ingest_audio.publisher import requested_options_identity

        speech_identity = requested_options_identity(TranscriptionOptions(), self.speech_root)
        key = cycle_idempotency_key(anonymizer().fingerprint, inventory, speech_identity)
        job = self.store.job_by_idempotency_key(key)
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.state, JOB_SUCCEEDED)
        self.assertEqual(self.cycle(now=1100), (0, 0, False))

    def test_state_metrics_record_one_generation_per_cycle(self):
        write_recording(self.incoming, "Meetings/kickoff.mp3")
        self.cycle()
        metrics = self.store.metrics()
        self.assertEqual(metrics["jobs"][JOB_SUCCEEDED], 1)
        self.assertEqual(metrics["source_generations"], 1)


if __name__ == "__main__":
    unittest.main()
