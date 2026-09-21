"""Conformance tests for the shared ingest status contract.

The versioned contract document lives in ``contracts/ingest-status/`` at the
repository root; the JSON fixtures in the same directory are executed here
against the Python implementations and by the JavaScript scanner tests
(``tests/test_contract_fixtures.mjs``) against theirs.
"""

import json
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


def load_cases(fixture: str) -> list[dict]:
    document = json.loads((CONTRACT_DIRECTORY / "fixtures" / fixture).read_text(encoding="utf-8"))
    return document["cases"]


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


class RevisionContractTests(unittest.TestCase):
    def test_cases(self):
        for case in load_cases("revisions.json"):
            with self.subTest(case=case["name"]):
                self.assertEqual(
                    bool(REVISION_RE.fullmatch(case["input"])), case["valid"], msg=case["input"]
                )


class IntervalContractTests(unittest.TestCase):
    def test_cases(self):
        for case in load_cases("intervals.json"):
            with self.subTest(case=case["name"]):
                if case["valid"]:
                    self.assertEqual(interval_seconds(case["input"]), case["seconds"])
                else:
                    with self.assertRaises(ValueError):
                        interval_seconds(case["input"])


class IdRangeContractTests(unittest.TestCase):
    def test_cases(self):
        for case in load_cases("id-ranges.json"):
            with self.subTest(case=case["name"]):
                self.assertEqual(document_directory(case["id"]), case["directory"])


class RevokedListContractTests(unittest.TestCase):
    def test_cases(self):
        for case in load_cases("revoked-lists.json"):
            with self.subTest(case=case["name"]):
                if case["valid"]:
                    self.assertEqual(parse_revoked_ids(case["input"]), set(case["ids"]))
                else:
                    with self.assertRaises(ValueError):
                        parse_revoked_ids(case["input"])


class FrontmatterContractTests(unittest.TestCase):
    def test_cases(self):
        for case in load_cases("frontmatter-fields.json"):
            with self.subTest(case=case["name"]):
                if case["valid"]:
                    fields = parse_frontmatter_fields(case["input"])
                    for name, value in case["expected"].items():
                        self.assertEqual(fields[name], value)
                else:
                    with self.assertRaises(ValueError):
                        parse_frontmatter_fields(case["input"])


if __name__ == "__main__":
    unittest.main()
