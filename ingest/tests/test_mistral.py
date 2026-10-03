"""Mocked API tests for the opt-in hosted Mistral transcription backend.

Everything runs against a scripted transport: no network access, no real
recording, no credential, and no private URL is required or committed. A
separate opt-in smoke test talks to the real service with a short recording
the operator supplies at run time.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from collections.abc import Iterable
from pathlib import Path
from unittest import mock

from karpathy_wiki_ingest.shared import TargetedAnonymizer
from karpathy_wiki_ingest_audio import publisher, speech_client
from karpathy_wiki_ingest_audio.redaction import redact_transcript
from karpathy_wiki_speech import cache
from karpathy_wiki_speech import worker as speech_worker
from karpathy_wiki_speech.backends import FakeBackend
from karpathy_wiki_speech.mistral import (
    DEFAULT_BASE_URL,
    DEFAULT_MODEL,
    MULTIPART_CHUNK_BYTES,
    SERVICE_MAX_BYTES,
    SERVICE_MAX_DURATION_SECONDS,
    DiarizationUnavailableError,
    HostedAuthenticationError,
    HostedConnectionError,
    HostedInputLimitError,
    HostedRateLimitError,
    HostedRequestError,
    HostedResponseError,
    HostedServiceError,
    HostedTimeoutError,
    MistralBackend,
    MultipartBody,
    TimestampLanguageConflictError,
    parse_transcription,
)
from karpathy_wiki_speech.types import (
    DEFAULT_LIMITS,
    DecodedAudio,
    TranscriptionOptions,
    file_revision,
)

API_KEY = "sk-test-secret-value"
RECORDING = b"synthetic-audio-bytes"

Answer = tuple[int, dict[str, str], bytes]


def decoded(duration_seconds: float = 2.0, input_bytes: int = 128) -> DecodedAudio:
    return DecodedAudio(
        duration_seconds=duration_seconds, input_bytes=input_bytes, format_name="mp3"
    )


def transcript(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "model": "voxtral-mini-2602",
        "text": "Guten Morgen.",
        "language": "de",
        "segments": [
            {"type": "transcription_segment", "text": "Guten", "start": 0.0, "end": 0.6},
            {"type": "transcription_segment", "text": "Morgen.", "start": 0.6, "end": 1.25},
        ],
    }
    payload.update(overrides)
    return payload


def answered(payload: object, status: int = 200, headers: dict[str, str] | None = None) -> Answer:
    return (status, dict(headers or {}), json.dumps(payload).encode("utf-8"))


class FakeTransport:
    """Scripted transport: records every call and replays scripted answers.

    Entries are ``(status, headers, body)`` tuples or exception instances to
    raise; the last entry repeats once the script is exhausted. The streamed
    request body is drained and joined per call, exactly as a real send would
    consume it.
    """

    def __init__(self, *script: object) -> None:
        self.script: list[object] = list(script) if script else [answered(transcript())]
        self.calls: list[tuple[str, dict[str, str], bytes, float]] = []

    def __call__(
        self, url: str, headers: dict[str, str], body: Iterable[bytes], timeout: float
    ) -> Answer:
        self.calls.append((url, dict(headers), b"".join(body), timeout))
        entry = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(entry, BaseException):
            raise entry
        assert isinstance(entry, tuple)
        return entry

    @property
    def body(self) -> bytes:
        self.assert_called()
        return self.calls[-1][2]

    @property
    def url(self) -> str:
        self.assert_called()
        return self.calls[-1][0]

    @property
    def headers(self) -> dict[str, str]:
        self.assert_called()
        return self.calls[-1][1]

    def assert_called(self) -> None:
        if not self.calls:
            raise AssertionError("the transport was never called")


def make_backend(
    transport: FakeTransport,
    sleeps: list[float],
    *,
    api_key: str = API_KEY,
    max_attempts: int = 4,
    max_bytes: int = SERVICE_MAX_BYTES,
    max_duration_seconds: int = SERVICE_MAX_DURATION_SECONDS,
) -> MistralBackend:
    return MistralBackend(
        api_key=api_key,
        transport=transport,
        sleep=sleeps.append,
        max_attempts=max_attempts,
        max_bytes=max_bytes,
        max_duration_seconds=max_duration_seconds,
    )


def body_text(transport: FakeTransport) -> str:
    return transport.body.decode("utf-8", errors="replace")


class RecordingCase(unittest.TestCase):
    """One private staged recording on disk, transcribed through a backend."""

    def setUp(self):
        self.sleeps: list[float] = []
        self.transport = FakeTransport()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "recording.mp3"
        self.path.write_bytes(RECORDING)

    def transcribe(
        self,
        backend: MistralBackend,
        options: TranscriptionOptions | None = None,
        audio: DecodedAudio | None = None,
        path: Path | None = None,
    ):
        return backend.transcribe(
            str(path or self.path), audio or decoded(), options or TranscriptionOptions()
        )


class RequestConstructionTests(RecordingCase):
    def setUp(self):
        super().setUp()
        self.backend = make_backend(self.transport, self.sleeps)

    def test_request_targets_the_transcription_endpoint_with_bearer_auth(self):
        self.transcribe(self.backend)
        self.assertEqual(self.transport.url, f"{DEFAULT_BASE_URL}/audio/transcriptions")
        self.assertEqual(self.transport.headers["Authorization"], f"Bearer {API_KEY}")
        self.assertEqual(self.transport.headers["Accept"], "application/json")
        self.assertTrue(
            self.transport.headers["Content-Type"].startswith("multipart/form-data; boundary=")
        )

    def test_request_uploads_the_audio_bytes_under_a_generic_file_name(self):
        self.transcribe(self.backend)
        body = body_text(self.transport)
        self.assertIn('name="file"; filename="audio.mp3"', body)
        self.assertIn(RECORDING.decode("utf-8"), body)
        self.assertIn("Content-Type: audio/mpeg", body)
        self.assertEqual(self.transport.headers["Content-Length"], str(len(self.transport.body)))

    def test_recording_is_streamed_in_blocks_and_never_buffered_whole(self):
        payload = b"x" * (3 * MULTIPART_CHUNK_BYTES + 7)
        path = Path(self.directory.name) / "large.mp3"
        path.write_bytes(payload)
        upload = MultipartBody.for_recording(
            str(path), model=DEFAULT_MODEL, diarize=False, boundary="BOUND"
        )
        blocks = list(upload.chunks())
        self.assertEqual(sum(len(block) for block in blocks), upload.length())
        self.assertLess(max(len(block) for block in blocks), MULTIPART_CHUNK_BYTES + 256)
        self.assertGreater(len(blocks), 4)
        body = b"".join(blocks)
        self.assertIn(b'filename="audio.mp3"', body)
        self.assertNotIn(str(path).encode(), body)

    def test_request_carries_no_file_name_or_provider_metadata(self):
        path = Path(self.directory.name) / "Team Sync - Max Mustermann.mp3"
        path.write_bytes(RECORDING)
        self.transcribe(self.backend, path=path)
        body = body_text(self.transport)
        self.assertNotIn("Mustermann", body)
        self.assertNotIn("Team Sync", body)
        self.assertNotIn(path.name, body)
        self.assertNotIn(str(path), body)

    def test_request_always_asks_for_segment_timestamps(self):
        self.transcribe(self.backend)
        body = body_text(self.transport)
        self.assertIn('name="timestamp_granularities"', body)
        self.assertIn("segment", body)

    def test_diarization_flag_is_sent_only_when_requested(self):
        self.transcribe(self.backend, TranscriptionOptions(diarize=False))
        self.assertNotIn('name="diarize"', body_text(self.transport))
        self.transport.script = [
            answered(
                transcript(
                    text="",
                    segments=[{"text": "a", "start": 0, "end": 1, "speaker_id": "speaker_0"}],
                )
            )
        ]
        self.transcribe(self.backend, TranscriptionOptions(diarize=True))
        self.assertIn('name="diarize"\r\n\r\ntrue', body_text(self.transport))

    def test_api_key_stays_out_of_body_url_and_identity(self):
        self.transcribe(self.backend)
        body = body_text(self.transport)
        self.assertNotIn(API_KEY, body)
        self.assertNotIn(API_KEY, self.transport.url)
        self.assertNotIn(API_KEY, json.dumps(self.backend.describe(TranscriptionOptions())))
        other = make_backend(self.transport, self.sleeps, api_key="sk-other-secret-value")
        self.assertEqual(
            speech_worker.worker_identity(self.backend, TranscriptionOptions()),
            speech_worker.worker_identity(other, TranscriptionOptions()),
        )

    def test_model_selection_is_sent_and_reported(self):
        self.transcribe(self.backend, TranscriptionOptions(model="voxtral-mini-2602"))
        self.assertIn("voxtral-mini-2602", body_text(self.transport))
        described = self.backend.describe(TranscriptionOptions(model="voxtral-mini-2602"))
        self.assertEqual(described["model"], "voxtral-mini-2602")
        self.assertEqual(self.backend.describe(TranscriptionOptions())["model"], DEFAULT_MODEL)


class TimestampConversionTests(unittest.TestCase):
    def test_seconds_are_converted_to_whole_milliseconds(self):
        segments, language, model = parse_transcription(
            transcript(
                segments=[
                    {"text": "eins", "start": 0.0, "end": 1.234},
                    {"text": "zwei", "start": 1.5, "end": 12.3456},
                ]
            ),
            diarize=False,
        )
        self.assertEqual(
            [(segment.start_ms, segment.end_ms) for segment in segments],
            [(0, 1234), (1500, 12346)],
        )
        self.assertEqual([segment.text for segment in segments], ["eins", "zwei"])
        self.assertEqual(language, "de")
        self.assertEqual(model, "voxtral-mini-2602")

    def test_segment_text_is_collapsed_and_trimmed(self):
        segments, _, _ = parse_transcription(
            transcript(segments=[{"text": "  Guten\n  Morgen ", "start": 0, "end": 1}]),
            diarize=False,
        )
        self.assertEqual(segments[0].text, "Guten Morgen")

    def test_missing_or_unusable_timestamps_are_rejected_content_free(self):
        broken: list[object] = [
            [{"text": "x", "start": None, "end": 1}],
            [{"text": "x", "start": 0, "end": None}],
            [{"text": "x", "start": "0", "end": 1}],
            [{"text": "x", "start": True, "end": 1}],
            [{"text": "x", "start": -1, "end": 1}],
            [{"text": "x", "start": 2, "end": 1}],
        ]
        for segments in broken:
            with self.subTest(segments=segments):
                with self.assertRaises(HostedResponseError) as caught:
                    parse_transcription(transcript(segments=segments), diarize=False)
                self.assertNotIn("Guten", str(caught.exception))


class ResponseValidationTests(RecordingCase):
    def test_empty_transcript_is_a_valid_result(self):
        segments, language, model = parse_transcription(
            transcript(text="", segments=[], language=None),
            diarize=False,
        )
        self.assertEqual(segments, [])
        self.assertIsNone(language)
        self.assertEqual(model, "voxtral-mini-2602")

    def test_missing_segment_list_with_empty_text_is_an_empty_transcript(self):
        payload = transcript(text="", segments=[])
        del payload["segments"]
        segments, _, _ = parse_transcription(payload, diarize=False)
        self.assertEqual(segments, [])

    def test_transcript_without_timed_segments_is_rejected(self):
        with self.assertRaises(HostedResponseError):
            parse_transcription(transcript(text="Guten Morgen.", segments=[]), diarize=False)

    def test_malformed_answers_are_rejected_content_free(self):
        broken: list[object] = [
            [],
            "Guten Morgen.",
            None,
            {"text": "Guten Morgen.", "model": "m", "language": "de", "segments": {}},
            {"model": "m", "language": "de", "segments": []},
            {"text": 7, "model": "m", "language": "de", "segments": []},
            {"text": "", "language": "de", "segments": []},
            {"text": "", "model": "", "language": "de", "segments": []},
            {"text": "", "model": "m\nx", "language": "de", "segments": []},
            {"text": "", "model": "x" * 200, "language": "de", "segments": []},
            {"text": "", "model": "m", "language": "", "segments": []},
            {"text": "", "model": "m", "language": 7, "segments": []},
            {"text": "", "model": "m", "language": "de", "segments": ["x"]},
            {"text": "", "model": "m", "language": "de", "segments": [{"start": 0, "end": 1}]},
            {
                "text": "",
                "model": "m",
                "language": "de",
                "segments": [{"text": 5, "start": 0, "end": 1}],
            },
        ]
        for payload in broken:
            with self.subTest(payload=payload):
                with self.assertRaises(HostedResponseError) as error:
                    parse_transcription(payload, diarize=False)
                self.assertNotIn("Guten Morgen.", str(error.exception))

    def test_unreadable_success_body_is_rejected_content_free(self):
        self.transport.script = [(200, {}, b"\xff not json")]
        with self.assertRaises(HostedResponseError):
            self.transcribe(make_backend(self.transport, self.sleeps))


class DiarizationTests(unittest.TestCase):
    def parse(self, segments: list[dict[str, object]], diarize: bool):
        return parse_transcription(transcript(text="", segments=segments), diarize=diarize)[0]

    def test_service_speakers_are_mapped_to_anonymous_ids(self):
        segments = self.parse(
            [
                {"text": "a", "start": 0, "end": 1, "speaker_id": "SPEAKER_B"},
                {"text": "b", "start": 1, "end": 2, "speaker_id": "SPEAKER_A"},
                {"text": "c", "start": 2, "end": 3, "speaker_id": "SPEAKER_B"},
            ],
            diarize=True,
        )
        self.assertEqual([segment.speaker_id for segment in segments], [0, 1, 0])
        self.assertNotIn("SPEAKER", json.dumps([segment.to_dict() for segment in segments]))

    def test_turns_without_a_speaker_are_marked_unknown(self):
        segments = self.parse(
            [
                {"text": "a", "start": 0, "end": 1, "speaker_id": "speaker_0"},
                {"text": "b", "start": 1, "end": 2},
            ],
            diarize=True,
        )
        self.assertEqual([segment.speaker_id for segment in segments], [0, -1])

    def test_diarization_request_without_any_speaker_is_rejected_content_free(self):
        with self.assertRaises(DiarizationUnavailableError):
            self.parse([{"text": "a", "start": 0, "end": 1}], diarize=True)

    def test_diarization_request_for_an_empty_transcript_stays_empty(self):
        self.assertEqual(self.parse([], diarize=True), [])

    def test_unusable_speaker_labels_are_rejected_content_free(self):
        with self.assertRaises(HostedResponseError):
            self.parse([{"text": "a", "start": 0, "end": 1, "speaker_id": 3}], diarize=True)

    def test_labels_are_ignored_without_a_diarization_request(self):
        segments = self.parse(
            [{"text": "a", "start": 0, "end": 1, "speaker_id": "speaker_0"}], diarize=False
        )
        self.assertIsNone(segments[0].speaker_id)


class LanguageConflictTests(RecordingCase):
    def test_explicit_language_is_rejected_before_any_request(self):
        backend = make_backend(self.transport, self.sleeps)
        with self.assertRaises(TimestampLanguageConflictError):
            self.transcribe(backend, TranscriptionOptions(language="de", diarize=True))
        self.assertEqual(self.transport.calls, [])

    def test_detected_language_is_reported_in_the_result(self):
        backend = make_backend(self.transport, self.sleeps)
        self.transport.script = [answered(transcript(language="de"))]
        result = self.transcribe(backend)
        self.assertEqual(result.language, "de")
        self.assertEqual(result.backend, "mistral")
        self.assertEqual(result.model, "voxtral-mini-2602")


class LimitTests(RecordingCase):
    def assert_rejected(self, backend: MistralBackend, audio: DecodedAudio) -> None:
        with self.assertRaises(HostedInputLimitError):
            self.transcribe(backend, audio=audio)
        self.assertEqual(self.transport.calls, [], "the recording must be rejected before upload")

    def test_hosted_duration_limit_is_enforced(self):
        self.assert_rejected(
            make_backend(self.transport, self.sleeps), decoded(duration_seconds=61 * 60)
        )

    def test_hosted_size_limit_is_enforced(self):
        self.assert_rejected(
            make_backend(self.transport, self.sleeps),
            decoded(input_bytes=SERVICE_MAX_BYTES + 1),
        )

    def test_hosted_format_limit_is_enforced(self):
        path = Path(self.directory.name) / "recording.m4a"
        path.write_bytes(RECORDING)
        with self.assertRaises(HostedInputLimitError):
            self.transcribe(make_backend(self.transport, self.sleeps), path=path)
        self.assertEqual(self.transport.calls, [])

    def test_limits_can_be_tightened_per_installation(self):
        self.assert_rejected(
            make_backend(self.transport, self.sleeps, max_duration_seconds=30),
            decoded(duration_seconds=31),
        )

    def test_recordings_inside_the_documented_limits_are_accepted(self):
        backend = make_backend(self.transport, self.sleeps)
        result = self.transcribe(backend, audio=decoded(duration_seconds=59 * 60))
        self.assertEqual(len(result.segments), 2)


class RetryTests(RecordingCase):
    def run_backend(self, *script: object, attempts: int = 3):
        self.transport.script = list(script)
        backend = make_backend(self.transport, self.sleeps, max_attempts=attempts)
        return self.transcribe(backend)

    def test_authentication_failure_is_not_retried(self):
        with self.assertRaises(HostedAuthenticationError):
            self.run_backend(answered({"detail": "unauthorized"}, status=401))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.sleeps, [])

    def test_rejected_requests_are_not_retried(self):
        with self.assertRaises(HostedRequestError):
            self.run_backend(answered({"detail": "bad"}, status=400))
        self.assertEqual(len(self.transport.calls), 1)

    def test_rate_limits_are_retried_with_bounded_backoff(self):
        result = self.run_backend(
            answered({"detail": "slow down"}, status=429, headers={"retry-after": "7"}),
            answered({"detail": "slow down"}, status=429),
            answered(transcript()),
        )
        self.assertEqual(len(result.segments), 2)
        self.assertEqual(len(self.transport.calls), 3)
        self.assertEqual(self.sleeps, [7.0, 4.0])

    def test_exhausted_rate_limit_reports_a_content_free_failure(self):
        with self.assertRaises(HostedRateLimitError) as caught:
            self.run_backend(answered({"detail": "slow down"}, status=429))
        self.assertEqual(len(self.transport.calls), 3)
        self.assertEqual(self.sleeps, [2.0, 4.0])
        self.assertNotIn("slow down", str(caught.exception))

    def test_timeouts_are_retried_then_reported_content_free(self):
        with self.assertRaises(HostedTimeoutError):
            self.run_backend(TimeoutError("private request line"), attempts=2)
        self.assertEqual(len(self.transport.calls), 2)

    def test_unreachable_service_is_retried_then_reported_content_free(self):
        with self.assertRaises(HostedConnectionError):
            self.run_backend(OSError("private host name"), attempts=2)
        self.assertEqual(len(self.transport.calls), 2)

    def test_server_errors_are_retried_then_reported_content_free(self):
        with self.assertRaises(HostedServiceError) as caught:
            self.run_backend(answered({"detail": "boom"}, status=503))
        self.assertEqual(len(self.transport.calls), 3)
        self.assertNotIn("boom", str(caught.exception))

    def test_server_errors_recover_when_the_service_answers(self):
        result = self.run_backend(answered({}, status=500), answered(transcript()))
        self.assertEqual(len(result.segments), 2)

    def test_malformed_success_answer_is_not_retried(self):
        with self.assertRaises(HostedResponseError):
            self.run_backend(answered({"model": "m", "text": 5}))
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(self.sleeps, [])


class Harness(unittest.TestCase):
    """Worker and publication flow against the mocked hosted service."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.incoming = self.root / "incoming"
        self.sanitized = self.root / "sanitized"
        self.quarantine = self.root / "quarantine"
        self.mapping = self.root / "state"
        self.speech_root = self.root / "speech"
        self.incoming.mkdir()
        self.snapshot = self.incoming / "meeting.mp3"
        self.snapshot.write_bytes(RECORDING)
        self.digest = file_revision(str(self.snapshot))
        self.sleeps: list[float] = []
        self.transport = FakeTransport()
        self.backend = make_backend(self.transport, self.sleeps)
        self.pipeline = TranscriptionOptions(model="voxtral-mini-2602")
        self.options = TranscriptionOptions()
        self.worker_tag = speech_worker.worker_identity(self.backend, self.pipeline)
        self.run_worker()

    def answer_with(self, *script: object) -> None:
        self.transport.script = list(script)

    def run_worker(self) -> tuple[int, int]:
        """One worker cycle over the queued requests, with no real decoding."""
        return speech_worker.run_cycle(
            self.speech_root,
            self.backend,
            self.pipeline,
            self.worker_tag,
            DEFAULT_LIMITS,
            decode=False,
        )

    def stage(self) -> str:
        return speech_client.stage_recording(
            self.speech_root, self.snapshot, self.digest, "mp3", DEFAULT_LIMITS.max_bytes
        )

    def request(self) -> None:
        reference = self.stage()
        speech_client.clear_failure(self.speech_root, self.digest, self.options)
        speech_client.ensure_requested(self.speech_root, self.digest, self.options, reference)

    def force_request(self) -> None:
        """Re-queue the same work even though a valid result already exists."""
        speech_worker.atomic_json(
            cache.request_path(
                self.speech_root,
                cache.request_id(self.digest, speech_client.result_identity(self.options)),
            ),
            {
                "audio": self.stage(),
                "audio_sha256": self.digest,
                "options": speech_client.requested_options(self.options),
            },
        )

    def publish(self, options: TranscriptionOptions | None = None) -> tuple[int, int]:
        return publisher.publish_generation(
            self.incoming,
            self.sanitized,
            self.quarantine,
            self.mapping,
            self.speech_root,
            TargetedAnonymizer.from_config(
                {"people": [{"replacement": "[ICH]", "values": ["Max Mustermann"]}]}
            ),
            options or self.options,
            {"meeting.mp3": self.digest},
            {},
            DEFAULT_LIMITS,
            60,
        )

    def worker_answer(
        self,
        _speech_root: Path,
        _digest: str,
        options: TranscriptionOptions,
        _timeout: int,
        **_kwargs: object,
    ) -> object:
        """Instant stand-in for the provider's wait for the speech worker."""
        self.run_worker()
        return speech_client.await_result(self.speech_root, self.digest, options, 0)

    def page(self) -> str:
        sources = sorted(
            (self.sanitized / os.readlink(self.sanitized / "current") / "recordings").glob("*.md")
        )
        self.assertTrue(sources, "no published recording")
        return sources[-1].read_text(encoding="utf-8")


