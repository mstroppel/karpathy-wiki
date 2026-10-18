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
import time
from pathlib import Path

LAUNCHER_PROTOCOL = 1
PAIR = ("karpathy-wiki.sh", "launcher_backup.py")
LAUNCHER_DIR = Path(__file__).resolve().parent
# Exact links installed by opencode/entrypoint.sh; archive links, never host targets.
CONTAINER_LINKS = {
    "opencode/config/AGENTS.md": "/etc/opencode/routing.md",
    **{
        f"opencode/config/plugins/{name}.js": f"/etc/opencode/plugins/{name}.js"
        for name in ("wiki-ingest-journal", "wiki-ingest-status", "wiki-ingest-transaction")
    },
}


class BackupError(Exception):
    pass


VERBOSE = False


@contextlib.contextmanager
def phase(name):
    """Only caller-owned labels, never command arguments or captured output."""
    print(f"karpathy-wiki: {name}", file=sys.stderr, flush=True)
    started = time.monotonic()
    try:
        yield
    except BaseException:
        print(f"karpathy-wiki: failed during {name}", file=sys.stderr, flush=True)
        raise
    finally:
        if VERBOSE:
            print(
                f"karpathy-wiki: {name}: elapsed {time.monotonic() - started:.2f}s",
                file=sys.stderr,
                flush=True,
            )


def operation(args):
    # Do not render arbitrary executable names, paths, flags or values.
    if args[:2] == ["docker", "compose"]:
        for command in ("config", "pull", "up", "down"):
            if command in args:
                return f"docker compose {command}"
        return "docker compose"
    for prefix in (
        ["docker", "image", "inspect"],
        ["docker", "image", "save"],
        ["docker", "image", "load"],
        ["docker", "image", "tag"],
        ["docker", "inspect"],
        ["docker", "ps"],
        ["docker", "stop"],
        ["tar"],
        ["curl"],
        ["sh", "-n"],
    ):
        if args[: len(prefix)] == prefix:
            return " ".join(prefix)
    return "launcher runtime probe"


def run(args, **kwargs):
    label = operation(args)
    started = time.monotonic()
    try:
        result = subprocess.run(args, capture_output=True, **kwargs)
        if result.returncode:
            raise BackupError(f"{label} failed (exit {result.returncode}); private output withheld")
        return result.stdout
    finally:
        if VERBOSE:
            print(
                f"karpathy-wiki: operation {label}: elapsed {time.monotonic() - started:.2f}s; "
                "private arguments/output withheld",
                file=sys.stderr,
                flush=True,
            )


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


def pair_hashes(directory):
    for name in PAIR:
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_nlink != 1:
            raise BackupError("launcher files must be independent regular files")
    return {name: digest(directory / name) for name in PAIR}


def accepted_pair(directory):
    record = directory / ".launcher-pair.json"
    if not record.is_file():
        raise BackupError(
            "unregistered launcher pair; inspect files, then run launcher-accept <version>"
        )
    value = json.loads(record.read_text())
    if value["sha256"] != pair_hashes(directory):
        raise BackupError(
            "locally modified launcher pair; preserve edits and explicitly "
            "launcher-accept <version> before updating"
        )
    return value


def validate_pair(directory):
    pair_hashes(directory)
    if "# launcher-protocol: 1" not in (directory / PAIR[0]).read_text():
        raise BackupError("target launcher does not support safe pair activation protocol 1")
    run(["sh", "-n", str(directory / PAIR[0])])
    # Downloads are trusted executable release code. Probe the actual target runtime
    # before stopping writers or changing configuration, not just Python syntax.
    if (
        run([sys.executable, str(directory / PAIR[1]), "check-runtime"]).strip()
        != b"launcher-protocol: 1"
    ):
        raise BackupError("target helper does not support launcher protocol 1")


def reset_pair_stage(instance, destinations=(), *, inspect_contents=True):
    stage = instance.path(str(LAUNCHER_DIR / ".launcher-stage"))
    for path in (
        *instance.paths(inspect_contents=inspect_contents),
        instance.data,
        instance.store,
        *destinations,
    ):
        if within(path, stage) or within(stage, path):
            raise BackupError("launcher staging overlaps data, backup storage or a snapshot root")
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(mode=0o700)
    return stage


