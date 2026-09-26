import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DOCKER_STUB = r"""#!/bin/sh
id=
for argument in "$@"; do id=$argument; done
printf '%s\n' "$id" >> "$SESSION_EXPORT_LOG"
if [ "${INVALID_EXPORT:-0}" = 1 ]; then
  printf '{"info":{"id":"%s"},"messages":{}}\n' "$id"
  exit 0
fi
case "$id" in
  ses_root)
    cat <<'EOF'
{"info":{"id":"ses_root"},"messages":[
  {"type":"assistant","content":[
    {"type":"tool","name":"subagent","state":{"metadata":{"sessionID":"ses_child"}}}
  ]}
]}
EOF
    ;;
  ses_child)
    cat <<'EOF'
{"info":{"id":"ses_child"},"messages":[
  {"type":"assistant","content":[
    {"type":"tool","name":"subagent","state":{"metadata":{"sessionID":"ses_root"}}}
  ]}
]}
EOF
    ;;
  *) exit 1 ;;
esac
"""

JQ_STUB = r"""#!/usr/bin/env python3
import json
import sys

arguments = sys.argv[1:]
with open(arguments[-1], encoding="utf-8") as exported:
    document = json.load(exported)

if arguments[0] == "-e":
    expected_id = arguments[3]
    valid = document.get("info", {}).get("id") == expected_id
    valid = valid and isinstance(document.get("messages"), list)
    sys.exit(0 if valid else 1)

for message in document.get("messages", []):
    if message.get("type") != "assistant":
        continue
    for part in message.get("content", []):
        if part.get("type") != "tool" or part.get("name") != "subagent":
            continue
        session_id = part.get("state", {}).get("metadata", {}).get("sessionID")
        if session_id:
            print(session_id)
"""


class SessionExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.project = root / "wiki"
        self.project.mkdir()
        self.cache = self.project / ".cache"
        self.cache.mkdir()
        (self.cache / "compose-v1.2.3.yaml").write_text("name: stub-compose\n")
        self.bin = root / "bin"
        self.bin.mkdir()
        self.export_log = root / "export.log"
        self.launcher = self.project / "karpathy-wiki.sh"
        self.exporter = self.project / "export-opencode-sessions.sh"
        shutil.copy(ROOT / "karpathy-wiki.sh", self.launcher)
        shutil.copy(ROOT / "export-opencode-sessions.sh", self.exporter)
        self.launcher.chmod(0o755)
        self.exporter.chmod(0o755)
        for name, contents in (("docker", DOCKER_STUB), ("jq", JQ_STUB)):
            path = self.bin / name
            path.write_text(contents)
            path.chmod(0o755)

    def run_export(self, *arguments, **overrides):
        environment = {
            **os.environ,
            "PATH": f"{self.bin}{os.pathsep}{os.environ['PATH']}",
            "KARPATHY_WIKI_VERSION": "1.2.3",
            "SESSION_EXPORT_LOG": str(self.export_log),
            **overrides,
        }
        return subprocess.run(
            ["sh", str(self.launcher), "export-sessions", *arguments],
            env=environment,
            cwd=self.project,
            text=True,
            capture_output=True,
        )

    def test_exports_session_tree_once_even_when_it_has_a_cycle(self):
        output_dir = self.project / "exports"

        result = self.run_export("ses_root", str(output_dir))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Done: 2 session(s)", result.stdout)
        self.assertEqual(self.export_log.read_text().splitlines(), ["ses_root", "ses_child"])
        for session_id in ("ses_root", "ses_child"):
            export_file = output_dir / f"{session_id}.json"
            with export_file.open(encoding="utf-8") as exported:
                self.assertEqual(json.load(exported)["info"]["id"], session_id)
            self.assertEqual(export_file.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(output_dir.glob(".*")))

    def test_rejects_invalid_session_id_before_running_docker(self):
        result = self.run_export("invalid")

        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stderr)
        self.assertFalse(self.export_log.exists())

    def test_invalid_json_export_is_not_published(self):
        output_dir = self.project / "exports"

        result = self.run_export("ses_root", str(output_dir), INVALID_EXPORT="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Invalid export for ses_root", result.stderr)
        self.assertFalse((output_dir / "ses_root.json").exists())
        self.assertFalse(list(output_dir.glob(".*")))


if __name__ == "__main__":
    unittest.main()
