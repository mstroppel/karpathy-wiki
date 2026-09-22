import dataclasses
import http.server
import json
import re
import tempfile
import threading
import unittest
import urllib.error
from collections import Counter
from pathlib import Path
from typing import Any
from unittest import mock

from karpathy_wiki_ingest.shared import atomic_write, canonical_phone
from karpathy_wiki_ingest_paperless import (
    Ingestor,
    PaperlessClient,
    Settings,
    TargetedAnonymizer,
    document_directory,
    read_revoked_ids,
    render_document,
    run_continuously,
    source_document_path,
    source_hash,
    write_health,
)


class FakeAnonymizer:
    fingerprint = "fake-v1"

    def anonymize(self, text):
        replacements = text.count("Max Mustermann")
        return text.replace("Max Mustermann", "[ICH]"), Counter({"PERSON": replacements})

    def contains_person_name(self, text):
        return "Max" in text or "Mustermann" in text


class FailingAnonymizer:
    fingerprint = "failing-v1"

    def anonymize(self, text):
        raise RuntimeError("technical failure")

    def contains_person_name(self, text):
        return False


class FakeClient:
    def __init__(self, document, selected=None, document_types=None, tags=None):
        self.value = document
        self.selected = [document["id"]] if selected is None else selected
        self.document_types = document_types or {}
        self.tags = tags or {}

    def selected_document_ids(self):
        return self.selected

    def document(self, document_id):
        self.document_id = document_id
        return self.value

    def document_type_name(self, value):
        return self.document_types.get(value)

    def tag_names(self, values):
        return [self.tags[value] for value in values]


