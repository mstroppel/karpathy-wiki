import dataclasses
import http.server
import json
import os
import re
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
from collections import Counter
from pathlib import Path
from typing import Any
from unittest import mock

from karpathy_wiki_ingest.shared import atomic_write, canonical_phone
from karpathy_wiki_ingest.state import (
    JOB_PENDING,
    JOB_SUCCEEDED,
    JOB_SUPERSEDED,
    StateError,
    StateStore,
)
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
        return source_document_path(root / "sanitized" / "current", document_id)

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

    def test_structured_first_names_are_redacted_in_signoffs(self):
        anonymizer = TargetedAnonymizer.from_config(
            {
                "people": [
                    {"replacement": "[PERSON_1]", "first_name": "Maria", "last_name": "Muster"},
                    {"replacement": "[PERSON_2]", "first_name": "Josef", "last_name": "Beispiel"},
                ]
            }
        )

        result, counts = anonymizer.anonymize(
            "Mit freundlichen Grüßen\n\nMaria und Josef\nMaria Muster, JOSEF Beispiel\nMarianne"
        )

        self.assertEqual(
            result,
            "Mit freundlichen Grüßen\n\n[PERSON_1] und [PERSON_2]\n"
            "[PERSON_1], [PERSON_2]\nMarianne",
        )
        self.assertEqual(counts, Counter({"PERSON": 4}))

    def test_structured_first_name_possessive_variants_are_filtered_and_redacted(self):
        anonymizer = TargetedAnonymizer.from_config(
            {
                "people": [
                    {"replacement": "[PERSON_1]", "first_name": "Anton", "last_name": "Muster"}
                ]
            }
        )

        for text in ("Antons House", "Anton's House", "Anton’s House"):
            with self.subTest(text=text):
                self.assertTrue(anonymizer.contains_person_name(text))
                result, counts = anonymizer.anonymize(text)
                self.assertEqual(result, "[PERSON_1] House")
                self.assertEqual(counts, Counter({"PERSON": 1}))

        self.assertFalse(anonymizer.contains_person_name("Antonym House"))

    def test_structured_first_names_allow_markdown_underscore_delimiters(self):
        anonymizer = TargetedAnonymizer.from_config(
            {
                "people": [
                    {"replacement": "[PERSON_1]", "first_name": "Anton", "last_name": "Muster"}
                ]
            }
        )

        for text, expected in (("_Anton:", "[PERSON_1]:"), ("_Anton_:", "[PERSON_1]:")):
            with self.subTest(text=text):
                self.assertTrue(anonymizer.contains_person_name(text))
                result, counts = anonymizer.anonymize(text)
                self.assertEqual(result, expected)
                self.assertEqual(counts, Counter({"PERSON": 1}))

        for text in ("foo_Anton", "_Antonym:", "_Anton_suffix"):
            with self.subTest(text=text):
                self.assertFalse(anonymizer.contains_person_name(text))

    def test_structured_first_name_ending_in_s_uses_apostrophe_only(self):
        anonymizer = TargetedAnonymizer.from_config(
            {
                "people": [
                    {"replacement": "[PERSON_1]", "first_name": "Felix", "last_name": "Muster"}
                ]
            }
        )

        for text in ("Felix' Haus", "Felix’ Haus"):
            with self.subTest(text=text):
                self.assertTrue(anonymizer.contains_person_name(text))
                result, counts = anonymizer.anonymize(text)
                self.assertEqual(result, "[PERSON_1] Haus")
                self.assertEqual(counts, Counter({"PERSON": 1}))

        for text in ("Felixs Haus", "Felix's Haus", "Felix’s Haus"):
            with self.subTest(text=text):
                self.assertFalse(anonymizer.contains_person_name(text))

    def test_shared_first_name_with_different_replacements_is_rejected(self):
        configuration = {
            "people": [
                {"replacement": "[PERSON_1]", "first_name": "Maria", "last_name": "Muster"},
                {"replacement": "[PERSON_2]", "first_name": "maria", "last_name": "Beispiel"},
            ]
        }

        with self.assertRaisesRegex(ValueError, "one literal value has multiple replacements"):
            TargetedAnonymizer.from_config(configuration)

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

    def test_rendered_source_keeps_the_paperless_frontmatter_and_link(self):
        """Regression: the Paperless link must survive source-to-wiki rendering.

        The sanitized source carries the validated HTTPS ``paperless_url`` in
        its frontmatter and a visible link; the provider manifest repeats the
        URL as trusted page frontmatter so every wiki summary page can render
        it.
        """
        source_url = "https://paperless.example.com/documents/3246"
        result = render_document(
            3246,
            "Test",
            "Inhalt",
            "https://paperless.example.com",
            Counter(),
            None,
            None,
            [],
            0,
            "a" * 64,
        )
        self.assertIn(f"paperless_url: {json.dumps(source_url)}", result)
        self.assertIn(f"]({source_url})", result)

    def test_settings_reject_non_https_public_urls(self):
        with mock.patch.dict(
            os.environ,
            {
                "PAPERLESS_TOKEN": "token",
                "PAPERLESS_SOURCE_TAG_ID": "5",
                "REDACTIONS_FILE": "/redactions.json",
                "PAPERLESS_PUBLIC_URL": "http://paperless.example.com",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ValueError, "PAPERLESS_PUBLIC_URL must use HTTPS"):
                Settings.from_env()

    def test_settings_accept_shared_state_path(self):
        with mock.patch.dict(
            os.environ,
            {
                "PAPERLESS_TOKEN": "token",
                "PAPERLESS_SOURCE_TAG_ID": "5",
                "REDACTIONS_FILE": "/redactions.json",
                "PAPERLESS_PUBLIC_URL": "https://paperless.example.com",
                "INGEST_STATE_PATH": "/data/state/ingest.sqlite3",
            },
            clear=True,
        ):
            self.assertEqual(Settings.from_env().state_path, Path("/data/state/ingest.sqlite3"))

    def test_settings_reject_public_urls_without_an_https_host(self):
        with mock.patch.dict(
            os.environ,
            {
                "PAPERLESS_TOKEN": "token",
                "PAPERLESS_SOURCE_TAG_ID": "5",
                "REDACTIONS_FILE": "/redactions.json",
                "PAPERLESS_PUBLIC_URL": "https:paperless.example.com",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(
                ValueError, "PAPERLESS_PUBLIC_URL must include an HTTPS host"
            ):
                Settings.from_env()

    def test_settings_reject_public_urls_with_query_or_fragment(self):
        for public_url in (
            "https://paperless.example.com/?query=1",
            "https://paperless.example.com/#section",
        ):
            with self.subTest(public_url=public_url):
                with mock.patch.dict(
                    os.environ,
                    {
                        "PAPERLESS_TOKEN": "token",
                        "PAPERLESS_SOURCE_TAG_ID": "5",
                        "REDACTIONS_FILE": "/redactions.json",
                        "PAPERLESS_PUBLIC_URL": public_url,
                    },
                    clear=True,
                ):
                    with self.assertRaisesRegex(
                        ValueError, "PAPERLESS_PUBLIC_URL must not include a query or fragment"
                    ):
                        Settings.from_env()

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
            previous = (root / "sanitized" / "current").resolve()
            self.assertEqual(ingestor.run_once(), (0, 0))
            self.assertEqual((root / "sanitized" / "current").resolve(), previous)
            self.assertEqual(self.source_path(root).stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                (root / "sanitized" / "current" / "revoked.md").stat().st_mode & 0o777,
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
            self.assertIn("- 3246", (root / "sanitized" / "current" / "revoked.md").read_text())

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
            self.assertRegex(
                item["source_path"], r"^generations/[0-9a-f]{32}/3000-3999/document-3246.md$"
            )
            self.assertEqual(item["wiki_path"], "3000-3999/paperless-3246.md")
            self.assertEqual(item["claim"], {"paperless_id": "3246"})
            self.assertEqual(item["frontmatter"]["source_revision"], item["source_revision"])
            # The manifest frontmatter carries the validated HTTPS Paperless
            # link so every generated wiki page can render it.
            self.assertEqual(
                item["frontmatter"]["paperless_url"],
                "https://paperless.example.com/documents/3246",
            )
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
            generation = (root / "sanitized" / "current").resolve()
            document["content"] = "changed"
            ingestor.anonymizer = FailingAnonymizer()
            self.assertEqual(ingestor.run_once(), (0, 1))
            self.assertEqual(target.read_text(), previous)
            self.assertEqual((root / "sanitized" / "current").resolve(), generation)

    def test_interrupted_staging_is_discarded_and_immutable_manifest_paths_resolve(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ingestor = Ingestor(self.settings(root), FakeAnonymizer())
            ingestor.client = FakeClient({"id": 3246, "title": "T", "content": "Inhalt"})
            staging = root / "sanitized" / "generations" / ".staging-interrupted"
            staging.mkdir(parents=True)
            (staging / "partial.md").write_text("unfinished")
            ingestor.run_once()
            self.assertFalse(staging.exists())
            root_sources = root / "sanitized"
            manifest = json.loads((root_sources / "manifest.json").read_text())
            source_path = manifest["items"][0]["source_path"]
            self.assertEqual(
                (root_sources / source_path).read_text(), self.source_path(root).read_text()
            )
            self.assertEqual(len(list((root_sources / "generations").iterdir())), 1)

    def test_manifest_failure_rolls_back_the_active_generation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ingestor = Ingestor(self.settings(root), FakeAnonymizer())
            document = {"id": 3246, "title": "T", "content": "Inhalt"}
            ingestor.client = FakeClient(document)
            ingestor.run_once()
            previous = (root / "sanitized" / "current").resolve()
            original = self.source_path(root).read_text()
            document["content"] = "changed"
            with mock.patch(
                "karpathy_wiki_ingest_paperless.ingestor.write_manifest",
                side_effect=OSError("disk"),
            ):
                with self.assertRaises(OSError):
                    ingestor.run_once()
            self.assertEqual((root / "sanitized" / "current").resolve(), previous)
            self.assertEqual(self.source_path(root).read_text(), original)
            self.assertEqual(len(list((root / "sanitized" / "generations").iterdir())), 1)

    def test_manifest_reader_can_open_the_previous_generation_during_switch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ingestor = Ingestor(self.settings(root), FakeAnonymizer())
            document = {"id": 3246, "title": "T", "content": "Inhalt"}
            ingestor.client = FakeClient(document)
            ingestor.run_once()
            source_root = root / "sanitized"
            old_manifest = json.loads((source_root / "manifest.json").read_text())
            old_source = old_manifest["items"][0]["source_path"]
            original = (source_root / old_source).read_text()
            document["content"] = "changed"

            from karpathy_wiki_ingest.manifest import write_manifest as real_write_manifest

            def read_before_manifest_change(path, manifest):
                self.assertEqual((source_root / old_source).read_text(), original)
                self.assertNotEqual(
                    (source_root / "current").resolve(), (source_root / old_source).parent.parent
                )
                real_write_manifest(path, manifest)

            with mock.patch(
                "karpathy_wiki_ingest_paperless.ingestor.write_manifest",
                side_effect=read_before_manifest_change,
            ):
                ingestor.run_once()
            new_manifest = json.loads((source_root / "manifest.json").read_text())
            self.assertNotEqual(new_manifest["items"][0]["source_path"], old_source)
            self.assertTrue((source_root / new_manifest["items"][0]["source_path"]).is_file())

    def test_old_flat_layout_requires_manual_reorganization(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ingestor = Ingestor(self.settings(root), FakeAnonymizer())
            old = source_document_path(root / "sanitized", 3246)
            old.parent.mkdir()
            old.write_text("old source")
            with self.assertRaisesRegex(ValueError, "flat source layout"):
                ingestor.run_once()
            self.assertEqual(old.read_text(), "old source")

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


class DurablePaperlessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings_with_state = Settings(
            public_url="https://paperless.example.com",
            source_tag_id=5,
            token="token",
            redactions_path=self.root / "redactions.json",
            interval_seconds=900,
            sanitized_root=self.root / "sanitized",
            quarantine_root=self.root / "quarantine",
            health_path=self.root / "health.json",
            state_path=self.root / "ingest.sqlite3",
        )
        self.ingestor = Ingestor(self.settings_with_state, FakeAnonymizer())
        self.document = {"id": 3246, "title": "T", "content": "Inhalt"}
        self.ingestor.client = FakeClient(self.document)

    def store(self):
        assert self.settings_with_state.state_path is not None
        store = StateStore.open(self.settings_with_state.state_path)
        self.addCleanup(store.close)
        return store

    def test_cycle_is_recorded_once_and_health_contains_metrics(self):
        self.assertEqual(self.ingestor.run_once(), (1, 0))
        generation = (self.root / "sanitized" / "current").resolve()
        self.assertEqual(self.ingestor.run_once(), (0, 0))
        self.assertEqual((self.root / "sanitized" / "current").resolve(), generation)
        store = self.store()
        key = self.ingestor.cycle_key(self.ingestor.snapshot()[1])
        job = store.job_by_idempotency_key(key)
        assert job is not None
        self.assertEqual(job.state, JOB_SUCCEEDED)
        self.assertEqual(job.attempts, 1)
        self.assertEqual(store.metrics()["source_generations"], 1)
        health = json.loads(self.settings_with_state.health_path.read_text())
        self.assertEqual(health["metrics"]["last_source_generation"]["provider"], "paperless")

    def test_restart_finishes_published_job_without_new_generation(self):
        with mock.patch.object(StateStore, "complete", side_effect=StateError("interrupted")):
            with self.assertRaises(StateError):
                self.ingestor.run_once()
        generation = (self.root / "sanitized" / "current").resolve()
        store = self.store()
        job = store.job_by_idempotency_key(self.ingestor.cycle_key(self.ingestor.snapshot()[1]))
        assert job is not None
        self.assertNotEqual(job.state, JOB_SUCCEEDED)
        lease = store._connection.execute(
            "SELECT expires_at FROM leases WHERE job_id = ?", (job.id,)
        ).fetchone()
        assert lease is not None
        future = lease[0] + 1
        with mock.patch("karpathy_wiki_ingest.state.time.time", return_value=future):
            self.assertEqual(self.ingestor.run_once(), (0, 0))
        self.assertEqual((self.root / "sanitized" / "current").resolve(), generation)
        self.assertEqual(store.job(job.id).state, JOB_SUCCEEDED)

    def test_relative_sanitized_root_keeps_the_active_generation_during_cleanup(self):
        previous = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        settings = dataclasses.replace(
            self.settings_with_state,
            sanitized_root=Path("sanitized"),
            quarantine_root=Path("quarantine"),
            health_path=Path("health.json"),
        )
        ingestor = Ingestor(settings, FakeAnonymizer())
        ingestor.client = FakeClient(self.document)
        self.assertEqual(ingestor.run_once(), (1, 0))
        self.document["content"] = "changed"
        self.assertEqual(ingestor.run_once(), (1, 0))
        root = self.root / "sanitized"
        self.assertTrue((root / "current").is_symlink())
        self.assertTrue(source_document_path(root / "current", 3246).is_file())
        self.assertEqual(len(list((root / "generations").iterdir())), 1)

    def test_recovery_prunes_obsolete_generations(self):
        self.assertEqual(self.ingestor.run_once(), (1, 0))
        first = (self.root / "sanitized" / "current").resolve()
        self.document["content"] = ""
        with mock.patch.object(StateStore, "complete", side_effect=StateError("interrupted")):
            with self.assertRaises(StateError):
                self.ingestor.run_once()
        second = (self.root / "sanitized" / "current").resolve()
        self.assertNotEqual(first, second)
        self.assertEqual(len(list((self.root / "sanitized" / "generations").iterdir())), 2)
        store = self.store()
        job = store.job_by_idempotency_key(self.ingestor.cycle_key(self.ingestor.snapshot()[1]))
        assert job is not None
        self.assertNotEqual(job.state, JOB_SUCCEEDED)
        lease = store._connection.execute(
            "SELECT expires_at FROM leases WHERE job_id = ?", (job.id,)
        ).fetchone()
        assert lease is not None
        future = lease[0] + 1
        with mock.patch("karpathy_wiki_ingest.state.time.time", return_value=future):
            self.assertEqual(self.ingestor.run_once(), (0, 1))
        self.assertEqual((self.root / "sanitized" / "current").resolve(), second)
        self.assertEqual(len(list((self.root / "sanitized" / "generations").iterdir())), 1)
        self.assertEqual(store.job(job.id).state, JOB_SUCCEEDED)
        # A restart that lands after completion but before cleanup also
        # finishes the pruning through the succeeded-job shortcut.
        stale = self.root / "sanitized" / "generations" / "stale"
        stale.mkdir()
        with mock.patch("karpathy_wiki_ingest.state.time.time", return_value=future):
            self.assertEqual(self.ingestor.run_once(), (0, 1))
        self.assertFalse(stale.exists())
        self.assertEqual(len(list((self.root / "sanitized" / "generations").iterdir())), 1)

    def test_stale_empty_manifest_is_republished_with_the_privacy_error(self):
        # An empty selection publishes an empty manifest for generation A.
        self.ingestor.client = FakeClient(self.document, selected=[])
        self.assertEqual(self.ingestor.run_once(), (0, 0))
        root = self.root / "sanitized"
        self.assertEqual(len(list((root / "generations").iterdir())), 1)
        stale_manifest = (root / "manifest.json").read_text()
        self.assertNotIn("privacy-validation", stale_manifest)
        # The next cycle revokes a document whose OCR is empty, so the new
        # manifest is empty again apart from the privacy error. The on-disk
        # state mimics an interruption before the manifest replacement: the
        # pointer switched, the manifest still belongs to generation A.
        from karpathy_wiki_ingest.manifest import write_manifest as real_write_manifest

        def write_stale_manifest(_path, _manifest):
            real_write_manifest(root / "manifest.json", json.loads(stale_manifest))

        self.document["content"] = ""
        self.ingestor.client = FakeClient(self.document)
        with mock.patch(
            "karpathy_wiki_ingest_paperless.ingestor.write_manifest",
            side_effect=write_stale_manifest,
        ):
            self.assertEqual(self.ingestor.run_once(), (0, 1))
        self.assertEqual((root / "manifest.json").read_text(), stale_manifest)
        store = self.store()
        job = store.job_by_idempotency_key(self.ingestor.cycle_key(self.ingestor.snapshot()[1]))
        assert job is not None
        self.assertEqual(job.state, JOB_PENDING)
        # Once the retry backoff expires the cycle republishes, and the
        # manifest carries the privacy error again.
        future = job.next_attempt_at + 1
        with mock.patch("karpathy_wiki_ingest.state.time.time", return_value=future):
            self.assertEqual(self.ingestor.run_once(), (0, 1))
        manifest = json.loads((root / "manifest.json").read_text())
        self.assertEqual(
            [error["error"] for error in manifest["errors"]],
            ["privacy-validation:PrivacyValidationError"],
        )
        self.assertEqual(json.loads(self.settings_with_state.health_path.read_text())["failed"], 1)
        self.assertEqual(len(list((root / "generations").iterdir())), 1)
        self.assertEqual(store.job(job.id).state, JOB_SUCCEEDED)

    def test_failed_cycle_backs_off_and_new_input_supersedes_it(self):
        with mock.patch(
            "karpathy_wiki_ingest_paperless.ingestor.write_manifest", side_effect=OSError("disk")
        ):
            with self.assertRaises(OSError):
                self.ingestor.run_once()
        store = self.store()
        key = self.ingestor.cycle_key(self.ingestor.snapshot()[1])
        failed = store.job_by_idempotency_key(key)
        assert failed is not None
        self.assertEqual(failed.state, JOB_PENDING)
        self.assertEqual(self.ingestor.run_once(), (0, 1))
        self.document["content"] = "changed"
        self.assertEqual(self.ingestor.run_once(), (1, 0))
        self.assertEqual(store.job(failed.id).state, JOB_SUPERSEDED)

    def test_failure_health_retains_retry_metrics(self):
        with mock.patch(
            "karpathy_wiki_ingest_paperless.ingestor.write_manifest", side_effect=OSError("disk")
        ):
            with self.assertRaises(OSError):
                self.ingestor.run_once()
        health = json.loads(self.settings_with_state.health_path.read_text())
        self.assertEqual(health["failed"], 1)
        self.assertEqual(health["metrics"]["failed_jobs"], 1)
        self.assertEqual(health["metrics"]["queue_depth"], 1)

    def test_continuous_failure_health_retains_retry_metrics(self):
        stop = threading.Event()

        def fail_manifest(_path, _manifest):
            stop.set()
            raise OSError("disk")

        with (
            mock.patch(
                "karpathy_wiki_ingest_paperless.ingestor.write_manifest",
                side_effect=fail_manifest,
            ),
            mock.patch(
                "karpathy_wiki_ingest_paperless.ingestor.TargetedAnonymizer.from_file",
                return_value=FakeAnonymizer(),
            ),
        ):
            run_continuously(self.ingestor, self.settings_with_state, stop)
        health = json.loads(self.settings_with_state.health_path.read_text())
        self.assertEqual(health["failed"], 1)
        self.assertEqual(health["metrics"]["failed_jobs"], 1)

    def test_lost_lease_cannot_publish(self):
        def fenced(work, _store, _lease_id, _seconds):
            return work(lambda: (_ for _ in ()).throw(StateError("lost")))

        with mock.patch.object(self.ingestor, "publish_with_heartbeat", side_effect=fenced):
            with self.assertRaises(StateError):
                self.ingestor.run_once()
        self.assertFalse((self.root / "sanitized" / "current").exists())

    def test_lease_lost_during_manifest_write_restores_previous_publication(self):
        self.ingestor.run_once()
        root = self.root / "sanitized"
        old_generation = (root / "current").resolve()
        old_manifest = (root / "manifest.json").read_text()
        self.document["content"] = "changed"
        from karpathy_wiki_ingest.manifest import write_manifest as real_write_manifest

        lost = False

        def write_then_lose(path, manifest):
            nonlocal lost
            real_write_manifest(path, manifest)
            lost = True

        def fenced(work, _store, _lease_id, _seconds):
            def guard():
                if lost:
                    raise StateError("lost")

            return work(guard)

        with (
            mock.patch.object(self.ingestor, "publish_with_heartbeat", side_effect=fenced),
            mock.patch(
                "karpathy_wiki_ingest_paperless.ingestor.write_manifest",
                side_effect=write_then_lose,
            ),
            self.assertRaises(StateError),
        ):
            self.ingestor.run_once()
        self.assertEqual((root / "current").resolve(), old_generation)
        self.assertEqual((root / "manifest.json").read_text(), old_manifest)
        self.assertEqual(len(list((root / "generations").iterdir())), 1)

    def test_lease_lost_after_worker_returns_rolls_back_publication(self):
        self.ingestor.run_once()
        root = self.root / "sanitized"
        old_generation = (root / "current").resolve()
        old_manifest = (root / "manifest.json").read_text()
        self.document["content"] = "changed"

        def fenced(work, _store, _lease_id, _seconds):
            work(lambda: None)
            raise StateError("lost after publication")

        with mock.patch.object(self.ingestor, "publish_with_heartbeat", side_effect=fenced):
            with self.assertRaises(StateError):
                self.ingestor.run_once()
        self.assertEqual((root / "current").resolve(), old_generation)
        self.assertEqual((root / "manifest.json").read_text(), old_manifest)

    def test_privacy_failure_stays_visible_when_cycle_is_idempotent(self):
        self.document["content"] = ""
        self.assertEqual(self.ingestor.run_once(), (0, 1))
        generation = (self.root / "sanitized" / "current").resolve()
        self.assertEqual(self.ingestor.run_once(), (0, 1))
        self.assertEqual((self.root / "sanitized" / "current").resolve(), generation)
        self.assertEqual(json.loads(self.settings_with_state.health_path.read_text())["failed"], 1)

    def test_new_invalid_snapshot_publishes_its_own_privacy_revocation(self):
        self.document["content"] = ""
        self.assertEqual(self.ingestor.run_once(), (0, 1))
        previous = (self.root / "sanitized" / "current").resolve()
        self.document["title"] = "new title"
        self.assertEqual(self.ingestor.run_once(), (0, 1))
        self.assertNotEqual((self.root / "sanitized" / "current").resolve(), previous)
        job = self.store().job_by_idempotency_key(
            self.ingestor.cycle_key(self.ingestor.snapshot()[1])
        )
        assert job is not None
        self.assertEqual(job.state, JOB_SUCCEEDED)

    def test_different_providers_do_not_supersede_one_another(self):
        store = self.store()
        webdav, _ = store.enqueue("ingest", {"provider": "webdav"}, now=100)
        self.ingestor.run_once()
        self.assertEqual(store.job(webdav.id).state, JOB_PENDING)

    def test_state_failure_prevents_publication(self):
        assert self.settings_with_state.state_path is not None
        self.settings_with_state.state_path.write_text("not a sqlite database")
        with self.assertRaises(sqlite3.DatabaseError):
            self.ingestor.run_once()
        self.assertFalse((self.root / "sanitized" / "current").exists())

    def test_upstream_change_after_acceptance_cannot_publish_mixed_snapshot(self):
        original = dict(self.document)
        changed = {**original, "content": "new content"}
        with mock.patch.object(self.ingestor.client, "document", side_effect=[original, changed]):
            self.assertEqual(self.ingestor.run_once(), (0, 1))
        self.assertFalse((self.root / "sanitized" / "current").exists())
        key = self.ingestor.cycle_key({"3246": source_hash(original, "fake-v1", None, [])})
        job = self.store().job_by_idempotency_key(key)
        assert job is not None
        self.assertEqual(job.state, JOB_PENDING)

    def test_public_url_change_republishes_even_when_document_revision_is_unchanged(self):
        self.ingestor.run_once()
        old_generation = (self.root / "sanitized" / "current").resolve()
        changed_settings = dataclasses.replace(
            self.settings_with_state, public_url="https://paperless-new.example.com"
        )
        other = Ingestor(changed_settings, FakeAnonymizer())
        other.client = FakeClient(self.document)
        self.assertEqual(other.run_once(), (1, 0))
        self.assertNotEqual((self.root / "sanitized" / "current").resolve(), old_generation)
        text = source_document_path(self.root / "sanitized" / "current", 3246).read_text()
        self.assertIn("https://paperless-new.example.com/documents/3246", text)


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
        target = source_document_path(self.root / "sanitized" / "current", 3246)
        previous = target.read_text()

        real_atomic_write = atomic_write

        def failing_for_sources(path, content):
            if path.suffix == ".md":
                raise OSError("disk full")
            return real_atomic_write(path, content)

        document["content"] = "changed"
        with mock.patch(
            "karpathy_wiki_ingest_paperless.ingestor.atomic_write",
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