def stage_pair(instance, target, *, backup=True):
    if not re.fullmatch(r"[0-9][0-9A-Za-z.-]*", target):
        raise BackupError("self-update requires a release version, not a PR preview")
    accepted_pair(LAUNCHER_DIR)
    stage = reset_pair_stage(instance, inspect_contents=backup)
    for name in PAIR:
        run(
            [
                "curl",
                "-fsSL",
                f"https://raw.githubusercontent.com/mstroppel/karpathy-wiki/v{target}/{name}",
                "-o",
                str(stage / name),
            ]
        )
    validate_pair(stage)
    return stage


def activate_pair(directory, stage, record, project):
    """A durable guard, not two allegedly atomic renames. Caller holds project lock."""
    if pair_hashes(stage) != record["sha256"]:
        raise BackupError("staged launcher checksum mismatch; retain backup and recover manually")
    marker = directory / ".launcher-activation.json"
    write_json(marker, {"pair": record, "project": str(project)})
    for name in PAIR:
        temporary = directory / (name + ".activate")
        shutil.copyfile(stage / name, temporary)
        temporary.chmod(0o755 if name.endswith(".sh") else 0o644)
        with temporary.open("rb") as stream:
            os.fsync(stream.fileno())
        temporary.replace(directory / name)
        sync_directory(directory)
    write_json(directory / ".launcher-pair.json", record)
    marker.unlink()
    sync_directory(directory)


def prepare_activation(stage, version):
    # Retain a complete, durable recovery pair until the next update.
    for name in PAIR:
        with (stage / name).open("rb") as stream:
            os.fsync(stream.fileno())
    sync_directory(stage)
    sync_directory(stage.parent)
    return {"version": version, "sha256": pair_hashes(stage)}


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
        with phase("startup and health verification (Compose up --wait)"):
            self.call(
                "up", "-d", "--remove-orphans", "--pull", "never", "--wait", "--wait-timeout", "600"
            )

    def paths(self, *, inspect_contents=True):
        if inspect_contents and b"GNU tar" not in run(["tar", "--version"]):
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
            LAUNCHER_DIR / ".launcher-pair.json",
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
            if path.is_dir() and within(LAUNCHER_DIR, path):
                raise BackupError(
                    "snapshot directory roots must not contain the launcher installation"
                )
            if path.is_mount() or not os.access(path.parent, os.W_OK | os.X_OK):
                raise BackupError(
                    "snapshot roots require writable parents and cannot be mountpoints"
                )
            if not inspect_contents:
                continue
            check_file_metadata(path)
            if path.is_file() and path.stat().st_nlink > 1:
                raise BackupError("hard-linked configuration roots must be independent copies")
            if path.is_dir():
                for root, directories, files in os.walk(path):
                    for name in directories + files:
                        entry = Path(root) / name
                        check_file_metadata(entry)
                        if entry.is_symlink():
                            relative = (
                                str(entry.relative_to(self.data))
                                if within(entry, self.data)
                                else ""
                            )
                            if (
                                relative in CONTAINER_LINKS
                                and os.readlink(entry) == CONTAINER_LINKS[relative]
                            ):
                                continue
                            if not within(entry.resolve(), path):
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
        profiles = {
            profile.strip() for profile in self.values.get("COMPOSE_PROFILES", "").split(",")
        } - {""}
        required = {
            name
            for name, service in self.config["services"].items()
            if not service.get("profiles")
            or "*" in profiles
            or profiles.intersection(service["profiles"])
            or name in containers
        }
        # Include dependencies even if Compose configuration came from an override.
        pending = list(required)
        while pending:
            dependencies = self.config["services"][pending.pop()].get("depends_on", {})
            for dependency, settings in dependencies.items():
                # Compose retains optional edges even when their disabled service
                # is absent from the resolved model. Independently enabled services
                # are already required; an optional edge must not enable a profile.
                if not settings.get("required", True):
                    continue
                if dependency not in self.config["services"]:
                    raise BackupError(
                        "required Compose dependency is absent; validate service profiles"
                    )
                if dependency not in required:
                    required.add(dependency)
                    pending.append(dependency)
        for name, service in self.config["services"].items():
            if not rescue and name not in required:
                continue
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
                    try:
                        info = json.loads(run(["docker", "image", "inspect", image]))
                    except BackupError:
                        container = containers.get(name)
                        if container is None or container["Config"].get("Image") != image:
                            raise
                        # A removed tag need not lose the installed software. Save
                        # its immutable ID; restore() recreates the configured tag.
                        # Never substitute a container created from another reference.
                        installed_id = container["Image"]
                        info = json.loads(run(["docker", "image", "inspect", installed_id]))
                        if info[0]["Id"] != installed_id:
                            raise BackupError(
                                "installed container image ID does not match"
                            ) from None
                    images[image] = info[0]["Id"]
                except BackupError:
                    if not rescue:
                        reference = (
                            image
                            if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}", image)
                            else "[unsafe image reference withheld]"
                        )
                        raise BackupError(
                            "backup image checks: cannot inspect required local image "
                            f"{reference}. "
                            "Check Docker access and restore the exact originally installed image "
                            "from a trusted image archive or verified immutable digest; reconcile "
                            "its tag with installed container image IDs, then retry. Do not pull "
                            "a mutable tag as a repair or bypass the backup. "
                            "Private output withheld."
                        ) from None
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
    with phase("backup preflight and image checks"):
        instance.paths()
        images = instance.images(rescue=kind == "rescue")
    now = dt.datetime.now(dt.UTC)
    identifier = now.strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(6)
    temporary = instance.store / (".partial-" + identifier)
    temporary.mkdir(mode=0o700)
    with phase("stopping writers"):
        instance.stop()
    try:
        with phase("backup creation and verification"):
            return archive_snapshot(instance, kind, images, now, identifier, temporary)
    except BaseException:
        print(
            "Snapshot failed; data/configuration unchanged; stack remains stopped.", file=sys.stderr
        )
        raise


