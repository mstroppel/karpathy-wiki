"""Private local snapshots; invoked by the launcher, never log subprocess secrets."""

import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


class BackupError(Exception):
    pass


def run(args, **kwargs):
    result = subprocess.run(args, capture_output=True, **kwargs)
    if result.returncode:
        raise BackupError(f"{args[0]} failed (exit {result.returncode}); private output withheld")
    return result.stdout


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.chmod(0o600)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    temporary.replace(path)
    sync_directory(path.parent)


def within(path, parent):
    return path == parent or parent in path.parents


def check_file_metadata(path):
    info = path.lstat()
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode)):
        raise BackupError("snapshot contains an unsupported socket/device/FIFO")
    if os.geteuid() != 0 and (
        info.st_uid != os.geteuid() or info.st_gid not in {*os.getgroups(), os.getgid()}
    ):
        raise BackupError("snapshot ownership requires running as a privileged operator")


class Instance:
    def __init__(self, project, compose, version):
        self.project = Path(project).resolve()
        self.compose = Path(compose).resolve()
        self.version = version
        self.override = self.project / "compose.override.yaml"
        for source in (self.compose, self.override):
            if source.exists() and re.search(r"\b(include|extends)[\"']?\s*:", source.read_text()):
                raise BackupError("Compose include/extends must be flattened before safe updates")
        # Configuration comes from the saved .env, not transient shell overrides.
        self.environment = {
            k: v
            for k, v in os.environ.items()
            if k
            in {
                "PATH",
                "HOME",
                "DOCKER_HOST",
                "DOCKER_CONTEXT",
                "DOCKER_CONFIG",
                "DOCKER_TLS_VERIFY",
                "DOCKER_CERT_PATH",
            }
            or k.startswith("STUB_")
            or k.endswith("_STUB_LOG")
        }
        self.environment["KARPATHY_WIKI_VERSION"] = version
        for key in ("COMPOSE_FILE", "COMPOSE_PROJECT_NAME", "COMPOSE_PROFILES"):
            self.environment.pop(key, None)
        self.base = [
            "docker",
            "compose",
            "--project-directory",
            str(self.project),
            "--env-file",
            str(self.project / ".env"),
            "-f",
            str(self.compose),
        ]
        if self.override.exists():
            self.base += ["-f", str(self.override)]
        self.config = json.loads(self.call("config", "--no-env-resolution", "--format", "json"))
        self.name = self.config["name"]
        values = {}
        for line in self.call("config", "--environment").decode().splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        values.update(
            {k: v for k, v in os.environ.items() if k.startswith("KARPATHY_WIKI_BACKUP_")}
        )
        self.values = values
        self.data = self.path(values.get("DATA_ROOT", "./data"))
        self.store = self.path(values.get("KARPATHY_WIKI_BACKUP_DIR", "./backups"))
        self.identity = hashlib.sha256(f"{self.project}\0{self.name}".encode()).hexdigest()

    def path(self, value):
        path = Path(value)
        path = path if path.is_absolute() else self.project / path
        resolved = path.resolve()
        if resolved != Path(os.path.abspath(path)):
            raise BackupError("configured paths and their parents must not be symlinks")
        return resolved

    def call(self, *args):
        return run(self.base + list(args), env=self.environment)

    def containers(self):
        return (
            run(
                ["docker", "ps", "-aq", "--filter", f"label=com.docker.compose.project={self.name}"]
            )
            .decode()
            .split()
        )

    def stop(self):
        # Includes inactive profiles and orphan services from previous releases.
        ids = self.containers()
        if ids:
            run(["docker", "stop", "--time", "60", *ids])
            states = json.loads(run(["docker", "inspect", *ids]))
            if any(item["State"]["Running"] for item in states):
                raise BackupError("not all instance writers stopped")

    def start(self):
        self.call(
            "up", "-d", "--remove-orphans", "--pull", "never", "--wait", "--wait-timeout", "600"
        )

    def paths(self):
        if b"GNU tar" not in run(["tar", "--version"]):
            raise BackupError("GNU tar with ACL/xattr support is required")
        if not self.data.is_dir() or self.data == self.project or within(self.project, self.data):
            raise BackupError("DATA_ROOT must be an existing dedicated data directory")
        if within(self.store, self.data) or within(self.data, self.store):
            raise BackupError("backup directory must be outside DATA_ROOT and not its ancestor")
        paths = {
            self.data,
            self.project / ".env",
            Path(__file__).resolve().with_name("karpathy-wiki.sh"),
            Path(__file__).resolve(),
            self.compose,
        }
        if self.override.exists():
            paths.add(self.override)
        for group in ("secrets", "configs"):
            for item in self.config.get(group, {}).values():
                if item.get("external"):
                    raise BackupError("external Docker secrets/configs are not supported")
                if "file" in item:
                    paths.add(self.path(item["file"]))
        for service in self.config["services"].values():
            if service.get("build"):
                raise BackupError("custom builds are not supported; use locally available images")
            for mount in service.get("volumes", []):
                if mount["type"] == "tmpfs":
                    continue
                if mount["type"] != "bind":
                    raise BackupError(
                        "named/anonymous volumes are not covered; use DATA_ROOT binds"
                    )
                source = self.path(mount["source"])
                if within(source, self.data):
                    continue
                if not mount.get("read_only", False):
                    raise BackupError("external writable mounts are not supported; use DATA_ROOT")
                paths.add(source)
            for item in service.get("env_file", []):
                paths.add(self.path(item["path"] if isinstance(item, dict) else item))
        try:
            extra = json.loads(self.values.get("KARPATHY_WIKI_BACKUP_EXTRA_PATHS", "[]"))
            if not isinstance(extra, list) or not all(isinstance(p, str) for p in extra):
                raise ValueError
            paths.update(self.path(p) for p in extra)
        except (ValueError, TypeError):
            raise BackupError(
                "KARPATHY_WIKI_BACKUP_EXTRA_PATHS must be a JSON array of paths"
            ) from None
        # Include file secrets even from disabled profiles.
        for key, value in self.values.items():
            if key.endswith("_FILE") and value and key != "COMPOSE_FILE":
                paths.add(self.path(value))
        for path in paths:
            if not path.exists():
                raise BackupError("a required snapshot path is missing")
            if path == Path("/") or within(self.store, path) or within(path, self.store):
                raise BackupError("snapshot path overlaps backup storage or filesystem root")
            if path.is_symlink():
                raise BackupError("snapshot roots must not be symlinks")
            if path.is_mount() or not os.access(path.parent, os.W_OK | os.X_OK):
                raise BackupError(
                    "snapshot roots require writable parents and cannot be mountpoints"
                )
            check_file_metadata(path)
            if path.is_file() and path.stat().st_nlink > 1:
                raise BackupError("hard-linked configuration roots must be independent copies")
            if path.is_dir():
                for root, directories, files in os.walk(path):
                    for name in directories + files:
                        entry = Path(root) / name
                        check_file_metadata(entry)
                        if entry.is_symlink() and not within(entry.resolve(), path):
                            raise BackupError("snapshot contains an external symlink")
        return sorted(p for p in paths if not any(p != q and within(p, q) for q in paths))

    def images(self, rescue=False):
        images = {}
        containers = {}
        ids = self.containers()
        if ids:
            for container in json.loads(run(["docker", "inspect", *ids])):
                service = container["Config"]["Labels"].get("com.docker.compose.service")
                containers[service] = container
        for name, service in self.config["services"].items():
            image = service.get("image")
            if not image:
                raise BackupError("every service must have an image")
            if "@" in image:
                raise BackupError(
                    "digest-only image references are not supported; use versioned tags"
                )
            if rescue and name in containers:
                images[image] = containers[name]["Image"]
            else:
                try:
                    info = json.loads(run(["docker", "image", "inspect", image]))
                    images[image] = info[0]["Id"]
                except BackupError:
                    if not rescue:
                        raise
                    # A failed pull/start can leave a declared service never installed.
                    # Rescue still saves all present software and complete current files.
        if not rescue:
            for service, container in containers.items():
                configured = self.config["services"].get(service, {}).get("image")
                if configured is None or images[configured] != container["Image"]:
                    raise BackupError(
                        "containers do not match pinned software; reconcile before update"
                    )
        else:
            for container in containers.values():
                reference = container["Config"].get("Image", container["Image"])
                images[reference] = container["Image"]
        return images


