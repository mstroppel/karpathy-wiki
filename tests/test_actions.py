"""Regression checks for runner registry configuration and update-head races."""

import json
import os
import subprocess
import tempfile
import textwrap
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


class RegistryMirrorTests(unittest.TestCase):
    def test_daemon_setup_preserves_existing_configuration_and_is_idempotent(self):
        script = (ROOT / "scripts/setup-ci-docker.sh").read_text()
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "daemon.json"
            config.write_text('{"debug":true,"registry-mirrors":["https://other.example"]}')
            script = script.replace("config=/etc/docker/daemon.json", f"config={config}")
            # Run without sudo or access to the host daemon.
            stubs = 'sudo() { "$@"; }\nsystemctl() { :; }\ndocker() { :; }\n'
            for _ in range(2):
                result = subprocess.run(
                    ["sh", "-eu", "-c", stubs + script],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    json.loads(config.read_text()),
                    {
                        "debug": True,
                        "registry-mirrors": ["https://mirror.gcr.io", "https://other.example"],
                    },
                )

    def test_container_builders_use_mirror_configuration(self):
        config = tomllib.loads((ROOT / ".github/docker/buildkitd.toml").read_text())
        self.assertEqual(config["registry"]["docker.io"]["mirrors"], ["mirror.gcr.io"])
        for name in ("ci.yml", "pre-release.yml", "images.yml"):
            with self.subTest(workflow=name):
                workflow = (WORKFLOWS / name).read_text()
                self.assertIn(
                    "uses: docker/setup-buildx-action@v4\n"
                    "        with:\n"
                    "          buildkitd-config: .github/docker/buildkitd.toml",
                    workflow,
                )
                builder_job = next(
                    job
                    for job in workflow.split("    steps:")[1:]
                    if "uses: docker/setup-buildx-action@v4" in job
                )
                self.assertLess(
                    builder_job.index("run: sh scripts/setup-ci-docker.sh"),
                    builder_job.index("uses: docker/setup-buildx-action@v4"),
                )

    def test_daemon_mirror_is_configured_before_each_integration_build(self):
        for name in ("ci.yml", "pre-release.yml", "images.yml"):
            workflow = (WORKFLOWS / name).read_text()
            for job in workflow.split("    steps:")[1:]:
                if "docker build " not in job and "run: tests/integration/run.sh" not in job:
                    continue
                with self.subTest(workflow=name):
                    setup = job.index("run: sh scripts/setup-ci-docker.sh")
                    build = min(
                        job.index(token)
                        for token in ("          docker build ", "run: tests/integration/run.sh")
                        if token in job
                    )
                    self.assertLess(setup, build)


class OpenCodeUpdateHeadTests(unittest.TestCase):
    def run_verifier(self, *, stale_reads=0, changed=False, failed=False, dispatch=False):
        workflow = (WORKFLOWS / "opencode-update.yml").read_text()
        function = textwrap.dedent(
            workflow.split("          verify_ci_and_maybe_merge() {", 1)[1].split(
                "\n          }", 1
            )[0]
        )
        # Execute the actual workflow function with a synthetic GitHub API.
        # No credentials, network, pushes, dispatches or merges are used.
        stub = r"""
gh() {
  printf '%s\n' "$*" >> "$MOCK_LOG"
  case "$1 $2" in
    'pr view')
      case "$*" in
        *headRefOid*)
          count=$(cat "$MOCK_COUNT")
          count=$((count + 1))
          printf '%s' "$count" > "$MOCK_COUNT"
          if [ "$count" -le "$STALE_READS" ]; then
            printf 'old-head\n'
          elif [ "$CHANGED" = 1 ] && grep -q '^run watch' "$MOCK_LOG"; then
            printf 'other-head\n'
          else
            printf 'expected-head\n'
          fi ;;
        *) printf '42\n' ;;
      esac ;;
    'run list')
      # The query must select the authoritative commit, never stale metadata.
      case "$*" in
        *expected-head*) ;;
        *) return 99 ;;
      esac
      if [ "$DISPATCH" = 0 ] || grep -q '^workflow run' "$MOCK_LOG"; then
        printf '{"databaseId":123,"status":"completed","conclusion":"success"}\n'
      fi ;;
    'run watch') [ "$FAILED" = 0 ] ;;
    'workflow run'|'pr merge') return 0 ;;
    *) return 98 ;;
  esac
}
sleep() { :; }
"""
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "calls"
            count = Path(directory) / "count"
            count.write_text("0")
            result = subprocess.run(
                [
                    "sh",
                    "-eu",
                    "-c",
                    stub
                    + "\nverify_ci_and_maybe_merge() {\n"
                    + function
                    + "\n}\nauto_merge=true\n"
                    + "verify_ci_and_maybe_merge mock-pr expected-head\n",
                ],
                env={
                    **os.environ,
                    "MOCK_LOG": str(log),
                    "MOCK_COUNT": str(count),
                    "STALE_READS": str(stale_reads),
                    "CHANGED": str(int(changed)),
                    "FAILED": str(int(failed)),
                    "DISPATCH": str(int(dispatch)),
                },
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result, log.read_text().splitlines()

    def test_stale_pr_metadata_waits_before_selecting_ci(self):
        result, calls = self.run_verifier(stale_reads=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        first_run = next(index for index, call in enumerate(calls) if call.startswith("run list"))
        self.assertEqual(sum("headRefOid" in call for call in calls[:first_run]), 3)
        self.assertIn("pr merge --auto --squash --match-head-commit expected-head mock-pr", calls)

    def test_metadata_timeout_never_selects_old_ci_or_merges(self):
        result, calls = self.run_verifier(stale_reads=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PR head did not reach expected commit", result.stderr)
        self.assertFalse(
            any(call.startswith(("run ", "workflow run", "pr merge")) for call in calls)
        )

    def test_head_change_after_green_ci_blocks_merge(self):
        result, calls = self.run_verifier(changed=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("PR head changed while CI was running", result.stderr)
        self.assertFalse(any(call.startswith("pr merge") for call in calls))

    def test_failed_ci_blocks_merge(self):
        result, calls = self.run_verifier(failed=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(any(call.startswith("pr merge") for call in calls))

    def test_missing_ci_dispatches_preview_for_expected_head(self):
        result, calls = self.run_verifier(dispatch=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "workflow run ci.yml --ref opencode-update --field pull_request_number=42", calls
        )
        self.assertIn("run watch 123 --exit-status", calls)

    def test_callers_supply_git_commit_not_pr_metadata(self):
        workflow = (WORKFLOWS / "opencode-update.yml").read_text()
        for ref in ("FETCH_HEAD", "HEAD"):
            self.assertIn(
                f'verify_ci_and_maybe_merge "$existing_pr_url" "$(git rev-parse {ref})"',
                workflow,
            )
