"""Conformance tests for the shared ingest status contract.

The versioned contract document lives in ``contracts/ingest-status/`` at the
repository root; the JSON fixtures in the same directory are executed here
against the Python implementations and by the JavaScript scanner tests
(``tests/test_contract_fixtures.mjs``) against theirs. Every topic test
iterates all fixture files of its topic, so a newly added file cannot
silently go untested.
"""

import json
import re
import unittest
from pathlib import Path

from karpathy_wiki_ingest.contract import REVISION_RE, STATUS_VALUES, parse_frontmatter_fields
from karpathy_wiki_ingest.shared import interval_seconds
from karpathy_wiki_ingest_paperless import (
    REVOKED_TITLE,
    SOURCE_FORMAT_VERSION,
    document_directory,
    parse_revoked_ids,
)

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DIRECTORY = ROOT / "contracts" / "ingest-status" / "v1"
CONTRACT = json.loads((CONTRACT_DIRECTORY / "contract.json").read_text(encoding="utf-8"))

# Every fixture topic a runtime test executes. A fixture with a topic outside
# this list fails the coverage test, and every listed topic must have at least
# one fixture file.
KNOWN_TOPICS = ("revisions", "intervals", "id-ranges", "revoked-lists", "frontmatter-fields")


def fixture_documents() -> dict[str, dict]:
    return {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((CONTRACT_DIRECTORY / "fixtures").glob("*.json"))
    }


FIXTURES = fixture_documents()


def cases_for_topic(topic: str) -> list[tuple[str, dict]]:
    """All cases of ``topic`` from every fixture file declaring it."""
    return [
        (name, case)
        for name, document in sorted(FIXTURES.items())
        if document.get("topic") == topic
        for case in document["cases"]
    ]


class ContractDocumentTests(unittest.TestCase):
    def test_contract_version_is_supported(self):
        self.assertEqual(CONTRACT["contract"], "karpathy-wiki-ingest-status")
        self.assertEqual(CONTRACT["version"], 1)

    def test_revision_pattern_matches_the_implementation(self):
        self.assertEqual(REVISION_RE.pattern, CONTRACT["revisions"]["pattern"])

    def test_status_values_match_the_implementation(self):
        self.assertEqual(STATUS_VALUES, tuple(CONTRACT["statusValues"]))

    def test_paperless_binding_matches_the_implementation(self):
        paperless = CONTRACT["paperless"]
        self.assertEqual(SOURCE_FORMAT_VERSION, paperless["sourceFormatVersion"])
        self.assertEqual(REVOKED_TITLE, paperless["revoked"]["title"])
        self.assertEqual(
            document_directory(paperless["idsPerDirectory"]),
            f"{paperless['idsPerDirectory']:04d}-{2 * paperless['idsPerDirectory'] - 1:04d}",
        )


class FixtureCoverageTests(unittest.TestCase):
    def test_every_fixture_is_versioned_with_a_known_topic(self):
        self.assertTrue(FIXTURES, "no fixture files found")
        for name, document in sorted(FIXTURES.items()):
            with self.subTest(fixture=name):
                self.assertIn("version", document)
                self.assertIn(document.get("topic"), KNOWN_TOPICS)

    def test_every_topic_has_fixture_cases(self):
        for topic in KNOWN_TOPICS:
            with self.subTest(topic=topic):
                self.assertTrue(cases_for_topic(topic), f"no cases for topic {topic}")


class RevisionContractTests(unittest.TestCase):
    def test_cases(self):
        for fixture, case in cases_for_topic("revisions"):
            with self.subTest(fixture=fixture, case=case["name"]):
                self.assertEqual(
                    bool(REVISION_RE.fullmatch(case["input"])), case["valid"], msg=case["input"]
                )


class IntervalContractTests(unittest.TestCase):
    def test_cases(self):
        for fixture, case in cases_for_topic("intervals"):
            with self.subTest(fixture=fixture, case=case["name"]):
                if case["valid"]:
                    self.assertEqual(interval_seconds(case["input"]), case["seconds"])
                else:
                    with self.assertRaises(ValueError):
                        interval_seconds(case["input"])


class IdRangeContractTests(unittest.TestCase):
    def test_cases(self):
        pattern = re.compile(CONTRACT["paperless"]["directoryPattern"])
        for fixture, case in cases_for_topic("id-ranges"):
            with self.subTest(fixture=fixture, case=case["name"]):
                self.assertEqual(document_directory(case["id"]), case["directory"])
                self.assertTrue(pattern.fullmatch(case["directory"]), msg=case["directory"])


class RevokedListContractTests(unittest.TestCase):
    def test_cases(self):
        for fixture, case in cases_for_topic("revoked-lists"):
            with self.subTest(fixture=fixture, case=case["name"]):
                if case["valid"]:
                    self.assertEqual(parse_revoked_ids(case["input"]), set(case["ids"]))
                else:
                    with self.assertRaises(ValueError):
                        parse_revoked_ids(case["input"])


class FrontmatterContractTests(unittest.TestCase):
    def test_cases(self):
        for fixture, case in cases_for_topic("frontmatter-fields"):
            with self.subTest(fixture=fixture, case=case["name"]):
                if case["valid"]:
                    fields = parse_frontmatter_fields(case["input"])
                    for name, value in case["expected"].items():
                        self.assertEqual(fields[name], value)
                else:
                    with self.assertRaises(ValueError):
                        parse_frontmatter_fields(case["input"])


if __name__ == "__main__":
    unittest.main()