def secure_store(path):
    if path.is_symlink():
        raise BackupError("backup storage cannot be a symlink")
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.stat()
    if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
        raise BackupError("backup storage must be operator-owned with mode 0700")


@contextlib.contextmanager
def project_lock(project):
    inherited = os.environ.get("KARPATHY_WIKI_LOCK_FD")
    if inherited is not None:
        descriptor = int(inherited)
        info = os.fstat(descriptor)
        current = (project / ".launcher.lock").stat()
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            raise BackupError("invalid inherited maintenance lock")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield descriptor
        return
    with (project / ".launcher.lock").open("a") as stream:
        os.chmod(stream.name, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("another launcher operation is active") from None
        yield stream.fileno()


@contextlib.contextmanager
def locked(instance):
    with project_lock(instance.project):
        secure_store(instance.store)
        yield


def verify(directory, metadata):
    for filename in ("snapshot.tar", "images.tar"):
        if digest(directory / filename) != metadata["sha256"][filename]:
            raise BackupError("backup checksum verification failed")
        with tarfile.open(directory / filename) as archive:
            for member in archive:
                if member.isfile():
                    stream = archive.extractfile(member)
                    if stream is None:
                        raise BackupError("incomplete archive")
                    while stream.read(1024 * 1024):
                        pass


def snapshot(instance, kind):
    paths = instance.paths()
    images = instance.images(rescue=kind == "rescue")
    now = dt.datetime.now(dt.UTC)
    identifier = now.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(6)
    temporary = instance.store / (".partial-" + identifier)
    temporary.mkdir(mode=0o700)
    instance.stop()
    try:
        # Writers may have changed entries after the running-state preflight.
        paths = instance.paths()
        run(
            [
                "tar",
                "--create",
                "--file",
                str(temporary / "snapshot.tar"),
                "--acls",
                "--xattrs",
                "--numeric-owner",
                "--directory",
                "/",
                "--",
                *(str(p).lstrip("/") for p in paths),
            ]
        )
        run(
            [
                "docker",
                "image",
                "save",
                "--output",
                str(temporary / "images.tar"),
                *sorted(set(images.values())),
            ]
        )
        metadata = {
            "id": identifier,
            "created": now.isoformat(),
            "instance": instance.identity,
            "version": instance.version,
            "kind": kind,
            "status": "verified" if kind == "rescue" else "pending",
            "paths": [str(p) for p in paths],
            "data": str(instance.data),
            "compose": str(instance.compose),
            "images": images,
            "sha256": {name: digest(temporary / name) for name in ("snapshot.tar", "images.tar")},
        }
        verify(temporary, metadata)
        for entry in temporary.iterdir():
            entry.chmod(0o600)
            with entry.open("rb") as stream:
                os.fsync(stream.fileno())
        write_json(temporary / "metadata.json", metadata)
        temporary.rename(instance.store / identifier)
        sync_directory(instance.store)
        print(f"Backup {identifier} ({kind}, version {instance.version}) verified", flush=True)
        return metadata
    except BaseException:
        print(
            "Snapshot failed; data/configuration unchanged; stack remains stopped.", file=sys.stderr
        )
        raise


def backups(instance):
    result = []
    for directory in instance.store.iterdir():
        if directory.is_dir() and not directory.name.startswith("."):
            metadata = json.loads((directory / "metadata.json").read_text())
            if metadata["instance"] == instance.identity:
                result.append(metadata)
    return sorted(result, key=lambda m: (m["created"], m["id"]), reverse=True)


def prune(instance, now=None):
    now = now or dt.datetime.now(dt.UTC)
    updates = [m for m in backups(instance) if m["kind"] == "update"]
    for metadata in updates[3:]:
        if metadata["status"] == "success" and now - dt.datetime.fromisoformat(
            metadata["created"]
        ) >= dt.timedelta(days=30):
            shutil.rmtree(instance.store / metadata["id"])


def set_version(project, version, ensure_password=True):
    path = project / ".env"
    text = path.read_text()
    if re.search(r"^KARPATHY_WIKI_VERSION=", text, re.M):
        text = re.sub(
            r"^KARPATHY_WIKI_VERSION=.*$", f"KARPATHY_WIKI_VERSION={version}", text, flags=re.M
        )
    else:
        text = text.rstrip("\n") + f"\nKARPATHY_WIKI_VERSION={version}\n"
    if ensure_password and not re.search(r"^OPENCODE_PASSWORD=[^\n]+$", text, re.M):
        text = re.sub(r"^OPENCODE_PASSWORD=.*\n?", "", text, flags=re.M)
        text = text.rstrip("\n") + f"\nOPENCODE_PASSWORD={secrets.token_hex(24)}\n"
    temporary = project / ".env.update"
    temporary.write_text(text)
    temporary.chmod(0o600)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    temporary.replace(path)
    sync_directory(path.parent)


def update(instance, target, target_compose):
    metadata = snapshot(instance, "update")
    directory = instance.store / metadata["id"]
    updated = None
    phase = "configuration preflight"
    try:
        updated = Instance(instance.project, target_compose, target)
        updated.paths()
        if updated.name != instance.name or updated.data != instance.data:
            raise BackupError("update changed instance identity or DATA_ROOT")
        phase = "image pull"
        updated.call("pull")
        set_version(instance.project, target)
        phase = "startup/health verification"
        updated.start()
        metadata["status"] = "success"
        write_json(directory / "metadata.json", metadata)
        print(f"Update to {target} verified")
    except BaseException:
        try:
            (updated or instance).stop()
        except BackupError:
            print(
                "Could not stop all containers after update failure; stop writers manually.",
                file=sys.stderr,
            )
        metadata["status"] = "failed"
        write_json(directory / "metadata.json", metadata)
        print(
            f"Update failed during {phase}. Backup {metadata['id']} retained. Explicit recovery: "
            f"./karpathy-wiki.sh rollback {metadata['id']}",
            file=sys.stderr,
        )
        raise
    try:
        prune(instance)
    except (BackupError, OSError, ValueError, KeyError):
        print(
            "Update succeeded, but backup cleanup failed; inspect private storage.", file=sys.stderr
        )


def restore(instance, metadata):
    directory = instance.store / metadata["id"]
    stages = []
    try:
        destinations = []
        for value in metadata["paths"]:
            destination = Path(value)
            if (
                not destination.is_absolute()
                or destination == Path("/")
                or within(instance.store, destination)
                or within(destination, instance.store)
            ):
                raise BackupError("unsafe restore destination")
            destinations.append(instance.path(value))
        with tarfile.open(directory / "snapshot.tar") as archive:
            names = {m.name.rstrip("/") for m in archive}
        for destination in destinations:
            destination.parent.mkdir(parents=True, exist_ok=True)
            stage = Path(tempfile.mkdtemp(prefix=".kw-restore-", dir=destination.parent))
            stages.append((destination, stage))
            member = str(destination).lstrip("/")
            if member not in names:
                raise BackupError("backup is missing a required root")
            run(
                [
                    "tar",
                    "--extract",
                    "--file",
                    str(directory / "snapshot.tar"),
                    "--acls",
                    "--xattrs",
                    "--xattrs-include=*",
                    "--same-owner",
                    "--same-permissions",
                    "--directory",
                    str(stage),
                    "--",
                    member,
                ]
            )
        run(["docker", "image", "load", "--input", str(directory / "images.tar")])
        for image, image_id in metadata["images"].items():
            if image != image_id and "@" not in image:
                run(["docker", "image", "tag", image_id, image])
        instance.call("--profile", "*", "down", "--remove-orphans")
        for destination, stage in stages:
            instance.path(str(destination))
            extracted = stage / str(destination).lstrip("/")
            if destination.exists() or destination.is_symlink():
                destination.rename(stage / "displaced")
            extracted.rename(destination)
        if str(instance.override) not in metadata["paths"] and instance.override.exists():
            instance.override.unlink()
        # A formerly floating pin must not resolve to today's release on restart.
        set_version(instance.project, metadata["version"], ensure_password=False)
        restored = Instance(instance.project, metadata["compose"], metadata["version"])
        restored.start()
    finally:
        for _, stage in stages:
            shutil.rmtree(stage)


def rollback(instance, identifier):
    if not re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{12}", identifier):
        raise BackupError("invalid backup ID")
    directory = instance.store / identifier
    metadata = json.loads((directory / "metadata.json").read_text())
    if metadata["instance"] != instance.identity or metadata["id"] != identifier:
        raise BackupError("backup belongs to a different instance")
    verify(directory, metadata)
    print(
        f"Backup date: {metadata['created']}\nCurrent version: {instance.version}\n"
        f"Target version: {metadata['version']}\n"
        "WARNING: All changes since this backup will be lost in the restored state.\n"
        f"Type 'rollback {identifier}' to confirm:",
        flush=True,
    )
    if input().strip() != f"rollback {identifier}":
        raise BackupError("rollback not confirmed; current state unchanged")
    rescue = snapshot(instance, "rescue")
    try:
        restore(instance, metadata)
        print(f"Rollback to {metadata['version']} verified; rescue backup {rescue['id']} retained")
    except BaseException:
        print(
            f"Restore failed; stack may be stopped or partially restored. Target {identifier} "
            f"and rescue {rescue['id']} retained. Do not resume writers before recovery.",
            file=sys.stderr,
        )
        raise


def main():
    if sys.version_info < (3, 11):  # noqa: UP036 -- standalone launcher supports Python 3.11+
        print("karpathy-wiki: Python 3.11+ is required", file=sys.stderr)
        return 1
    os.umask(0o077)
    try:
        if sys.argv[1] in {"launch", "forward"}:
            action = sys.argv[1]
            _, project, *command = sys.argv[1:]
            with project_lock(Path(project).resolve()) as descriptor:
                environment = dict(os.environ)
                if action == "launch":
                    environment["KARPATHY_WIKI_LOCK_FD"] = str(descriptor)
                return subprocess.run(command, env=environment, pass_fds=(descriptor,)).returncode
        action, project, compose, version, *args = sys.argv[1:]
        with project_lock(Path(project).resolve()):
            instance = Instance(project, compose, version)
            secure_store(instance.store)
            if action == "update":
                update(instance, *args)
            elif action == "rollback":
                rollback(instance, *args)
            elif action == "list":
                print("ID  DATE  VERSION  KIND  STATUS")
                for metadata in backups(instance):
                    print(
                        "  ".join(
                            metadata[k] for k in ("id", "created", "version", "kind", "status")
                        )
                    )
            else:
                raise BackupError("unknown backup action")
    except BackupError as error:
        print(f"karpathy-wiki: {error}", file=sys.stderr)
        return 1
    except OSError as error:
        reason = os.strerror(error.errno) if error.errno is not None else "I/O error"
        print(
            f"karpathy-wiki: filesystem/process failure: {reason}; no automatic rollback.",
            file=sys.stderr,
        )
        return 1
    except (ValueError, TypeError, KeyError, EOFError, tarfile.TarError):
        print(
            "karpathy-wiki: backup/update/restore failed; no automatic rollback. "
            "Check prerequisites, private storage and Docker state.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
