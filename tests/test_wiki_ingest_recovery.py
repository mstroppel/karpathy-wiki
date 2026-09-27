import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRANSACTION_TOOL = ROOT / "config" / "tools" / "wiki-ingest-transaction.sh"


class WikiIngestRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = Path(self.temp.name)
        self.run_git("init", "--initial-branch=main")
        self.run_git("config", "user.name", "Wiki Test")
        self.run_git("config", "user.email", "wiki-test@example.invalid")
        for name, content in (
            ("index.md", "# Index\n"),
            ("overview.md", "# Overview\n"),
            ("log.md", "# Log\n"),
            (
                "sources/60 Schwangerschaft/Ärztliche Notizen d.md",
                "# Existing similar path\n",
            ),
        ):
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.run_git("add", "--all")
        self.run_git("commit", "-m", "chore: initialize test wiki")
        self.baseline = self.run_git("rev-parse", "HEAD").stdout.strip()

    def tearDown(self):
        self.temp.cleanup()

    def run_git(self, *args, check=True):
        result = subprocess.run(
            ["git", *args],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode:
            self.fail(f"git {' '.join(args)} failed: {result.stderr}")
        return result

    def run_tool(self, *args, content=None, check=True):
        result = subprocess.run(
            [str(TRANSACTION_TOOL), *args],
            cwd=self.repo,
            input=content,
            capture_output=True,
            text=True,
            check=False,
        )
        if check and result.returncode:
            self.fail(f"transaction tool {' '.join(args)} failed: {result.stderr}")
        return result

    def begin(self):
        self.run_tool("begin", content="paperless\n42\nrevision-1\n")

    def test_recovery_restores_changed_files_and_removes_new_files(self):
        self.begin()
        self.run_tool("write", "index.md", content="# First transaction write\n")
        self.run_tool("write", "index.md", content="# Updated index\n")
        self.run_tool("write", "sources/42.md", content="# Imported source\n")
        self.run_git("add", "index.md", "sources/42.md")

        result = self.run_tool("recover")

        self.assertIn("rolled back", result.stdout)
        self.assertEqual((self.repo / "index.md").read_text(), "# Index\n")
        self.assertFalse((self.repo / "sources/42.md").exists())
        self.assertEqual(self.run_git("status", "--porcelain").stdout, "")
        self.assertFalse((self.repo / ".git/wiki-ingest-recovery").exists())

    def test_recovery_preserves_a_file_changed_after_interruption(self):
        self.begin()
        self.run_tool("write", "index.md", content="# Transaction output\n")
        (self.repo / "index.md").write_text("# Later user edit\n")

        result = self.run_tool("recover", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed outside the transaction", result.stderr)
        self.assertEqual((self.repo / "index.md").read_text(), "# Later user edit\n")
        self.assertTrue((self.repo / ".git/wiki-ingest-recovery").exists())

    def test_first_write_refuses_a_file_changed_since_transaction_start(self):
        self.begin()
        (self.repo / "index.md").write_text("# Concurrent user edit\n")

        result = self.run_tool("write", "index.md", content="# Ingest output\n", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed after the transaction began", result.stderr)
        self.assertEqual((self.repo / "index.md").read_text(), "# Concurrent user edit\n")
        recovery = self.run_tool("recover", check=False)
        self.assertNotEqual(recovery.returncode, 0)
        self.assertIn("empty transaction has wiki changes", recovery.stderr)
        self.assertEqual((self.repo / "index.md").read_text(), "# Concurrent user edit\n")
        self.assertTrue((self.repo / ".git/wiki-ingest-recovery").exists())

    def test_recovery_preserves_unrelated_worktree_changes(self):
        self.begin()
        self.run_tool("write", "index.md", content="# Transaction output\n")
        (self.repo / "unrelated.md").write_text("# Other work\n")

        result = self.run_tool("recover", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unrelated wiki changes", result.stderr)
        self.assertEqual((self.repo / "index.md").read_text(), "# Transaction output\n")
        self.assertEqual((self.repo / "unrelated.md").read_text(), "# Other work\n")

    def test_recovery_preserves_unrecognized_staged_content(self):
        self.begin()
        self.run_tool("write", "index.md", content="# Transaction output\n")
        blob = self.run_git("hash-object", "-w", "--stdin", check=True).stdout.strip()
        self.run_git("update-index", "--cacheinfo", f"100644,{blob},index.md")

        result = self.run_tool("recover", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("staged file changed outside the transaction", result.stderr)
        self.assertEqual((self.repo / "index.md").read_text(), "# Transaction output\n")
        self.assertTrue((self.repo / ".git/wiki-ingest-recovery").exists())

    def test_recovery_preserves_unrecognized_staged_mode(self):
        self.begin()
        self.run_tool("write", "index.md", content="# Transaction output\n")
        self.run_git("update-index", "--chmod=+x", "index.md")

        result = self.run_tool("recover", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("staged file mode changed outside the transaction", result.stderr)
        self.assertEqual((self.repo / "index.md").read_text(), "# Transaction output\n")
        self.assertTrue((self.repo / ".git/wiki-ingest-recovery").exists())

    def test_begin_refuses_to_adopt_preexisting_work(self):
        (self.repo / "overview.md").write_text("# Existing pending edit\n")

        result = self.run_tool("begin", content="webdav\nsource-a\nrevision-a\n", check=False)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("worktree must be clean", result.stderr)
        self.assertEqual((self.repo / "overview.md").read_text(), "# Existing pending edit\n")
        self.assertFalse((self.repo / ".git/wiki-ingest-recovery").exists())

    def test_commit_contains_only_files_recorded_for_source(self):
        self.begin()
        self.run_tool("write", "index.md", content="# Imported index\n")
        source_path = "sources/60 Schwangerschaft/Ärztliche Notizen [draft].md"
        self.run_tool("write", source_path, content="# Imported source\n")

        result = self.run_tool("commit", content="feat(wiki): import paperless source 42\n")

        self.assertIn("Committed source transaction", result.stdout)
        committed_paths = self.run_git(
            "-c",
            "core.quotePath=false",
            "show",
            "--format=",
            "--name-only",
            "HEAD",
        ).stdout.splitlines()
        self.assertEqual(committed_paths, ["index.md", source_path])
        self.assertEqual(self.run_git("status", "--porcelain").stdout, "")
        self.assertFalse((self.repo / ".git/wiki-ingest-recovery").exists())

    def test_commit_omits_journaled_files_with_unchanged_content(self):
        self.begin()
        self.run_tool("write", "index.md", content="# Index\n")
        self.run_tool("write", "overview.md", content="# Updated overview\n")

        result = self.run_tool("commit", content="feat(wiki): import source with unchanged index\n")

        self.assertIn("Committed source transaction", result.stdout)
        committed_paths = self.run_git(
            "show", "--format=", "--name-only", "HEAD"
        ).stdout.splitlines()
        self.assertEqual(committed_paths, ["overview.md"])
        self.assertEqual(self.run_git("status", "--porcelain").stdout, "")

    def test_recovery_restores_original_file_permissions(self):
        os.chmod(self.repo / "overview.md", 0o640)
        self.begin()
        self.run_tool("write", "overview.md", content="# Changed overview\n")

        self.run_tool("recover")

        self.assertEqual((self.repo / "overview.md").stat().st_mode & 0o777, 0o640)
        self.assertEqual((self.repo / "overview.md").read_text(), "# Overview\n")

    def test_recovery_finalizes_a_commit_completed_before_journal_cleanup(self):
        self.begin()
        self.run_tool("write", "index.md", content="# Committed index\n")
        self.run_git("add", "index.md")
        self.run_git("commit", "-m", "feat(wiki): import paperless source 42")

        result = self.run_tool("recover")

        self.assertIn("Finalized already-committed transaction", result.stdout)
        self.assertEqual(
            self.run_git("rev-parse", "HEAD^", check=False).stdout.strip(), self.baseline
        )
        self.assertEqual((self.repo / "index.md").read_text(), "# Committed index\n")
        self.assertEqual(self.run_git("status", "--porcelain").stdout, "")
        self.assertFalse((self.repo / ".git/wiki-ingest-recovery").exists())

    def test_recovery_clears_incomplete_setup_without_wiki_changes(self):
        state = self.repo / ".git/wiki-ingest-recovery"
        (state / "entries").mkdir(parents=True)
        (state / "baseline").write_text(self.baseline + "\n")

        result = self.run_tool("recover")

        self.assertIn("no wiki changes were made", result.stdout)
        self.assertFalse(state.exists())
        self.assertEqual(self.run_git("status", "--porcelain").stdout, "")

    def test_recovery_clears_empty_transaction_after_a_write_failure(self):
        self.begin()

        result = self.run_tool("recover")

        self.assertIn("no wiki files were written", result.stdout)
        self.assertFalse((self.repo / ".git/wiki-ingest-recovery").exists())
        self.assertEqual(self.run_git("status", "--porcelain").stdout, "")

    def test_recovery_does_not_touch_legacy_changes_without_a_journal(self):
        (self.repo / "index.md").write_text("# Existing change\n")
        (self.repo / "untracked.md").write_text("# Existing untracked file\n")

        result = self.run_tool("recover")

        self.assertIn("No interrupted ingest transaction", result.stdout)
        self.assertEqual((self.repo / "index.md").read_text(), "# Existing change\n")
        self.assertEqual((self.repo / "untracked.md").read_text(), "# Existing untracked file\n")
        self.assertNotEqual(self.run_git("status", "--porcelain").stdout, "")


if __name__ == "__main__":
    unittest.main()
