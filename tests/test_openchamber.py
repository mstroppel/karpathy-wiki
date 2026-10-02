"""Release wiring and access boundaries of the primary chat service."""

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def service(name: str) -> str:
    content = (ROOT / "compose.yaml").read_text()
    match = re.search(rf"(?ms)^  {name}:\n(.*?)(?=^  [\w-]+:|^\S|\Z)", content)
    assert match is not None, f"missing service: {name}"
    return match[1]


class OpenChamberTests(unittest.TestCase):
    def test_chat_is_a_core_service_and_backend_is_private(self):
        chat = service("openchamber")
        backend = service("opencode")
        self.assertNotIn("profiles:", chat)
        self.assertIn("${STACK_ID:-karpathy-wiki}-openchamber", chat)
        self.assertNotIn("webproxy:", backend)
        self.assertNotIn("ports:", chat + backend)
        self.assertIn("OPENCODE_HOST: http://opencode:4096", chat)
        self.assertIn("OPENCODE_SKIP_START:", chat)
        self.assertIn("service_healthy", chat)

    def test_frontend_has_only_read_only_wiki_and_own_settings_mounts(self):
        chat = service("openchamber")
        self.assertIn("/wiki:/knowledge/wiki:ro", chat)
        self.assertIn("/openchamber:/home/openchamber/.config/openchamber:rw", chat)
        forbidden_paths = (
            "/sources",
            "/incoming",
            "/speech",
            "/models",
            "/opencode/",
            "docker.sock",
        )
        for forbidden in forbidden_paths:
            self.assertNotIn(forbidden, chat)
        self.assertIn("read_only: true", chat)
        self.assertIn("no-new-privileges:true", chat)
        self.assertIn("OPENCHAMBER_UI_PASSWORD is required", chat)

    def test_chat_dependencies_are_locked_and_build_uses_shared_cli(self):
        manifest = json.loads((ROOT / "openchamber/package.json").read_text())
        lock = json.loads((ROOT / "openchamber/package-lock.json").read_text())
        self.assertEqual(lock["packages"][""]["dependencies"], manifest["dependencies"])
        for name, package in lock["packages"].items():
            if name:
                self.assertIn("integrity", package, name)
        dockerfile = (ROOT / "opencode/Dockerfile").read_text()
        self.assertIn("AS openchamber", dockerfile)
        self.assertIn("COPY --from=opencode /usr/local/bin/opencode", dockerfile)
        self.assertIn("npm ci --omit=dev --ignore-scripts", dockerfile)

    def test_chat_is_built_for_preview_release_and_stable(self):
        bake = (ROOT / "docker-bake.hcl").read_text()
        for name in ("openchamber", "openchamber-release", "openchamber-stable"):
            self.assertIn(f'target "{name}"', bake)
        for group in ("default", "release", "stable"):
            match = re.search(rf'(?s)group "{group}" \{{(.*?)\}}', bake)
            assert match is not None, f"missing group: {group}"
            block = match[1]
            self.assertIn("openchamber", block)
        self.assertIn("directory: /openchamber", (ROOT / ".github/dependabot.yml").read_text())
        prerelease = (ROOT / ".github/workflows/pre-release.yml").read_text()
        self.assertIn("needs: [tests, lint, chat-integration]", prerelease)
        publication = (ROOT / ".github/workflows/images.yml").read_text()
        self.assertLess(
            publication.index("sh tests/integration/chat.sh"),
            publication.index("name: Build and publish images"),
        )

    def test_no_evaluation_artifacts_remain(self):
        self.assertFalse((ROOT / "docs/openchamber-evaluation.md").exists())
        self.assertFalse((ROOT / "tests/integration/openchamber-smoke.sh").exists())
        self.assertFalse((ROOT / "tests/integration/openchamber-smoke.mjs").exists())


if __name__ == "__main__":
    unittest.main()
