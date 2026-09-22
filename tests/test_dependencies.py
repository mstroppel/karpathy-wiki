"""Validate repository-managed dependency metadata (supply-chain checks).

These tests keep the version pins, digests, and lockfiles consistent. They are
static checks: CI additionally runs ``npm ci``, which fails when the JavaScript
manifest and lockfile drift apart.
"""

import json
import re
import tomllib
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
# build argument (docker-bake.hcl). Internal stages (FROM core AS webdav)
# reuse an earlier stage of the same build and are not base images.
DIGEST_RE = re.compile(r"^FROM \S+@sha256:[0-9a-f]{64}(?: AS \S+)?$")
INTERNAL_STAGE_RE = re.compile(r"^FROM core(?: AS \S+)?$")


# The PEP 517 build backend for the ingest distributions, pinned so image
# builds do not resolve unpinned tooling from the package index.
SETUPTOOLS_PIN = "setuptools==80.9.0"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def read_ingest_project(directory: str) -> dict:
    path = ROOT / "ingest" / directory / "pyproject.toml"
    return tomllib.loads(read(path))["project"]


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
                    if INTERNAL_STAGE_RE.fullmatch(line):
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


class IngestPackageTests(unittest.TestCase):
    """Metadata checks for the split ingest distributions (#24)."""

    def test_ingest_distributions_are_pinned_in_lockstep(self):
        versions = {}
        for directory in ("core", "webdav", "paperless"):
            with self.subTest(package=directory):
                project = read_ingest_project(directory)
                expected_name = (
                    "karpathy-wiki-ingest"
                    if directory == "core"
                    else f"karpathy-wiki-ingest-{directory}"
                )
                self.assertEqual(project["name"], expected_name)
                versions[directory] = project["version"]
        self.assertEqual(len(set(versions.values())), 1, "ingest distributions ship in lockstep")

    def test_plugins_depend_on_the_core_version(self):
        version = read_ingest_project("core")["version"]
        for directory in ("webdav", "paperless"):
            with self.subTest(package=directory):
                self.assertIn(
                    f"karpathy-wiki-ingest=={version}",
                    read_ingest_project(directory)["dependencies"],
                )

    def test_build_backends_are_pinned(self):
        for directory in ("core", "webdav", "paperless"):
            with self.subTest(package=directory):
                data = tomllib.loads(read(ROOT / "ingest" / directory / "pyproject.toml"))
                self.assertIn(SETUPTOOLS_PIN, data["build-system"]["requires"])

    def test_bake_and_compose_target_the_split_ingest_images(self):
        bake = read(BAKE_FILE)
        for target in ("ingest", "ingest-webdav", "ingest-paperless"):
            self.assertIn(f'target "{target}"', bake)
        compose = read(ROOT / "compose.yaml")
        for image in ("karpathy-wiki-ingest-webdav", "karpathy-wiki-ingest-paperless"):
            self.assertIn(f"ghcr.io/mstroppel/{image}", compose)


class IngestBuildIsolationTests(unittest.TestCase):
    """The ingest wheel build must not execute index-resolved tooling."""

    def test_build_backend_is_hash_pinned_and_preinstalled(self):
        content = read(INGEST_DOCKERFILE)
        self.assertIn("--no-build-isolation", content)
        self.assertIn("--require-hashes", content)
        requirements = read(ROOT / "ingest" / "build-requirements.txt")
        self.assertIn(SETUPTOOLS_PIN, requirements)
        self.assertRegex(requirements, r"--hash=sha256:[0-9a-f]{64}")

    def test_wheel_builds_use_no_deps_against_repository_sources(self):
        content = read(INGEST_DOCKERFILE)
        wheel_lines = [line for line in content.splitlines() if "pip wheel" in line]
        self.assertTrue(wheel_lines, "no pip wheel build step")
        for line in wheel_lines:
            self.assertIn("--no-deps", line)
            self.assertIn("--no-build-isolation", line)

    def test_runtime_installs_resolve_from_built_wheels_only(self):
        content = read(INGEST_DOCKERFILE)
        install_lines = [line for line in content.splitlines() if "pip install" in line]
        self.assertTrue(install_lines, "no pip install step")
        for line in install_lines:
            if "build-requirements.txt" in line:
                # The hash-pinned build backend preinstall is the only index
                # access; wheel builds and image installs never resolve there.
                self.assertIn("--require-hashes", line)
                self.assertIn("--no-deps", line)
            else:
                self.assertIn("--no-index", line)


class IngestImageIsolationTests(unittest.TestCase):
    """No final stage may contain another plugin's wheel in any layer."""

    PLUGIN_WHEEL_DIRS = ("webdav", "paperless")

    def ingest_stage(self, stage: str) -> list[str]:
        lines = read(INGEST_DOCKERFILE).splitlines()
        start = next(
            index for index, line in enumerate(lines) if re.fullmatch(rf"FROM \S+ AS {stage}", line)
        )
        end = next(
            (index for index in range(start + 1, len(lines)) if lines[index].startswith("FROM ")),
            len(lines),
        )
        return lines[start:end]

    def test_each_final_stage_copies_only_its_own_plugin_wheel(self):
        for stage, plugin in (("core", None), ("webdav", "webdav"), ("paperless", "paperless")):
            with self.subTest(stage=stage):
                block = "\n".join(self.ingest_stage(stage))
                if plugin:
                    self.assertIn(f"/wheels/{plugin}", block)
                for other in self.PLUGIN_WHEEL_DIRS:
                    if other != plugin:
                        self.assertNotIn(
                            f"/wheels/{other}",
                            block,
                            msg=f"{stage} stage ships another plugin's wheel",
                        )
                self.assertNotIn("COPY --from=build /wheels /wheels", block)


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
