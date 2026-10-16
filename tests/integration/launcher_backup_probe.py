"""Exercise real Compose stop/save/load/health and delayed snapshot replacement."""

import json
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]


def run(*args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs)


def main():
    image = os.environ.get("LAUNCHER_TEST_IMAGE", "python:3.12-alpine")
    new_image = os.environ.get("LAUNCHER_TEST_NEW_IMAGE", "python:3.13-alpine")
    run("docker", "pull", image)
    with tempfile.TemporaryDirectory(prefix="kw-backup-probe-") as temporary:
        project = Path(temporary)
        name = f"kw-backup-{os.getpid()}-{int(time.time())}"
        cache = project / ".cache"
        cache.mkdir()
        data = project / "data"
        data.mkdir()
        (data / ".session").write_text("synthetic-old-session")
        (data / ".session").chmod(0o640)
        links = {"opencode/config/AGENTS.md": "/etc/opencode/routing.md"}
        for plugin in (ROOT / "config" / "plugins").glob("*.js"):
            links[f"opencode/config/plugins/{plugin.name}"] = f"/etc/opencode/plugins/{plugin.name}"
        for relative, target in links.items():
            path = data / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target)
        (project / "secret").write_text("synthetic-old-secret")
        (project / ".env").write_text(
            f"COMPOSE_PROJECT_NAME={name}\nKARPATHY_WIKI_VERSION=1.0.0\n"
            "OPENCODE_PASSWORD=synthetic-test-only\nDATA_ROOT=./data\nTOKEN_FILE=./secret\n"
        )
        for file in ("karpathy-wiki.sh", "launcher_backup.py"):
            shutil.copy(ROOT / file, project / file)
        run("sh", str(project / "karpathy-wiki.sh"), "launcher-accept", "1.0.0")
        # Serve synthetic release pairs locally; retain real Docker/Compose behavior.
        binary = project / "bin"
        binary.mkdir()
        curl = binary / "curl"
        curl.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\nfrom pathlib import Path\n"
            f"root = Path({str(ROOT)!r})\n"
            "url = sys.argv[sys.argv.index('-fsSL') + 1]\n"
            "name = url.rsplit('/', 1)[1]\n"
            "assert name in ('karpathy-wiki.sh', 'launcher_backup.py')\n"
            "content = (root / name).read_text() + '\\n# synthetic release pair\\n'\n"
            "Path(sys.argv[sys.argv.index('-o') + 1]).write_text(content)\n"
        )
        curl.chmod(0o755)
        os.environ["PATH"] = str(binary) + os.pathsep + os.environ["PATH"]
        config: dict[str, Any] = {
            "name": name,
            "services": {
                "writer": {
                    "image": image,
                    "user": f"{os.getuid()}:{os.getgid()}",
                    "command": ["python", "-c", "import time; time.sleep(3600)"],
                    "volumes": [{"type": "bind", "source": str(data), "target": "/data"}],
                    "healthcheck": {"test": ["CMD", "python", "-c", "pass"], "interval": "1s"},
                    "network_mode": "none",
                }
            },
        }
        for version in ("1.0.0", "2.0.0", "3.0.0"):
            config["services"]["writer"]["image"] = image if version == "1.0.0" else new_image
            (cache / f"compose-v{version}.yaml").write_text(json.dumps(config))
        launcher = project / "karpathy-wiki.sh"
        try:
            run("sh", str(launcher), "up", "-d", "--wait", "--wait-timeout", "30")
            # Prove that the live writer and the archived data use the same bind.
            run(
                "sh",
                str(launcher),
                "exec",
                "-T",
                "writer",
                "python",
                "-c",
                "assert open('/data/.session').read() == 'synthetic-old-session'",
            )
            run("sh", str(launcher), "update", "2.0.0")
            metadata = [
                json.loads(p.read_text()) for p in (project / "backups").glob("*/metadata.json")
            ]
            identifier = metadata[0]["id"]
            (data / ".session").write_text("synthetic-new-session")
            (data / "new-only").write_text("must disappear")
            (project / "secret").write_text("synthetic-new-secret")
            run("sh", str(launcher), "update", "3.0.0")
            restored = run(
                "sh", str(launcher), "rollback", identifier, input=f"rollback {identifier}\n"
            )
            assert "verified" in restored.stdout
            assert (data / ".session").read_text() == "synthetic-old-session"
            assert (data / ".session").stat().st_mode & 0o777 == 0o640
            assert not (data / "new-only").exists()
            assert (project / "secret").read_text() == "synthetic-old-secret"
            for relative, target in links.items():
                assert os.readlink(data / relative) == target
            assert "KARPATHY_WIKI_VERSION=1.0.0" in (project / ".env").read_text()
            for file in ("karpathy-wiki.sh", "launcher_backup.py"):
                assert (project / file).read_bytes() == (ROOT / file).read_bytes()
            assert json.loads((project / ".launcher-pair.json").read_text())["version"] == "1.0.0"
            container = run("sh", str(launcher), "ps", "-q", "writer").stdout.strip()
            actual_image = run(
                "docker", "inspect", container, "--format", "{{.Image}}"
            ).stdout.strip()
            old_image = run(
                "docker", "image", "inspect", image, "--format", "{{.Id}}"
            ).stdout.strip()
            assert actual_image == old_image
            run(
                "sh",
                str(launcher),
                "exec",
                "-T",
                "writer",
                "python",
                "-c",
                "assert open('/data/.session').read() == 'synthetic-old-session'",
            )
            metadata = [
                json.loads(p.read_text()) for p in (project / "backups").glob("*/metadata.json")
            ]
            assert sum(m["kind"] == "rescue" for m in metadata) == 1
            config["services"]["writer"]["image"] = image
            config["services"]["writer"]["command"] = ["python", "-c", "raise SystemExit(2)"]
            (cache / "compose-v4.0.0.yaml").write_text(json.dumps(config))
            failed = subprocess.run(
                ["sh", str(launcher), "update", "4.0.0"], capture_output=True, text=True
            )
            assert failed.returncode != 0 and "rollback" in failed.stderr
            metadata = [
                json.loads(p.read_text()) for p in (project / "backups").glob("*/metadata.json")
            ]
            failed_id = next(m["id"] for m in metadata if m["status"] == "failed")
            run("sh", str(launcher), "rollback", failed_id, input=f"rollback {failed_id}\n")
            assert "KARPATHY_WIKI_VERSION=1.0.0" in (project / ".env").read_text()
            before = {p.name for p in (project / "backups").iterdir()}
            run("sh", str(launcher), "update", "2.0.0", "--no-backup")
            assert {p.name for p in (project / "backups").iterdir()} == before
            assert "KARPATHY_WIKI_VERSION=2.0.0" in (project / ".env").read_text()
            assert (data / ".session").read_text() == "synthetic-old-session"
            failed = subprocess.run(
                ["sh", str(launcher), "update", "--no-backup", "4.0.0"],
                capture_output=True,
                text=True,
            )
            assert failed.returncode != 0 and "No update backup was created" in failed.stderr
            assert "./karpathy-wiki.sh rollback" not in failed.stderr
            assert {p.name for p in (project / "backups").iterdir()} == before
            print("launcher-backup: container links, delayed rollback and no-backup verified")
        finally:
            run(
                "docker",
                "compose",
                "--project-directory",
                str(project),
                "-f",
                str(cache / "compose-v1.0.0.yaml"),
                "down",
                "--remove-orphans",
            )


if __name__ == "__main__":
    main()
