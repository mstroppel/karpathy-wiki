import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "runtime_write_isolation", ROOT / "opencode/runtime_write_isolation.py"
)
assert SPEC and SPEC.loader
isolation = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = isolation
SPEC.loader.exec_module(isolation)

STATUS = "NoNewPrivs:\t1\n" + "\n".join(
    f"{field}:\t0000000000000000" for field in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
)


def mount(target, root="/", mode="ro", device="8:1", identifier=1):
    return f"{identifier} 0 {device} {root} {target} {mode} - ext4 /dev/synthetic rw"


def topology():
    lines = [mount("/", device="0:1")]
    lines += [
        mount(path, f"/data/{index}", identifier=index + 2)
        for index, path in enumerate(isolation.PROTECTED_PATHS)
    ]
    lines += [
        mount("/tmp", mode="rw", device="0:2"),
        mount("/home/opencode/.config/opencode", root="/data/config", mode="rw"),
    ]
    return "\n".join(lines)


class RuntimeWriteIsolationTests(unittest.TestCase):
    def test_accepts_read_only_topology_with_private_writable_state(self):
        isolation.validate_isolation(topology(), STATUS, 1000)

    def test_rejects_root_capabilities_and_privilege_escalation(self):
        cases = [(STATUS, 0), (STATUS.replace("NoNewPrivs:\t1", "NoNewPrivs:\t0"), 1000)]
        cases += [
            (STATUS.replace(f"{field}:\t0000000000000000", f"{field}:\t0000000000000001"), 1000)
            for field in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
        ]
        for status, uid in cases:
            with self.subTest(status=status, uid=uid), self.assertRaises(ValueError):
                isolation.validate_isolation(topology(), status, uid)

    def test_rejects_writable_root_or_missing_or_writable_protected_mount(self):
        for path in ("/", *isolation.PROTECTED_PATHS):
            for operation in ("missing", "writable"):
                lines = topology().splitlines()
                modified = [line for line in lines if line.split()[4] != path]
                if operation == "writable":
                    modified += [mount(path, mode="rw")]
                with self.subTest(path=path, operation=operation), self.assertRaises(ValueError):
                    isolation.validate_isolation("\n".join(modified), STATUS, 1000)

    def test_rejects_overlapping_targets_and_backing_aliases(self):
        aliases = [
            mount("/alias", root="/data/0", mode="rw"),
            mount("/alias", root="/data", mode="rw"),
            mount("/alias", root="/data/0/subdir", mode="rw"),
            mount("/knowledge/wiki/nested", mode="rw", device="0:9"),
            mount("/knowledge", mode="rw", device="0:9"),
        ]
        for alias in aliases:
            with self.subTest(alias=alias), self.assertRaises(ValueError):
                isolation.validate_isolation(topology() + "\n" + alias, STATUS, 1000)

    def test_read_only_alias_is_safe_and_escaped_mount_paths_are_decoded(self):
        isolation.validate_isolation(
            topology() + "\n" + mount("/alias", root="/data/0"), STATUS, 1000
        )
        parsed = isolation.parse_mounts(mount(r"/path\040with\040spaces", root=r"/root\134name"))
        self.assertEqual(str(parsed[0].target), "/path with spaces")
        self.assertEqual(str(parsed[0].root), "/root\\name")

    def test_mode_defaults_to_manual_and_invalid_mode_fails(self):
        script = str(ROOT / "opencode/runtime_write_isolation.py")
        for mode, expected in (("false", 0), ("invalid", 1), ("true", 1)):
            result = subprocess.run(
                [sys.executable, script], env={"WIKI_RUNTIME_READ_ONLY": mode}, capture_output=True
            )
            self.assertEqual(result.returncode, expected)


if __name__ == "__main__":
    unittest.main()
