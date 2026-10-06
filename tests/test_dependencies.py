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
BUILD_REQUIREMENTS = ROOT / "ingest" / "build-requirements.txt"

# Runtime base images must be pinned by digest; the rclone stage is a
# build-time binary source pinned by tag and digest via the RCLONE_VERSION
# build argument (docker-bake.hcl). Internal stages (FROM core AS webdav)
# reuse an earlier stage of the same build and are not base images.
DIGEST_RE = re.compile(r"^FROM \S+@sha256:[0-9a-f]{64}(?: AS \S+)?$")
INTERNAL_STAGE_RE = re.compile(r"^FROM core(?: AS \S+)?$")


# The PEP 517 build backend for the ingest distributions. The version is
# pinned in exactly one place: ingest/build-requirements.txt (hash-pinned so
# image builds never resolve unpinned tooling from the package index). Every
# pyproject.toml must agree with that pin (IngestPackageTests); the tests
# derive the version from the file so a bump stays a single-manifest change.
SETUPTOOLS_REQUIREMENT = r"(?m)^setuptools==(\d[\w.]*)\s*\\?$"


def ingest_setuptools_version() -> str:
    content = read(BUILD_REQUIREMENTS)
    match = re.search(SETUPTOOLS_REQUIREMENT, content, re.MULTILINE)
    assert match is not None, "no pinned setuptools in ingest/build-requirements.txt"
    return match.group(1)


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
        for directory in ("core", "webdav", "audio", "speech", "paperless"):
            with self.subTest(package=directory):
                project = read_ingest_project(directory)
                expected_name = {
                    "core": "karpathy-wiki-ingest",
                    "speech": "karpathy-wiki-speech",
                }.get(directory, f"karpathy-wiki-ingest-{directory}")
                self.assertEqual(project["name"], expected_name)
                versions[directory] = project["version"]
        self.assertEqual(len(set(versions.values())), 1, "ingest distributions ship in lockstep")

    def test_plugins_depend_on_the_core_version(self):
        version = read_ingest_project("core")["version"]
        for directory in ("webdav", "audio", "paperless"):
            with self.subTest(package=directory):
                dependencies = read_ingest_project(directory)["dependencies"]
                self.assertIn(f"karpathy-wiki-ingest=={version}", dependencies)
        # The audio provider additionally uses the shared speech library.
        audio_dependencies = read_ingest_project("audio")["dependencies"]
        self.assertIn(f"karpathy-wiki-speech=={version}", audio_dependencies)

    def test_speech_runtime_requirements_are_hash_pinned(self):
        requirements = read(ROOT / "ingest" / "speech" / "requirements.txt")
        pins: dict[str, set[str]] = {}
        for line in requirements.replace("\\\n", " ").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            with self.subTest(requirement=line):
                self.assertRegex(line, r"^[\w-]+==\d[\w.]*(?:\s+--hash=sha256:[0-9a-f]{64})+$")
                name = line.split("==", 1)[0].replace("-", "_").lower()
                self.assertNotIn(name, pins, "duplicate speech dependency")
                pins[name] = set(re.findall(r"--hash=sha256:([0-9a-f]{64})", line))
        self.assertIn("faster_whisper", pins)
        # These native dependencies need distinct amd64 and arm64 wheels on
        # CPython 3.14 (the speech stage base). Catch missing pins and
        # single-architecture lock updates; actual wheel compatibility/digests
        # are verified with pip download.
        for name in (
            "ctranslate2",
            "av",
            "tokenizers",
            "onnxruntime",
            "hf_xet",
            "numpy",
            "protobuf",
            "pyyaml",
        ):
            with self.subTest(native_dependency=name):
                self.assertIn(name, pins)
                self.assertGreaterEqual(len(pins[name]), 2, "missing amd64/arm64 wheel hashes")

    def test_speech_optional_dependencies_are_hash_pinned(self):
        """Every speech extra installs from the hash-pinned runtime lockfile."""
        requirements = read(ROOT / "ingest" / "speech" / "requirements.txt")
        pinned = {
            (line.split("==", 1)[0].replace("-", "_").lower(), line.split("==", 1)[1].split()[0])
            for line in requirements.replace("\\\n", " ").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        for extra, dependencies in read_ingest_project("speech")["optional-dependencies"].items():
            for dependency in dependencies:
                with self.subTest(extra=extra, dependency=dependency):
                    self.assertRegex(dependency, r"^[\w-]+==\d[\w.]*$")
                    name, version = dependency.split("==", 1)
                    self.assertIn((name.replace("-", "_").lower(), version), pinned)

    def test_build_backends_are_pinned(self):
        requires_pin = f"setuptools=={ingest_setuptools_version()}"
        for directory in ("core", "webdav", "audio", "speech", "paperless"):
            with self.subTest(package=directory):
                data = tomllib.loads(read(ROOT / "ingest" / directory / "pyproject.toml"))
                self.assertIn(requires_pin, data["build-system"]["requires"])

    def test_bake_and_compose_target_the_split_ingest_images(self):
        bake = read(BAKE_FILE)
        for target in (
            "ingest",
            "ingest-webdav",
            "ingest-audio",
            "ingest-speech",
            "ingest-paperless",
        ):
            self.assertIn(f'target "{target}"', bake)
        compose = read(ROOT / "compose.yaml")
        for image in (
            "karpathy-wiki-ingest-webdav",
            "karpathy-wiki-ingest-audio",
            "karpathy-wiki-ingest-speech",
            "karpathy-wiki-ingest-paperless",
        ):
            self.assertIn(f"ghcr.io/mstroppel/{image}", compose)


class IngestBuildIsolationTests(unittest.TestCase):
    """The ingest wheel build must not execute index-resolved tooling."""

    def test_build_backend_is_hash_pinned_and_preinstalled(self):
        content = read(INGEST_DOCKERFILE)
        self.assertIn("--no-build-isolation", content)
        self.assertIn("--require-hashes", content)
        requirements = read(BUILD_REQUIREMENTS)
        self.assertRegex(requirements, SETUPTOOLS_REQUIREMENT)
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
            if "build-requirements.txt" in line or "speech-requirements.txt" in line:
                # The hash-pinned build backend preinstall and the hash-pinned
                # speech runtime are the only index accesses; both install
                # with --require-hashes so nothing is resolved unpinned.
                self.assertIn("--require-hashes", line)
                self.assertTrue(
                    "--no-deps" in line or "--only-binary=:all:" in line,
                    f"unpinned wheel resolution: {line}",
                )
            elif "/wheels" in line:
                self.assertIn("--no-index", line)


class IngestImageIsolationTests(unittest.TestCase):
    """No final stage may contain another plugin's wheel in any layer.

    ``karpathy-wiki-speech`` is a shared library (like the core), not a
    plugin: the audio stage ships it alongside the audio wheel, and the
    speech stage ships it alone.
    """

    PLUGIN_WHEEL_DIRS = ("webdav", "paperless")
    SHARED_WHEEL_DIRS = ("core", "speech")

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
        for stage, plugin in (
            ("core", None),
            ("webdav", "webdav"),
            ("audio", "audio"),
            ("speech", None),
            ("paperless", "paperless"),
        ):
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

    def test_audio_stage_ships_the_shared_speech_library(self):
        block = "\n".join(self.ingest_stage("audio"))
        self.assertIn("/wheels/speech", block)


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
            ("pip", "directory: /ingest"),
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