def archive_snapshot(instance, kind, images, now, identifier, temporary):
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
        "launcher": accepted_pair(LAUNCHER_DIR),
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


def update(instance, target, target_compose, *, backup=True):
    if instance.project != LAUNCHER_DIR:
        raise BackupError("safe updates require the launcher pair in the project directory")
    if not backup:
        print(
            "WARNING: --no-backup: no update snapshot will be created. "
            "This update has no new rollback point; recovery requires an existing "
            "backup or manual recovery. No automatic rollback.",
            file=sys.stderr,
            flush=True,
        )
    # All non-mutating checks precede staging and stopping. Failure here must not
    # enter the post-stop cleanup path, which deliberately stops all writers.
    with phase("configuration preflight"):
        accepted_pair(LAUNCHER_DIR)
        instance.paths(inspect_contents=backup)
        updated = Instance(instance.project, target_compose, target)
        updated.paths(inspect_contents=backup)
        if updated.name != instance.name or updated.data != instance.data:
            raise BackupError("update changed instance identity or DATA_ROOT")
    if backup:
        with phase("backup image checks"):
            instance.images()
        with phase("backup storage preflight"):
            secure_store(instance.store)
    with phase("launcher staging"):
        stage = stage_pair(instance, target, backup=backup)
        record = prepare_activation(stage, target)
    metadata = snapshot(instance, "update") if backup else None
    try:
        if not backup:
            with phase("stopping writers"):
                instance.stop()
        with phase("image pull"):
            updated.call("pull")
        with phase("launcher activation (use staged helper recover if interrupted)"):
            activate_pair(LAUNCHER_DIR, stage, record, instance.project)
            set_version(instance.project, target)
        updated.start()
        if metadata is not None:
            metadata["status"] = "success"
            write_json(instance.store / metadata["id"] / "metadata.json", metadata)
        print(f"Update to {target} verified")
    except BaseException:
        try:
            updated.stop()
        except BackupError:
            print(
                "Could not stop all containers after update failure; stop writers manually.",
                file=sys.stderr,
            )
        if metadata is not None:
            metadata["status"] = "failed"
            write_json(instance.store / metadata["id"] / "metadata.json", metadata)
            recovery = (
                f"Backup {metadata['id']} retained. Explicit recovery: "
                f"./karpathy-wiki.sh rollback {metadata['id']}"
            )
        else:
            recovery = (
                "No update backup was created (--no-backup). Stack may be stopped; "
                "inspect configuration, data and launcher state before manual recovery "
                "or selecting an existing backup."
            )
        print(f"Update failed. {recovery}", file=sys.stderr)
        raise
    try:
        if backup:
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
        required = {LAUNCHER_DIR / name for name in (*PAIR, ".launcher-pair.json")}
        saved_pair = metadata.get("launcher")
        if not required.issubset(destinations) or not isinstance(saved_pair, dict):
            raise BackupError("unsupported snapshot: complete launcher pair metadata is required")
        if not isinstance(saved_pair.get("version"), str) or not isinstance(
            saved_pair.get("sha256"), dict
        ):
            raise BackupError("unsupported snapshot: invalid launcher pair metadata")
        pair_stage = reset_pair_stage(instance, destinations)
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
        for name in PAIR:
            destination = LAUNCHER_DIR / name
            source = next(
                stage / str(destination).lstrip("/")
                for dest, stage in stages
                if dest == destination
            )
            shutil.copyfile(source, pair_stage / name)
        validate_pair(pair_stage)
        record = prepare_activation(pair_stage, saved_pair["version"])
        if record != saved_pair:
            raise BackupError("saved launcher pair does not match snapshot metadata")
        run(["docker", "image", "load", "--input", str(directory / "images.tar")])
        for image, image_id in metadata["images"].items():
            if image != image_id and "@" not in image:
                run(["docker", "image", "tag", image_id, image])
        instance.call("--profile", "*", "down", "--remove-orphans")
        for destination, stage in stages:
            if destination in {LAUNCHER_DIR / name for name in (*PAIR, ".launcher-pair.json")}:
                continue
            instance.path(str(destination))
            extracted = stage / str(destination).lstrip("/")
            if destination.exists() or destination.is_symlink():
                destination.rename(stage / "displaced")
            extracted.rename(destination)
        activate_pair(LAUNCHER_DIR, pair_stage, record, instance.project)
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
    if instance.project != LAUNCHER_DIR:
        raise BackupError("safe rollback requires the launcher pair in the project directory")
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
    global VERBOSE
    if sys.version_info < (3, 11):  # noqa: UP036 -- standalone launcher supports Python 3.11+
        print("karpathy-wiki: Python 3.11+ is required", file=sys.stderr)
        return 1
    os.umask(0o077)
    try:
        if sys.argv[1] == "check-runtime":
            print(f"launcher-protocol: {LAUNCHER_PROTOCOL}")
            return 0
        if sys.argv[1] in {"accept", "recover"}:
            action, project, *args = sys.argv[1:]
            with project_lock(Path(project).resolve()):
                if action == "accept":
                    if (LAUNCHER_DIR / ".launcher-activation.json").exists():
                        raise BackupError("recover interrupted activation before accepting files")
                    validate_pair(LAUNCHER_DIR)
                    write_json(
                        LAUNCHER_DIR / ".launcher-pair.json",
                        {"version": args[0], "sha256": pair_hashes(LAUNCHER_DIR)},
                    )
                    print("Installed launcher pair explicitly accepted")
                else:
                    directory = LAUNCHER_DIR.parent
                    journal = json.loads((directory / ".launcher-activation.json").read_text())
                    if journal["project"] != str(Path(project).resolve()):
                        raise BackupError("activation belongs to a different project")
                    activate_pair(directory, LAUNCHER_DIR, journal["pair"], Path(project).resolve())
                    print(
                        "Launcher pair recovered only; inspect stopped stack, then retry update "
                        "or explicitly rollback. No data restored."
                    )
            return 0
        if (LAUNCHER_DIR / ".launcher-activation.json").exists():
            raise BackupError(
                "interrupted launcher activation; use "
                ".launcher-stage/launcher_backup.py recover <project>"
            )
        if sys.argv[1] in {"launch", "forward"}:
            action = sys.argv[1]
            _, project, *command = sys.argv[1:]
            with project_lock(Path(project).resolve()) as descriptor:
                environment = dict(os.environ)
                if action == "launch":
                    environment["KARPATHY_WIKI_LOCK_FD"] = str(descriptor)
                return subprocess.run(command, env=environment, pass_fds=(descriptor,)).returncode
        action, project, compose, version, *args = sys.argv[1:]
        if action == "update":
            VERBOSE = "--verbose" in args or "-v" in args
            no_backup = "--no-backup" in args
            args = [arg for arg in args if arg not in {"--verbose", "-v", "--no-backup"}]
            if len(args) != 2 or any(arg.startswith("-") for arg in args):
                raise BackupError(
                    "usage: update project compose version target target-compose [options]"
                )
        else:
            no_backup = False
        with project_lock(Path(project).resolve()):
            with phase("installed configuration preflight"):
                instance = Instance(project, compose, version)
            if action != "update":
                secure_store(instance.store)
            if action == "update":
                update(instance, *args, backup=not no_backup)
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