class WorkerHandoffTests(Harness):
    def test_completed_result_replays_without_a_second_paid_request(self):
        self.request()
        self.assertEqual(self.run_worker(), (1, 0))
        self.assertEqual(len(self.transport.calls), 1)
        self.force_request()
        with mock.patch.object(
            self.backend, "transcribe", side_effect=AssertionError
        ) as transcribe:
            self.assertEqual(self.run_worker(), (1, 0))
            transcribe.assert_not_called()
        self.assertEqual(len(self.transport.calls), 1)
        self.assertEqual(list(cache.requests_dir(self.speech_root).glob("*.json")), [])
        result = speech_client.load_result(self.speech_root, self.digest, self.options)
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual([segment.text for segment in result.segments], ["Guten", "Morgen."])
        self.assertEqual(result.language, "de")
        self.assertEqual(result.backend, "mistral")
        self.assertEqual(result.model, "voxtral-mini-2602")

    def test_switching_models_reprocesses_and_never_mixes_cache_results(self):
        self.request()
        self.assertEqual(self.run_worker(), (1, 0))
        first = speech_client.load_result(self.speech_root, self.digest, self.options)
        self.assertEqual(len(self.transport.calls), 1)
        pipeline = TranscriptionOptions(model="voxtral-mini-other")
        tag = speech_worker.worker_identity(self.backend, pipeline)
        self.assertNotEqual(tag, self.worker_tag)
        self.transport.script = [
            answered(
                transcript(
                    text="Anderes Modell.",
                    segments=[{"text": "Anderes", "start": 0.0, "end": 1.0}],
                )
            )
        ]
        self.force_request()
        self.assertEqual(
            speech_worker.run_cycle(
                self.speech_root, self.backend, pipeline, tag, DEFAULT_LIMITS, decode=False
            ),
            (1, 0),
        )
        self.assertEqual(len(self.transport.calls), 2)
        second = speech_client.load_result(self.speech_root, self.digest, self.options)
        self.assertIsNotNone(second)
        assert second is not None and first is not None
        self.assertNotEqual(first.segments, second.segments)
        self.assertEqual(first.model, "voxtral-mini-2602")

    def test_switching_backends_invalidates_the_cached_result(self):
        self.request()
        self.assertEqual(self.run_worker(), (1, 0))
        local_tag = speech_worker.worker_identity(FakeBackend(), TranscriptionOptions())
        self.assertNotEqual(local_tag, self.worker_tag)
        speech_worker.atomic_json(
            self.speech_root / "worker-identity.json", {"identity": local_tag}
        )
        self.assertIsNone(speech_client.load_result(self.speech_root, self.digest, self.options))

    def test_worker_failures_are_content_free(self):
        self.answer_with(answered({"detail": "unauthorized"}, status=401))
        self.request()
        self.assertEqual(self.run_worker(), (0, 1))
        failure = speech_client.load_failure(self.speech_root, self.digest, self.options)
        self.assertEqual(failure, "speech:processing:HostedAuthenticationError")
        self.assertNotIn(API_KEY, failure or "")
        self.assertEqual(list(cache.requests_dir(self.speech_root).glob("*.json")), [])