class IngestTests(unittest.TestCase):
    def settings(self, root):
        return Settings(
            public_url="https://paperless.example.com",
            source_tag_id=5,
            token="token",
            redactions_path=root / "redactions.json",
            interval_seconds=900,
            sanitized_root=root / "sanitized",
            quarantine_root=root / "quarantine",
            health_path=root / "health.json",
        )

    def source_path(self, root, document_id=3246):
        return source_document_path(root / "sanitized", document_id)

    def anonymizer(self):
        return TargetedAnonymizer.from_config(
            {
                "people": [
                    {
                        "replacement": "[ICH]",
                        "first_name": "Max",
                        "middle_names": ["Moritz"],
                        "last_name": "Mustermann",
                        "previous_last_name": "Musterfrau",
                        "aliases": ["Maks Mustermann"],
                    }
                ],
                "addresses": [
                    {
                        "replacement": "[ADRESSE]",
                        "street": "Berliner Straße",
                        "house_number": "12a",
                        "postal_code": "84334",
                        "city": ["Musterstadt", "Musterort"],
                    }
                ],
                "phones": [{"replacement": "[TELEFON]", "values": ["+49 170 1234567"]}],
                "emails": [
                    {
                        "replacement": "[EMAIL_ICH]",
                        "values": ["max.mustermann@example.de"],
                    }
                ],
                "birth_dates": [{"replacement": "[GEBURTSDATUM_ICH]", "date": "1990-02-01"}],
            }
        )

    def test_document_directories_contain_at_most_one_thousand_ids(self):
        self.assertEqual(document_directory(0), "0000-0999")
        self.assertEqual(document_directory(999), "0000-0999")
        self.assertEqual(document_directory(1000), "1000-1999")
        self.assertEqual(document_directory(3246), "3000-3999")

    def test_structured_redactions_cover_supported_variants(self):
        anonymizer = self.anonymizer()
        cases = {
            "Mustermann, Max": ("[ICH]", "PERSON"),
            "Max Moritz Musterfrau": ("[ICH]", "PERSON"),
            "Maks Mustermann": ("[ICH]", "PERSON"),
            "Berliner Str. 12a,84334 Musterstadt": ("[ADRESSE]", "ADDRESS"),
            "Musterort 84334 Berliner Strasse 12a": ("[ADRESSE]", "ADDRESS"),
            "0170 / 123 45 67": ("[TELEFON]", "PHONE"),
            "Max.Mustermann@Example.de": ("[EMAIL_ICH]", "EMAIL"),
            "1.2.1990": ("[GEBURTSDATUM_ICH]", "BIRTH_DATE"),
        }
        for source, (expected, category) in cases.items():
            with self.subTest(source=source):
                result, counts = anonymizer.anonymize(source)
                self.assertEqual(result, expected)
                self.assertEqual(counts, Counter({category: 1}))

    def test_phone_formats_are_equivalent(self):
        expected = canonical_phone("+49 170 1234567")
        self.assertEqual(canonical_phone("0049 (170) 123-4567"), expected)
        self.assertEqual(canonical_phone("+49 (0)170 1234567"), expected)
        self.assertEqual(canonical_phone("0170/1234567"), expected)

    def test_person_names_make_tags_sensitive(self):
        anonymizer = self.anonymizer()
        self.assertTrue(anonymizer.contains_person_name("Unterlagen Max"))
        self.assertTrue(anonymizer.contains_person_name("Musterfrau-Vertrag"))
        self.assertFalse(anonymizer.contains_person_name("Maximum"))

    def test_unconfigured_personal_data_remains_unchanged(self):
        source = "Fremde Person, fremd@example.de, IBAN DE89370400440532013000"
        result, counts = self.anonymizer().anonymize(source)
        self.assertEqual(result, source)
        self.assertFalse(counts)

    def test_invalid_redaction_configuration_is_rejected(self):
        cases = [
            ({}, "at least one"),
            ({"unknown": []}, "unknown redaction categories"),
            (
                {"birth_dates": [{"replacement": "[DATE]", "date": "31.02.1990"}]},
                "YYYY-MM-DD",
            ),
            (
                {
                    "people": [
                        {
                            "replacement": "[ICH]",
                            "first_name": "Max",
                            "middle_names": "Moritz",
                            "last_name": "Mustermann",
                        }
                    ]
                },
                "middle_names",
            ),
            (
                {"phones": [{"replacement": "[PHONE]", "values": ["123"]}]},
                "too short",
            ),
        ]
        for config, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValueError, message):
                    TargetedAnonymizer.from_config(config)

    def test_rendered_source_contains_metadata(self):
        result = render_document(
            3246,
            "Test",
            "Inhalt",
            "https://paperless.example.com",
            Counter({"PERSON": 2}),
            "2026-09-01",
            "Brief",
            ["Versicherung"],
            1,
            "a" * 64,
        )
        self.assertIn("https://paperless.example.com/documents/3246", result)
        self.assertIn("paperless_id: 3246", result)
        self.assertIn('source_revision: "' + "a" * 64 + '"', result)
        self.assertIn('issued_date: "2026-09-01"', result)
        self.assertIn('document_type: "Brief"', result)
        self.assertIn('tags: ["Versicherung"]', result)
        self.assertIn("anonymized: true", result)

    def test_ingest_anonymizes_metadata_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ingestor = Ingestor(self.settings(root), self.anonymizer())
            ingestor.client = FakeClient(
                {
                    "id": 3246,
                    "title": "Brief an Max Mustermann",
                    "content": "Hallo Max Mustermann",
                    "created": "2026-09-01",
                    "document_type": 2,
                    "tags": [5, 7, 8, 9],
                },
                document_types={2: "Brief an Max Mustermann"},
                tags={
                    7: "Versicherung",
                    8: "Unterlagen Max",
                    9: "Ablage Berliner Straße 12a, 84334 Musterstadt",
                },
            )
            self.assertEqual(ingestor.run_once(), (1, 0))
            output = self.source_path(root).read_text(encoding="utf-8")
            self.assertNotIn("Max Mustermann", output)
            self.assertNotIn("Unterlagen Max", output)
            self.assertIn('document_type: "Brief an [ICH]"', output)
            self.assertIn('tags: ["Ablage [ADRESSE]", "Versicherung"]', output)
            self.assertEqual(ingestor.run_once(), (0, 0))
            self.assertEqual(self.source_path(root).stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                (root / "sanitized" / "revoked.md").stat().st_mode & 0o777,
                0o600,
            )

    def test_removed_tag_revokes_sanitized_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ingestor = Ingestor(self.settings(root), FakeAnonymizer())
            ingestor.client = FakeClient({"id": 3246, "title": "Test", "content": "Inhalt"})
            ingestor.run_once()
            ingestor.client.selected = []
            ingestor.run_once()
            self.assertFalse(self.source_path(root).exists())
            self.assertIn("- 3246", (root / "sanitized" / "revoked.md").read_text())

    def test_manifest_is_written_after_each_cycle(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ingestor = Ingestor(self.settings(root), FakeAnonymizer())
            ingestor.client = FakeClient({"id": 3246, "title": "Test", "content": "Inhalt"})
            ingestor.run_once()
            manifest = json.loads(
                (root / "sanitized" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["contract"], "karpathy-wiki-provider-manifest")
            self.assertEqual(manifest["version"], 1)
            self.assertEqual(manifest["source"], "paperless")
            self.assertEqual(len(manifest["items"]), 1)
            item = manifest["items"][0]
            self.assertEqual(item["source_key"], "3246")
            self.assertEqual(item["source_path"], "3000-3999/document-3246.md")
            self.assertEqual(item["wiki_path"], "3000-3999/paperless-3246.md")
            self.assertEqual(item["claim"], {"paperless_id": "3246"})
            self.assertEqual(item["frontmatter"]["source_revision"], item["source_revision"])
            self.assertTrue(
                re.fullmatch(r"[0-9a-f]{64}", item["source_revision"]), item["source_revision"]
            )

    def test_manifest_reports_revocations_and_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ingestor = Ingestor(self.settings(root), FakeAnonymizer())
            document = {"id": 3246, "title": "Test", "content": "Inhalt"}
            ingestor.client = FakeClient(document)
            ingestor.run_once()
            ingestor.client.selected = []
            ingestor.run_once()
            manifest = json.loads(
                (root / "sanitized" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                manifest["revoked"],
                [{"source_key": "3246", "claim": {"paperless_id": "3246"}}],
            )
            self.assertEqual(manifest["items"], [])

            ingestor.client = FakeClient(
                {"id": 3247, "title": "Test", "content": ""}, selected=[3247]
            )
            ingestor.run_once()
            manifest = json.loads(
                (root / "sanitized" / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(len(manifest["errors"]), 1)
            self.assertEqual(manifest["errors"][0]["source_key"], "3247")
            self.assertIn("privacy-validation", manifest["errors"][0]["error"])
            self.assertNotIn("Paperless document has no OCR text", manifest["errors"][0]["error"])

    def test_privacy_failure_revokes_previous_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ingestor = Ingestor(self.settings(root), FakeAnonymizer())
            document = {"id": 3246, "title": "Test", "content": "Inhalt"}
            ingestor.client = FakeClient(document)
            ingestor.run_once()
            document["content"] = ""
            self.assertEqual(ingestor.run_once(), (0, 1))
            self.assertFalse(self.source_path(root).exists())
            quarantine = (root / "quarantine" / "document-3246.txt").read_text()
            self.assertIn("category=privacy-validation", quarantine)
            self.assertNotIn("Paperless document has no OCR text", quarantine)

    def test_operational_error_keeps_last_safe_version(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ingestor = Ingestor(self.settings(root), FakeAnonymizer())
            document = {"id": 3246, "title": "Test", "content": "Inhalt"}
            ingestor.client = FakeClient(document)
            ingestor.run_once()
            target = self.source_path(root)
            previous = target.read_text()
            document["content"] = "changed"
            ingestor.anonymizer = FailingAnonymizer()
            self.assertEqual(ingestor.run_once(), (0, 1))
            self.assertEqual(target.read_text(), previous)

    def test_invalid_revoked_list_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "revoked.md"
            path.write_text("invalid\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Invalid revoked document list"):
                read_revoked_ids(path)
            self.assertEqual(path.read_text(encoding="utf-8"), "invalid\n")

    def test_non_exported_fields_do_not_change_source_revision(self):
        document: dict[str, Any] = {
            "id": 3246,
            "title": "Test",
            "content": "Inhalt",
            "modified": "1",
            "versions": [{"id": 10}],
        }
        first = source_hash(document, "redactions", None, ["Versicherung"])
        document["modified"] = "2"
        document["versions"].append({"id": 11})
        self.assertEqual(first, source_hash(document, "redactions", None, ["Versicherung"]))


class ClientAndShutdownTests(unittest.TestCase):
    def test_client_selects_source_documents_and_uses_branded_agent(self):
        client = PaperlessClient("http://paperless:8000", "token", 5)
        responses = [
            {"results": [{"id": 3}, {"id": 1}], "next": "page-2"},
            {"results": [{"id": 2}, {"id": 3}], "next": None},
        ]
        with mock.patch.object(client, "_get_json", side_effect=responses) as get_json:
            self.assertEqual(client.selected_document_ids(), [1, 2, 3])
        self.assertEqual(client.headers["User-Agent"], "karpathy-wiki-ingest/1")
        self.assertIn("tags__id__all=5", get_json.call_args_list[0].args[0])
        self.assertIn("page=2", get_json.call_args_list[1].args[0])

    def test_client_rejects_malformed_document_entries(self):
        client = PaperlessClient("http://paperless:8000", "token", 5)
        malformed_results: tuple[Any, ...] = (
            ["3"],
            [{}],
            [{"name": "no ID"}],
            [{"id": None}],
            [{"id": {"nested": True}}],
            [{"id": "not-a-number"}],
        )
        for results in malformed_results:
            with self.subTest(results=results):
                with mock.patch.object(
                    client, "_get_json", return_value={"results": results, "next": None}
                ):
                    with self.assertRaisesRegex(ValueError, "malformed"):
                        client.selected_document_ids()

    def test_stop_event_interrupts_interval_wait(self):
        ingestor = mock.Mock()
        settings = mock.Mock(interval_seconds=900, redactions_path=Path("redactions"))
        stop_event = mock.Mock()
        stop_event.is_set.return_value = False
        stop_event.wait.return_value = True
        with mock.patch("karpathy_wiki_ingest_paperless.TargetedAnonymizer.from_file") as from_file:
            run_continuously(ingestor, settings, stop_event)
        from_file.assert_called_once_with(settings.redactions_path)
        ingestor.run_once.assert_called_once_with()
        stop_event.wait.assert_called_once_with(900)


class PaperlessApiStub(http.server.BaseHTTPRequestHandler):
    """Minimal Paperless API stub serving scripted responses."""

    queued: list[tuple[int, bytes]] = []
    received: list[str] = []

    def do_GET(self):  # noqa: N802 - http.server API
        PaperlessApiStub.received.append(self.path)
        if not PaperlessApiStub.queued:
            self.send_error(500)
            return
        status, body = PaperlessApiStub.queued.pop(0)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002 - http.server API
        pass


class MalformedResponseTests(unittest.TestCase):
    def setUp(self):
        PaperlessApiStub.received = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), PaperlessApiStub)
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        self.client = PaperlessClient(self.base_url, "token", 5)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        # Cleanup runs last-in-first-out: stop serve_forever, then close.
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def respond(self, status, payload):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        PaperlessApiStub.queued = [(status, body)]

    def test_document_list_requires_results_array(self):
        self.respond(200, {"results": None})
        with self.assertRaisesRegex(ValueError, "no results array"):
            self.client.selected_document_ids()

    def test_document_list_rejects_html_error_pages(self):
        self.respond(200, b"<html>gateway error</html>")
        with self.assertRaises((json.JSONDecodeError, UnicodeDecodeError)):
            self.client.selected_document_ids()

    def test_non_object_json_is_rejected(self):
        self.respond(200, [1, 2, 3])
        with self.assertRaisesRegex(ValueError, "non-object"):
            self.client._get_json(f"{self.base_url}/api/documents/1/")

    def test_http_error_propagates(self):
        self.respond(500, b"boom")
        with self.assertRaises(urllib.error.HTTPError):
            self.client.selected_document_ids()

    def test_wrong_document_id_is_rejected(self):
        self.respond(200, {"id": 99})
        with self.assertRaisesRegex(ValueError, "wrong document"):
            self.client.document(7)

    def test_non_list_tags_are_rejected(self):
        self.respond(200, {"tags": "tag"})
        with self.assertRaisesRegex(ValueError, "not an array"):
            self.client.tag_names("tag")

    def test_resource_entry_without_name_is_rejected(self):
        self.respond(200, {"name": "   "})
        with self.assertRaisesRegex(ValueError, "has no name"):
            self.client.document_type_name({"id": 2, "name": "  "})

    def test_resource_entry_without_numeric_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no numeric ID"):
            self.client.document_type_name("typ")


class PersistenceFailureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.redactions = self.root / "redactions.json"
        self.redactions.write_text(
            json.dumps({"people": [{"replacement": "[ICH]", "values": ["Max Mustermann"]}]}),
            encoding="utf-8",
        )

    def settings(self):
        return Settings(
            public_url="https://paperless.example.com",
            source_tag_id=5,
            token="token",
            redactions_path=self.redactions,
            interval_seconds=900,
            sanitized_root=self.root / "sanitized",
            quarantine_root=self.root / "quarantine",
            health_path=self.root / "health.json",
        )

    def test_write_failure_is_recorded_and_last_source_survives(self):
        ingestor = Ingestor(self.settings(), FakeAnonymizer())
        document = {"id": 3246, "title": "T", "content": "Inhalt"}
        ingestor.client = FakeClient(document)
        ingestor.run_once()
        target = source_document_path(self.root / "sanitized", 3246)
        previous = target.read_text()

        real_atomic_write = atomic_write

        def failing_for_sources(path, content):
            if path.suffix == ".md":
                raise OSError("disk full")
            return real_atomic_write(path, content)

        document["content"] = "changed"
        with mock.patch(
            "karpathy_wiki_ingest_paperless.atomic_write",
            side_effect=failing_for_sources,
        ):
            changed, failed = ingestor.run_once()
        self.assertEqual((changed, failed), (0, 1))
        self.assertEqual(target.read_text(), previous)
        quarantine = (self.root / "quarantine" / "document-3246.txt").read_text()
        self.assertIn("category=operational", quarantine)
        self.assertNotIn("disk full", quarantine)

    def test_unreadable_redaction_file_fails_closed(self):
        path = self.root / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "not readable JSON"):
            TargetedAnonymizer.from_file(path)

    def test_unwritable_sanitized_root_fails(self):
        blocked = self.root / "blocked"
        blocked.write_text("", encoding="utf-8")
        settings = self.settings()
        sanitized = dataclasses.replace(settings, sanitized_root=blocked / "nested")
        with self.assertRaises(NotADirectoryError):
            Ingestor(sanitized, FakeAnonymizer())

    def test_run_continuously_records_failure_in_health(self):
        ingestor = mock.Mock()
        ingestor.run_once.side_effect = urllib.error.URLError("unreachable")
        settings = mock.Mock(
            interval_seconds=1,
            redactions_path=Path("redactions"),
            health_path=self.root / "health.json",
        )
        stop_event = threading.Event()

        def fail_once():
            stop_event.set()
            raise urllib.error.URLError("unreachable")

        ingestor.run_once.side_effect = fail_once
        with mock.patch("karpathy_wiki_ingest_paperless.TargetedAnonymizer.from_file"):
            run_continuously(ingestor, settings, stop_event)
        health = json.loads((self.root / "health.json").read_text())
        self.assertEqual(health["failed"], 1)

    def test_health_record_shape(self):
        health_path = self.root / "health.json"
        write_health(health_path, 3)
        payload = json.loads(health_path.read_text())
        self.assertEqual(payload["failed"], 3)
        self.assertGreaterEqual(payload["checked_at"], 0)
        self.assertEqual(health_path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
