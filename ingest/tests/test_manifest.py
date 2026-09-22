"""Conformance tests for the provider manifest contract.

The versioned contract document lives in ``contracts/provider-manifest/`` at
the repository root; the JSON fixtures in the same directory are executed here
against the Python validator and by the JavaScript scanner tests
(``tests/test_contract_fixtures.mjs``) against theirs.
"""

import json
import unittest
from pathlib import Path

from karpathy_wiki_ingest.manifest import (
    MANIFEST_CONTRACT,
    MANIFEST_FILENAME,
    MANIFEST_VERSION,
    ManifestError,
    build_manifest,
    validate_manifest,
)
from karpathy_wiki_ingest_paperless import SOURCE_FORMAT_VERSION

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_DIRECTORY = ROOT / "contracts" / "provider-manifest" / "v1"
CONTRACT = json.loads((CONTRACT_DIRECTORY / "contract.json").read_text(encoding="utf-8"))
FIXTURES = json.loads(
    (CONTRACT_DIRECTORY / "fixtures" / "manifests.json").read_text(encoding="utf-8")
)


class ManifestContractTests(unittest.TestCase):
    def test_contract_version_is_supported(self):
        self.assertEqual(CONTRACT["contract"], "karpathy-wiki-provider-manifest")
        self.assertEqual(CONTRACT["version"], MANIFEST_VERSION)

    def test_implementation_matches_the_declared_file_and_contract(self):
        self.assertEqual(CONTRACT["file"], MANIFEST_FILENAME)
        self.assertEqual(MANIFEST_CONTRACT, CONTRACT["contract"])

    def test_build_produces_a_valid_manifest(self):
        from karpathy_wiki_ingest.manifest import ManifestItem

        revision = "a" * 64
        manifest = build_manifest(
            "notes",
            [
                ManifestItem(
                    source_key="a.md",
                    source_path="a.md",
                    wiki_path="notes/a/index.md",
                    source_revision=revision,
                    frontmatter={"source_revision": revision},
                    claim={"source_path": "a.md"},
                )
            ],
            revoked=[{"source_key": "b.md", "claim": {"source_path": "b.md"}}],
            errors=[{"path": "c.md", "error": "quarantine:UnicodeError"}],
            generated_at=1760000000,
        )
        self.assertEqual(validate_manifest(manifest, expected_source="notes"), manifest)

    def test_build_rejects_mismatched_source(self):
        with self.assertRaises(ManifestError):
            build_manifest("Not A Source", [], generated_at=1760000000)

    def test_paperless_source_format_stays_versioned(self):
        self.assertEqual(SOURCE_FORMAT_VERSION, 2)


class ManifestFixtureTests(unittest.TestCase):
    def test_cases(self):
        for case in FIXTURES["cases"]:
            with self.subTest(case=case["name"]):
                if case["valid"]:
                    self.assertEqual(
                        validate_manifest(case["manifest"]),
                        case["manifest"],
                    )
                else:
                    with self.assertRaises(ManifestError):
                        validate_manifest(case["manifest"])


if __name__ == "__main__":
    unittest.main()