class PublicationTests(Harness):
    def test_hosted_transcript_is_redacted_before_publication(self):
        self.answer_with(
            answered(
                transcript(
                    text="Max Mustermann spricht.",
                    segments=[
                        {
                            "text": "Max Mustermann",
                            "start": 0.0,
                            "end": 0.9,
                            "speaker_id": "speaker_1",
                        },
                        {"text": "spricht.", "start": 0.9, "end": 1.7, "speaker_id": "speaker_1"},
                    ],
                )
            )
        )
        with mock.patch.object(publisher, "await_result", side_effect=self.worker_answer):
            self.assertEqual(self.publish(TranscriptionOptions(diarize=True)), (1, 0))
        page = self.page()
        self.assertNotIn("Mustermann", page)
        self.assertIn("[ICH]", page)
        self.assertIn("Sprecher 1", page)
        self.assertIn('speech_backend: "mistral"', page)
        self.assertNotIn(API_KEY, page)

    def test_failure_keeps_the_last_successful_generation(self):
        with mock.patch.object(publisher, "await_result", side_effect=self.worker_answer):
            self.assertEqual(self.publish(), (1, 0))
        generation = os.readlink(self.sanitized / "current")
        page = self.page()
        shutil.rmtree(cache.cache_dir(self.speech_root))
        self.answer_with(TimeoutError("private detail"))
        with mock.patch.object(publisher, "await_result", side_effect=self.worker_answer):
            self.assertEqual(self.publish(), (0, 1))
        self.assertEqual(os.readlink(self.sanitized / "current"), generation)
        self.assertEqual(self.page(), page)
        reports = sorted(self.quarantine.glob("*.error"))
        self.assertEqual(len(reports), 1)
        self.assertNotIn("private detail", reports[0].read_text(encoding="utf-8"))

    def test_empty_hosted_transcript_publishes_an_empty_document(self):
        self.answer_with(answered(transcript(text="", segments=[])))
        with mock.patch.object(publisher, "await_result", side_effect=self.worker_answer):
            self.assertEqual(self.publish(), (1, 0))
        self.assertIn("(leeres Transkript)", self.page())


