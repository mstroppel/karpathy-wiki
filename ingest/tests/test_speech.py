"""Regression tests for the shared provider/worker cache replay contract."""

from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from karpathy_wiki_ingest_audio import speech_client
from karpathy_wiki_speech import cache, worker
from karpathy_wiki_speech.backends import FakeBackend
from karpathy_wiki_speech.types import DEFAULT_LIMITS, TranscriptionOptions, file_revision


class CacheReplayTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.options = TranscriptionOptions()
        self.backend = FakeBackend()
        snapshot = self.root / "snapshot.mp3"
        snapshot.write_bytes(b"synthetic recording")
        self.digest = file_revision(str(snapshot))
        self.reference = speech_client.stage_recording(
            self.root, snapshot, self.digest, "mp3", DEFAULT_LIMITS.max_bytes
        )
        self.queue_request()
        self.assertEqual(self.run_worker(), (1, 0))
        self.result_path = cache.find_cached_result(
            self.root, self.digest, speech_client.requested_options(self.options)
        )
        assert self.result_path is not None
        self.document = json.loads(self.result_path.read_text())
        self.original = speech_client.load_result(self.root, self.digest, self.options)
        self.request_path = cache.request_path(
            self.root, cache.request_id(self.digest, speech_client.result_identity(self.options))
        )

    def queue_request(self):
        speech_client.ensure_requested(self.root, self.digest, self.options, self.reference)

    def run_worker(self):
        return worker.run_cycle(
            self.root, self.backend, self.options, "worker-1", DEFAULT_LIMITS, decode=False
        )

    def force_replay_request(self):
        worker.atomic_json(
            self.request_path,
            {
                "audio": self.reference,
                "audio_sha256": self.digest,
                "options": speech_client.requested_options(self.options),
            },
        )

    def test_missing_or_corrupt_result_is_regenerated_after_provider_requeues(self):
        malformed_documents: list[object] = [
            [],
            {},
            {**self.document, "backend": None},
            {**self.document, "model": 4},
            {**self.document, "language": []},
            {**self.document, "segments": None},
            {**self.document, "segments": [None]},
            {**self.document, "segments": [{"start_ms": 0, "end_ms": 1}]},
            {
                **self.document,
                "segments": [{"start_ms": True, "end_ms": 1, "text": "synthetic"}],
            },
            {
                **self.document,
                "segments": [
                    {"start_ms": 0, "end_ms": 1, "text": "synthetic", "speaker_id": "bad"}
                ],
            },
            {**self.document, "identity": "0" * 64},
            {**self.document, "audio_sha256": "0" * 64},
            {**self.document, "worker_identity": "other-worker"},
            {**self.document, "options": None},
            {**self.document, "options": {}},
            {**self.document, "options": {"language": "de", "diarize": False}},
        ]
        corruptions = [None, b"{truncated", b"\xff"] + [
            json.dumps(document).encode() for document in malformed_documents
        ]
        for corruption in corruptions:
            with self.subTest(corruption=corruption):
                assert self.result_path is not None
                if corruption is None:
                    self.result_path.unlink()
                else:
                    self.result_path.write_bytes(corruption)
                self.assertIsNone(speech_client.load_result(self.root, self.digest, self.options))
                self.queue_request()
                self.assertTrue(self.request_path.exists())
                with patch.object(
                    self.backend, "transcribe", wraps=self.backend.transcribe
                ) as transcribe:
                    self.assertEqual(self.run_worker(), (1, 0))
                    transcribe.assert_called_once()
                self.assertFalse(self.request_path.exists())
                self.assertEqual(
                    speech_client.await_result(self.root, self.digest, self.options, 0),
                    self.original,
                )

    def test_valid_result_replays_without_transcription(self):
        self.force_replay_request()
        with patch.object(self.backend, "transcribe", side_effect=AssertionError) as transcribe:
            self.assertEqual(self.run_worker(), (1, 0))
            transcribe.assert_not_called()
        self.assertFalse(self.request_path.exists())
        self.assertEqual(
            speech_client.await_result(self.root, self.digest, self.options, 0), self.original
        )

    def test_empty_transcript_is_a_valid_cache_hit(self):
        assert self.original is not None
        worker.write_result(
            self.root, self.digest, self.options, replace(self.original, segments=[]), "worker-1"
        )
        self.force_replay_request()
        with patch.object(self.backend, "transcribe", side_effect=AssertionError) as transcribe:
            self.assertEqual(self.run_worker(), (1, 0))
            transcribe.assert_not_called()
        self.assertEqual(
            speech_client.await_result(self.root, self.digest, self.options, 0).segments, []
        )

    def test_failed_regeneration_reports_failure_instead_of_false_completion(self):
        assert self.result_path is not None
        self.result_path.unlink()
        self.queue_request()
        with patch.object(self.backend, "transcribe", side_effect=RuntimeError("private detail")):
            self.assertEqual(self.run_worker(), (0, 1))
        self.assertFalse(self.request_path.exists())
        self.assertEqual(
            speech_client.load_failure(self.root, self.digest, self.options),
            "speech:processing:RuntimeError",
        )

    def test_index_cannot_point_outside_cache(self):
        index = cache.load_index(self.root, self.digest)
        index[-1]["identity"] = "../outside"
        worker.atomic_json(cache.cache_index_path(self.root, self.digest), index)
        self.assertIsNone(speech_client.load_result(self.root, self.digest, self.options))
        self.queue_request()
        with patch.object(self.backend, "transcribe", wraps=self.backend.transcribe) as transcribe:
            self.assertEqual(self.run_worker(), (1, 0))
            transcribe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
