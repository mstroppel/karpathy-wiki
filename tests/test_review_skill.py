"""Validate the repository code-review skill and its documented request path.

The review integration is GitHub Copilot code review with a project agent
skill in its supported location (``.github/skills/code-review/SKILL.md``).
These tests keep that skill grounded, keep its non-negotiable boundaries in
place, and keep ``CONTRIBUTING.md`` documenting how contributors request a
review. They are static checks, in the style of ``tests/test_init.py``: the
review process itself is deliberately not asserted here.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".github" / "skills" / "code-review" / "SKILL.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"

# Documents a review must be grounded in (issue #129): contribution and
# validation rules, the contracts, and the architecture/security guidance.
# Every entry is also checked to exist, so the skill cannot reference a
# document that has been renamed or removed.
GROUNDING_PATHS = (
    "AGENTS.md",
    "CONTRIBUTING.md",
    "README.md",
    "SECURITY.md",
    "contracts/ingest-status/v1",
    "contracts/provider-manifest/v1",
    "docs/architecture.md",
    "docs/data-layout.md",
    "docs/agents/issue-tracker.md",
)

# The non-negotiable boundaries from issue #129 (private data, content-free
# errors, published generations, no migrations before 1.0.0) plus the trust
# boundary for contributor-controlled review content. Phrase checks are
# reserved for these security-relevant rules; the rest of the skill is
# reviewed, not asserted.
BOUNDARY_PHRASES = (
    "real source documents",
    "content-free",
    "source generations",
    "migration code",
    "legacy-path fallbacks",
    "contributor-controlled",
    "prompt injection",
    "secret access",
)


class CodeReviewSkillLocationTests(unittest.TestCase):
    def test_skill_lives_in_the_supported_copilot_location(self):
        self.assertTrue(SKILL.is_file(), "missing .github/skills/code-review/SKILL.md")

    def test_frontmatter_matches_the_skill_directory(self):
        text = SKILL.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("---\n"), "skill must start with YAML frontmatter")
        self.assertIn("name: code-review\n", text)
        self.assertIn("description: ", text)


class CodeReviewSkillGroundingTests(unittest.TestCase):
    def test_skill_grounds_reviews_in_repository_documentation(self):
        text = SKILL.read_text(encoding="utf-8")
        for path in GROUNDING_PATHS:
            with self.subTest(path=path):
                self.assertIn(path, text, f"skill does not reference {path}")
                self.assertTrue((ROOT / path).exists(), f"referenced {path} does not exist")

    def test_skill_states_the_non_negotiable_boundaries(self):
        text = SKILL.read_text(encoding="utf-8")
        for phrase in BOUNDARY_PHRASES:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)


class ContributingReviewRequestTests(unittest.TestCase):
    def test_contributing_documents_how_to_request_a_review(self):
        text = CONTRIBUTING.read_text(encoding="utf-8")
        self.assertIn("## Code review", text)
        self.assertIn(".github/skills/code-review/SKILL.md", text)
        self.assertIn("Ask Copilot to review", text)
        self.assertIn("--add-reviewer @copilot", text)
        self.assertIn("copilot-pull-request-reviewer[bot]", text)


if __name__ == "__main__":
    unittest.main()