class RedactionContractTests(Harness):
    def test_hosted_segments_use_the_shared_redaction_contract(self):
        self.answer_with(
            answered(
                transcript(
                    segments=[
                        {"text": "Max", "start": 0.0, "end": 1.0, "speaker_id": "a"},
                        {"text": "Mustermann", "start": 1.0, "end": 2.0, "speaker_id": "b"},
                    ]
                )
            )
        )
        self.request()
        self.assertEqual(self.run_worker(), (1, 0))
        result = speech_client.load_result(self.speech_root, self.digest, self.options)
        assert result is not None
        segments, counts = redact_transcript(
            result.segments,
            TargetedAnonymizer.from_config(
                {"people": [{"replacement": "[ICH]", "values": ["Max Mustermann"]}]}
            ),
        )
        self.assertEqual([segment.text for segment in segments], ["[ICH]", ""])
        self.assertEqual(counts, {"PERSON": 1})


class SmokeTests(unittest.TestCase):
    """Opt-in real-service smoke test; nothing real is committed.

    Run with a short, consented recording of your own and a worker-only API
    key, for example::

        SPEECH_MISTRAL_SMOKE_AUDIO=/path/to/short-consented-recording.mp3 \
        MISTRAL_API_KEY_FILE=./secrets/mistral-api-key \
        python3 -m unittest ingest.tests.test_mistral.SmokeTests
    """

    def setUp(self):
        self.recording = os.getenv("SPEECH_MISTRAL_SMOKE_AUDIO", "").strip()
        if not self.recording:
            self.skipTest("set SPEECH_MISTRAL_SMOKE_AUDIO to a short consented recording")
        checkout = Path(__file__).resolve().parents[2]
        if checkout in Path(self.recording).resolve().parents:
            self.skipTest("the smoke recording must live outside the repository checkout")

    def test_real_service_transcribes_the_supplied_recording(self):
        backend = MistralBackend.from_env()
        result = backend.transcribe(
            self.recording,
            decoded(input_bytes=os.path.getsize(self.recording)),
            TranscriptionOptions(),
        )
        self.assertEqual(result.backend, "mistral")
        self.assertTrue(result.model)
        for segment in result.segments:
            self.assertGreaterEqual(segment.start_ms, 0)
            self.assertGreaterEqual(segment.end_ms, segment.start_ms)


if __name__ == "__main__":
    unittest.main()
