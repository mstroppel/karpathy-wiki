"""Contracts for cached amd64 builds, parallel gates and digest-only promotion."""

import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PROMOTE = runpy.run_path(str(ROOT / "scripts/promote-images.py"))


class PipelineTests(unittest.TestCase):
    def test_only_amd64_is_built_and_each_image_has_its_own_opt_in_cache(self):
        bake = (ROOT / "docker-bake.hcl").read_text()
        self.assertIn('platforms = ["linux/amd64"]', bake)
        for image in PROMOTE["IMAGES"]:
            self.assertIn(f'type=gha,scope=kw-{image}"', bake)
            self.assertIn(f'type=gha,scope=kw-{image},mode=max"', bake)
        for name in ("ci.yml", "images.yml", "pre-release.yml"):
            workflow = (ROOT / ".github/workflows" / name).read_text()
            self.assertNotIn("setup-qemu", workflow)
            self.assertNotIn("linux/arm64", workflow)

    def test_all_six_integration_suites_run_independently_and_gate_release(self):
        ci = (ROOT / ".github/workflows/ci.yml").read_text()
        integration = ci.split("  integration:\n", 1)[1].split("  integration-gate:", 1)[0]
        self.assertNotIn("needs:", integration)
        self.assertIn("fail-fast: false", integration)
        for suite in (
            "run",
            "email",
            "launcher-backup",
            "chat",
            "batch-ingest",
            "runtime-write-isolation",
        ):
            self.assertIn(f"script: tests/integration/{suite}.sh", integration)
        gate = ci.split("  integration-gate:", 1)[1].split("  images:", 1)[0]
        self.assertIn("name: Compose integration", gate)
        self.assertIn("if: always()", gate)
        self.assertIn('test "$RESULT" = success', gate)
        self.assertIn("needs: [tests, lint, integration-gate, images]", ci)

    def test_prerelease_reuses_current_run_metadata_without_running_checks_or_builds(self):
        workflow = (ROOT / ".github/workflows/pre-release.yml").read_text()
        self.assertIn("workflow_call:", workflow)
        self.assertNotIn("workflow_dispatch:", workflow)
        self.assertNotIn("  push:", workflow)
        for duplicate in ("npm test", "pip install", "docker build", "publish-images.sh"):
            self.assertNotIn(duplicate, workflow)
        self.assertIn("name: release-image-metadata", workflow)
        self.assertNotIn("run-id:", workflow)
        self.assertIn("python3 scripts/promote-images.py image-metadata.json", workflow)
        self.assertIn("github.event_name == 'push' && github.ref == 'refs/heads/main'", workflow)

    def test_integration_image_loader_rejects_unknown_images_before_building(self):
        result = subprocess.run(
            ["sh", str(ROOT / "scripts/build-integration-images.sh"), "untrusted"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("Unsupported integration image", result.stderr)


class PromotionTests(unittest.TestCase):
    def metadata(self):
        return {
            f"{image}-release": {"containerimage.digest": "sha256:" + f"{index:064x}"}
            for index, image in enumerate(PROMOTE["IMAGES"])
        }

    def promote(self, metadata, version="0.2.2-pre.1", failures=0):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "metadata.json"
            source.write_text(json.dumps(metadata))
            with (
                patch.dict(os.environ, IMAGE_VERSION=version),
                patch.object(sys, "argv", ["promote-images.py", str(source)]),
                patch("subprocess.run") as run,
                patch("time.sleep") as sleep,
            ):
                run.side_effect = [
                    subprocess.CompletedProcess([], 17 if index < failures else 0)
                    for index in range(8 + failures)
                ]
                try:
                    PROMOTE["main"]()
                except (ValueError, KeyError, SystemExit) as error:
                    return error, run.call_args_list, sleep.call_args_list
                return None, run.call_args_list, sleep.call_args_list

    def test_promotes_all_eight_images_by_digest_not_mutable_tag(self):
        error, calls, sleeps = self.promote(self.metadata())
        self.assertIsNone(error)
        self.assertEqual(len(calls), 8)
        self.assertFalse(sleeps)
        for image, call in zip(PROMOTE["IMAGES"], calls, strict=True):
            command = call.args[0]
            self.assertEqual(command[:5], ["docker", "buildx", "imagetools", "create", "--tag"])
            self.assertEqual(command[5], f"ghcr.io/mstroppel/karpathy-wiki-{image}:0.2.2-pre.1")
            self.assertRegex(command[6], rf"/karpathy-wiki-{image}@sha256:[0-9a-f]{{64}}$")

    def test_invalid_or_missing_last_digest_prevents_all_publication(self):
        for invalid in ("bad", "sha256:" + "a" * 63, None):
            metadata = self.metadata()
            if invalid is None:
                del metadata["ingest-email-release"]
            else:
                metadata["ingest-email-release"]["containerimage.digest"] = invalid
            with self.subTest(invalid=invalid):
                error, calls, _ = self.promote(metadata)
                self.assertIsNotNone(error)
                self.assertFalse(calls)

    def test_invalid_version_prevents_publication(self):
        error, calls, _ = self.promote(self.metadata(), version="")
        self.assertIsInstance(error, ValueError)
        self.assertFalse(calls)

    def test_transient_publication_failure_retries_identical_digest(self):
        error, calls, sleeps = self.promote(self.metadata(), failures=2)
        self.assertIsNone(error)
        self.assertEqual(len(calls), 10)
        self.assertEqual(calls[:3], [calls[0]] * 3)
        self.assertEqual([call.args[0] for call in sleeps], [30, 60])

    def test_persistent_failure_stops_with_original_status(self):
        error, calls, sleeps = self.promote(self.metadata(), failures=3)
        self.assertIsInstance(error, SystemExit)
        assert isinstance(error, SystemExit)
        self.assertEqual(error.code, 17)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(sleeps), 2)
