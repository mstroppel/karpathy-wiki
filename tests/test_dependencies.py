"""Validate repository-managed dependency metadata (supply-chain checks).

These tests keep the version pins, digests, and lockfiles consistent. They are
static checks: CI additionally runs ``npm ci``, which fails when the JavaScript
manifest and lockfile drift apart.
"""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENCODE_DOCKERFILE = ROOT / "opencode" / "Dockerfile"
INGEST_DOCKERFILE = ROOT / "ingest" / "Dockerfile"
OPENCODE_UPDATE_WORKFLOW = ROOT / ".github" / "workflows" / "opencode-update.yml"
BAKE_FILE = ROOT / "docker-bake.hcl"
ENV_EXAMPLE = ROOT / ".env.example"
DEPENDABOT = ROOT / ".github" / "dependabot.yml"
PACKAGE_JSON = ROOT / "package.json"
PACKAGE_LOCK = ROOT / "package-lock.json"

# Runtime base images must be pinned by digest; the rclone stage is a
# build-time binary source pinned by tag and digest via the RCLONE_VERSION
# build argument (docker-bake.hcl).
DIGEST_RE = re.compile(r"^FROM \S+@sha256:[0-9a-f]{64}$")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


class RuntimeBaseImageTests(unittest.TestCase):
    def test_runtime_base_images_are_pinned_by_digest(self):
        for path in (OPENCODE_DOCKERFILE, INGEST_DOCKERFILE):
            with self.subTest(dockerfile=str(path.relative_to(ROOT))):
                self.assertTrue(
                    any(DIGEST_RE.fullmatch(line) for line in read(path).splitlines()),
                    msg=f"no digest-pinned FROM line in {path.relative_to(ROOT)}",
                )

    def test_every_unpinned_from_is_a_build_arg_reference(self):
        for path in (OPENCODE_DOCKERFILE, INGEST_DOCKERFILE):
            with self.subTest(dockerfile=str(path.relative_to(ROOT))):
                for line in read(path).splitlines():
                    if not line.startswith("FROM ") or DIGEST_RE.fullmatch(line):
                        continue
                    self.assertIn("${", line, f"unpinned base image: {line}")


class OpenCodeDownloadTests(unittest.TestCase):
    def test_opencode_version_and_checksums_are_pinned(self):
        content = read(OPENCODE_DOCKERFILE)
        self.assertRegex(content, r"(?m)^ARG OPENCODE_VERSION=\S+$", msg=content)
        self.assertRegex(content, r"(?m)^ARG OPENCODE_SHA512_LINUX_X64=[0-9a-f]{128}$", msg=content)
        self.assertRegex(
            content, r"(?m)^ARG OPENCODE_SHA512_LINUX_ARM64=[0-9a-f]{128}$", msg=content
        )

    def test_download_is_checksum_verified(self):
        content = read(OPENCODE_DOCKERFILE)
        self.assertIn("sha512sum --check", content)
        self.assertNotIn("sha512sum -c -", content)  # spelling: --check

    def test_update_automation_bumps_version_and_checksums(self):
        workflow = read(OPENCODE_UPDATE_WORKFLOW)
        self.assertIn("schedule:", workflow)
        self.assertIn("registry.npmjs.org/@opencode/cli-linux-x64/latest", workflow)
        self.assertIn("@opencode/cli-$1/-/cli-$1-", workflow)
        self.assertIn("download linux-x64", workflow)
        self.assertIn("download linux-arm64", workflow)
        self.assertIn("sha512sum", workflow)
        self.assertIn("OPENCODE_VERSION", workflow)


class RcloneVersionTests(unittest.TestCase):
    def test_bake_file_and_ingest_dockerfile_agree_on_pinned_rclone_digest(self):
        bake = read(BAKE_FILE)
        match = re.search(
            r'variable\s+"RCLONE_VERSION"\s*\{[^}]*?default\s*=\s*"([^"]+)"',
            bake,
            re.DOTALL,
        )
        bake_version = match.group(1) if match else ""
        # Tag and digest must be pinned together so a registry retag cannot
        # swap the rclone binary that the ingest stage copies.
        self.assertRegex(
            bake_version,
            r"^1\.\d+\.\d+@sha256:[0-9a-f]{64}$",
            msg="docker-bake.hcl RCLONE_VERSION default",
        )
        self.assertIn(f"ARG RCLONE_VERSION={bake_version}", read(INGEST_DOCKERFILE))

    def test_env_example_obscures_with_latest_rclone(self):
        self.assertIn("docker run --rm rclone/rclone:latest obscure", read(ENV_EXAMPLE))


class PackageLockTests(unittest.TestCase):
    def test_manifest_and_lockfile_are_in_sync(self):
        manifest = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
        lock = json.loads(PACKAGE_LOCK.read_text(encoding="utf-8"))
        self.assertEqual(lock["name"], manifest["name"])
        self.assertEqual(lock["lockfileVersion"], 3)
        root = lock["packages"][""]
        self.assertEqual(root["devDependencies"], manifest["devDependencies"], msg="npm ci in CI")
        for name in manifest["devDependencies"]:
            with self.subTest(package=name):
                self.assertIn(f"node_modules/{name}", lock["packages"])


class DependabotCoverageTests(unittest.TestCase):
    def test_all_package_sources_are_covered(self):
        config = read(DEPENDABOT)
        for ecosystem, directory in (
            ("npm", "directory: /"),
            ("docker", "directory: /opencode"),
            ("docker", "directory: /ingest"),
            ("docker-compose", "directory: /"),
            ("github-actions", "directory: /"),
        ):
            with self.subTest(ecosystem=ecosystem, directory=directory):
                blocks = config.split("- package-ecosystem:")
                self.assertTrue(
                    any(
                        block.lstrip().startswith(ecosystem) and directory in block
                        for block in blocks[1:]
                    )
                )


if __name__ == "__main__":
    unittest.main()
