"""Fail closed unless the kernel-enforced reader topology is intact."""

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

PROTECTED_PATHS = (
    "/knowledge/wiki",
    "/knowledge/sources",
    "/knowledge/incoming/ingest-journal",
    "/knowledge/incoming/answers",
)


@dataclass(frozen=True)
class Mount:
    device: str
    root: PurePosixPath
    target: PurePosixPath
    read_only: bool


def mount_path(value: str) -> PurePosixPath:
    return PurePosixPath(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value))


def parse_mounts(mountinfo: str) -> list[Mount]:
    mounts = []
    for line in mountinfo.splitlines():
        fields = line.split()
        if len(fields) < 10 or "-" not in fields[6:]:
            raise ValueError("invalid_mountinfo")
        mounts.append(
            Mount(
                fields[2],
                mount_path(fields[3]),
                mount_path(fields[4]),
                "ro" in fields[5].split(","),
            )
        )
    return mounts


def overlaps(first: PurePosixPath, second: PurePosixPath) -> bool:
    return first.is_relative_to(second) or second.is_relative_to(first)


def validate_isolation(mountinfo: str, process_status: str, uid: int) -> None:
    if uid == 0:
        raise ValueError("root_runtime")
    status = dict(line.split(":", 1) for line in process_status.splitlines() if ":" in line)
    if status.get("NoNewPrivs", "").strip() != "1":
        raise ValueError("privilege_escalation_allowed")
    for field in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"):
        if field not in status or int(status[field].strip(), 16) != 0:
            raise ValueError("runtime_capabilities")

    mounts = parse_mounts(mountinfo)
    roots = [mount for mount in mounts if str(mount.target) == "/"]
    if len(roots) != 1 or not roots[0].read_only:
        raise ValueError("writable_container_root")
    for path in PROTECTED_PATHS:
        target = PurePosixPath(path)
        matches = [mount for mount in mounts if mount.target == target]
        if len(matches) != 1 or not matches[0].read_only:
            raise ValueError("protected_mount_not_read_only")
        protected = matches[0]
        for mount in mounts:
            if mount.read_only:
                continue
            if overlaps(mount.target, target):
                raise ValueError("writable_protected_subtree")
            if mount.device == protected.device and overlaps(mount.root, protected.root):
                raise ValueError("writable_backing_alias")


def main() -> int:
    mode = os.environ.get("WIKI_RUNTIME_READ_ONLY", "false")
    if mode == "false":
        return 0
    if mode != "true":
        print("runtime-write-isolation: invalid_mode", file=sys.stderr)
        return 1
    try:
        validate_isolation(
            Path("/proc/self/mountinfo").read_text(),
            Path("/proc/self/status").read_text(),
            os.getuid(),
        )
        for path in PROTECTED_PATHS:
            directory = Path(path)
            if directory.is_symlink() or not directory.is_dir():
                raise ValueError("invalid_protected_directory")
            if not os.statvfs(path).f_flag & os.ST_RDONLY:
                raise ValueError("protected_filesystem_not_read_only")
        if Path("/var/run/docker.sock").exists():
            raise ValueError("docker_socket_exposed")
    except (OSError, ValueError):
        # Never log mount sources, data paths, configuration or credentials.
        print("runtime-write-isolation: unsafe topology; refusing startup", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
