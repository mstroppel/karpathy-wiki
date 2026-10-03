"""Validate the repository code-review skill and its documented request path.

The review integration is GitHub Copilot code review with a project agent
skill in its supported location (``.github/skills/code-review/SKILL.md``).
These tests keep that skill complete: it must ground reviews in the
repository documentation, cover the mandatory review boundaries from issue
#129, and ``CONTRIBUTING.md`` must document how contributors request a
review. They are static checks, in the style of ``tests/test_init.py``.
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / ".github" / "skills" / "code-review" / "SKILL.md"
CONTRIBUTING = ROOT / "CONTRIBUTING.md"

# Documents a review must be grounded in (issue #129): contribution and
# validation rules, the contracts, and the architecture/security guidance.
GROUNDING_PATHS = (
    "AGENTS.md",
    "CONTRIBUTING.md",
    "README.md",
    "SECURITY.md",
    "contracts",
    "docs/architecture.md",
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class CodeReviewSkillLocationTests(unittest.TestCase):
    def test_skill_lives_in_the_supported_copilot_location(self):
        self.assertTrue(SKILL.is_file(), "missing .github/skills/code-review/SKILL.md")

    def test_frontmatter_matches_the_skill_directory(self):
        text = read(SKILL)
        self.assertTrue(text.startswith("---\n"), "skill must start with YAML frontmatter")
        self.assertIn("name: code-review\n", text)
        self.assertIn("description: ", text)


class CodeReviewSkillGroundingTests(unittest.TestCase):
    def test_skill_grounds_reviews_in_repository_documentation(self):
        text = read(SKILL)
        for path in GROUNDING_PATHS:
            with self.subTest(path=path):
                self.assertIn(path, text, f"skill does not reference {path}")
                self.assertTrue((ROOT / path).exists(), f"referenced {path} does not exist")

    def test_skill_covers_the_mandatory_review_boundaries(self):
        text = read(SKILL)
        for phrase in (
            "acceptance criteria",
            "file/line",
            "validation evidence",
            "private",
            "content-free",
            "generation",
            "migration",
            "legacy",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_skill_requires_actionable_findings(self):
        text = read(SKILL)
        self.assertIn("file/line", text)
        self.assertIn("no findings", text)


class ContributingReviewRequestTests(unittest.TestCase):
    def test_contributing_documents_how_to_request_a_review(self):
        text = read(CONTRIBUTING)
        self.assertIn("## Code review", text)
        self.assertIn(".github/skills/code-review/SKILL.md", text)
        self.assertIn("review", text.lower())


if __name__ == "__main__":
    unittest.main()
