# Backup and restore

Back up each installation independently. A remote source sync is not a wiki
backup: generated pages, their Git history, ingest state, credentials, and
OpenCode sessions live in the installation. Before changing releases or data
layout, make a backup you can restore.

## Safe launcher updates and explicit rollback

Install `karpathy-wiki.sh` and its matching `launcher_backup.py` together. Safe
updates must run on the Docker host (or a maintenance container with identical
host paths): local snapshot paths must refer to the same files as the daemon's
bind sources. An unrelated remote Docker filesystem is outside the guarantee.
Safe updates require Python 3.11+, GNU tar with ACL/xattr support, Docker Compose with
`config --no-env-resolution` and `up --wait`, and permission to read and restore
all files and their ownership. `.env` is authoritative; transient shell overrides
of stack configuration are not supported for safe update/rollback. Pin the actual
installed release and cache its Compose file before updating. Images must exist
locally and match the installed containers; no registry access is needed to restore
saved images. Reconcile mismatched/orphan containers before a new update.

For update snapshots, required software means every unprofiled service, every
service enabled by saved `COMPOSE_PROFILES` (including `*`, with whitespace around
comma-separated names ignored), their required dependencies,
and every service with an existing project container, even stopped or in a disabled
profile. Only disabled services without containers are excluded from the image
archive; enabling them later requires installing their software separately.
An optional dependency (`required: false`) does not enable a disabled service;
its edge may remain in the resolved model even when that service is absent.
Configuration/data/file secrets remain fully covered, including disabled profiles.
Rescue snapshots retain all locally available declared images and all existing
container images, including partially updated software; they do not invent images
for never-installed services. Restore loads saved image IDs and retags them before
starting the saved configuration with its saved profiles and `--pull never`.

If a configured tag is missing locally, the launcher can still save the exact
installed image by ID when a container of this project and service was created
from that same configured reference and its image ID remains locally inspectable.
This does not pull software or recreate tags during preflight; rollback recreates
the saved tag. Containers created from another reference (including `latest`) do
not prove which image belongs to the configured version and are not substituted.

Required-image and pinned/container consistency checks run before launcher staging,
backup storage creation, or stopping writers. A failed inspection names the image
and the phase: check Docker access first, then restore the exact originally
installed image from a trusted saved archive or an independently verified immutable
digest and reconcile the configured tag with container image IDs. Do not pull a
mutable tag and assume it is the installed software. The launcher never acquires
missing recovery images automatically and never bypasses a required-image failure.
Compose downloads may populate `.cache` during earlier version resolution; the
installed pair, `.env`, data and running containers stay untouched on preflight failure.

Updates print concise phases on stderr in execution order, including early
resolution/configuration/staging failures. Use `update -v`, `update --verbose`, or
`update <version> --verbose` for elapsed phase/operation times and safe operation
names. Options belong after `update`, on either side of the optional version;
`--no-backup` can appear anywhere there too. Repeated verbose flags are idempotent.
Startup and health verification are one Compose `up --wait` phase. Verbose mode
does not disable secret protection: arguments, raw stdout/stderr, Compose config,
environment values and secret files are never dumped. Failures report curated
context and exit codes instead. Inspect Docker state privately when these safe
diagnostics are insufficient; do not publish private configuration or command output.

### Launcher bootstrap and recovery

Older launchers cannot update themselves. Once, with no launcher operations or
host writers running, preserve the installed pair separately and download
`karpathy-wiki.sh` and `launcher_backup.py` from the **same reviewed release tag**
that includes self-update support. Stage both outside the installation, check
`sh -n karpathy-wiki.sh` and `python3 launcher_backup.py check-runtime`, then
install both together in the project directory (`0755` shell, `0644` helper).
Do not run a launcher between these manual replacements. Register the pair with
`./karpathy-wiki.sh launcher-accept <installed-launcher-version>`. Fresh manual
installations also require this explicit initial acceptance. This records an
operator-attested version and SHA-256 hashes, not a cryptographic release signature.
The launcher version can differ from the stack version pinned in `.env`.

Subsequent `update [version]` commands resolve one release, stage and validate
both files (shell syntax and the target Python runtime/protocol), then take a
verified backup by default. Target releases must support launcher protocol 1;
PR preview versions and older non-self-updating launchers cannot be update targets.
Bootstrap acceptance also validates protocol/runtime support: unsupported local
launcher edits cannot be accepted as a future rollback pair. Rollback rejects
missing launcher metadata, unsupported saved pairs or mismatched hashes before
loading images, removing containers or replacing live configuration/data.
The Compose file is cached by that same release version. Python 3.11+ is required
on the host; if a future target needs unavailable dependencies, its runtime probe
fails before stopping writers or changing the installed pair or `.env`.

Downloads over HTTPS from this repository's release tags are **trusted executable
host code**: the target helper's runtime probe executes before backup. Hashes detect
local changes and interrupted writes, not a compromised publisher or moved tag.
Keep installation and staging directories writable only by trusted operators;
do not share one launcher pair across different project directories. Locally
modified files block updates. Preserve/review edits first; an explicit
`launcher-accept <version>` authorizes replacing those exact bytes on the next
update, with the accepted pair retained in the backup. Prefer configuration and
Compose overrides over launcher edits.
The reserved `.launcher-stage` directory must not overlap current data, backup
storage, snapshot roots or saved restore destinations; overlap is rejected before
staging cleanup or destructive restoration.

After image pull, the old shell **execs** the in-memory updater and never resumes
reading its replaced script. Pair activation uses a durable
`.launcher-activation.json` guard under the installation lock, fsynced staged
files and atomic individual replacements; two file renames are not pair-atomic.
The guard blocks normal launcher operations until the complete pair and its hash
record are durable. The same protocol applies when explicitly restoring a backup.

If activation is interrupted, **do not delete the guard or run the installed mixed
helper**. With host writers stopped, from the installation directory run:

```bash
python3 .launcher-stage/launcher_backup.py recover "$PWD"
```

This takes the same installation lock, verifies staged hashes, completes only the
selected launcher pair and clears the guard. It does not change `.env`, restore
data or restart containers. Inspect `backup list` and the private snapshot metadata,
then retry the update or choose an explicit full rollback. An interruption after
pair activation can leave a complete new launcher with the old stack pin, or a
new pin without a verified startup; this is not reported as update success. If
staging is missing/corrupt or recovery fails, retain target/rescue snapshots and
the guard, stop writers, and manually reinstall a complete reviewed pair before
investigating configuration/data recovery. No automatic data rollback occurs.

Configure `KARPATHY_WIKI_BACKUP_DIR` in `.env` (default `./backups`, relative to the
installation). It must be outside `DATA_ROOT`, owned by the operator, mode `0700`.
All snapshot files are private. Choose trusted, preferably encrypted local storage:
there is no built-in encryption, cloud upload, offsite copy or protection against
failure of the same disk. Allow space for complete data and image archives plus
restore staging and a rescue snapshot; archive/write/verification errors block
updates without changing the data, version or configuration. The stack can remain
stopped on failure: inspect the error and restart the unchanged release explicitly.

```bash
./karpathy-wiki.sh update 0.2.0
./karpathy-wiki.sh backup list
./karpathy-wiki.sh rollback <backup-id>
```

Every update stops **all containers labeled with this Compose project**, including
inactive-profile and orphan writers. Unless `--no-backup` is explicitly given, it archives the entire data root (hidden
files, Git history, SQLite and queue state, sessions and credentials), `.env`, both
launcher files, the old cached Compose configuration, override, file secrets,
service env files, and external read-only bind sources. It saves actual local
image IDs and image contents, verifies SHA-256 and reads archive bodies before
changing `.env`. Pull, startup and health failures report the snapshot ID and
explicit rollback command. Successful startup requires Compose `up --wait` (up to
600 seconds); services without health checks are checked for running state, not
application-level correctness. Check the wiki/chat before resuming ingestion.

`backup list` exposes only ID, time, version, kind and status, never configuration
or secret values. `pending` means completion was interrupted/unknown: keep it.
Only successful update snapshots may be pruned, only after a successful update,
only at age 30 days or older, and never among the latest three update snapshots.
Failed/pending updates and separate rescue snapshots are retained indefinitely.
Incomplete `.partial-*` directories are not selectable and require operator
inspection and manual removal.

### Explicitly skip an update backup

```bash
./karpathy-wiki.sh update --no-backup
./karpathy-wiki.sh update 0.2.0 --no-backup
```

The flag can appear before or after the version. It applies to this update only,
never through `.env`, and prints a warning. **No snapshot or new rollback point is
created**, including for the replaced launcher pair. No backup storage is created,
written or pruned; storage permissions, old local images and archive tools are not required.
Supported configuration/layout and staging-overlap checks, the installation lock,
launcher acceptance, stopping writers, guarded pair activation and startup health
verification still apply. Snapshot-content checks (including ownership, special
files and escaping links) do not run because no archive is made.

On failure, the launcher attempts to stop all instance containers and reports the
failed phase without inventing a backup ID or rollback command. Inspect the actual
pin, launcher and data state before recovery; an earlier snapshot restores its
older state, not the state immediately before this update. This option does not
disable the mandatory rescue snapshot for `rollback`.

Rollback works immediately or after days and additional updates. It displays the
date and current/target versions and warns that **all changes since the snapshot
are lost in the restored state**. Type the exact requested confirmation. It then
stops writers and verifies a separate `rescue` snapshot of the current state. If
that fails, current data/configuration are not replaced. The chosen snapshot is
staged beside each destination, current containers removed, and complete roots
replaced, not overlaid or merged. Saved images are loaded and tagged before the
old stack starts without pulling; health verification precedes success. A restore
failure can leave a stopped/partially restored installation: retain target and
rescue snapshots and do not start writers until recovery. Neither error path
automatically rolls back data. There are no data migrations or legacy fallbacks.

### Coverage boundaries

The launcher is the supported safe entry point; direct Docker/Compose commands
have no backup/rollback guarantee. Do not start other launcher/Compose operations
or host writers during maintenance. The installation lock serializes all launcher
Compose calls and update/rollback commands; close long-running `logs -f` before
maintenance. Host processes and other Compose projects writing to
the same data are outside the guarantee and must be stopped by the operator.

Named/anonymous volumes, digest-only image references, external Docker secrets/configs, custom builds, external
writable binds and escaping symlinks are rejected before a backed-up update. Move persistent
writers into the dedicated `DATA_ROOT`; read-only external binds are included.
File secrets referenced by `.env` must exist, even for disabled profiles; remove
unused `*_FILE` entries or supply their files before updating. Hard-linked
configuration roots must be independent copies (links within `DATA_ROOT` remain
preserved). Snapshot roots need writable parents for staged replacement and must
not be filesystem mountpoints; use a dedicated subdirectory on the mounted disk.
Custom
Compose `include`/`extends` and remote configuration are not supported; flatten
them into the override. Proxy configuration and other non-Compose dependencies
cannot be discovered automatically: explicitly list required **files/directories**
in `KARPATHY_WIKI_BACKUP_EXTRA_PATHS`, a JSON array (single-quote the array in
`.env` when needed). The operator must stop external writers of those paths and
reload the proxy after restore. Shared proxy files are restored in full, so prefer
per-instance configuration. Backups are bound to the original installation path
and Compose project; another instance cannot select them for launcher rollback.
For separate-instance testing use the manual procedure below.

The only external-link exceptions are the exact links generated by the OpenCode
entrypoint beneath `DATA_ROOT/opencode/config`: `AGENTS.md` to
`/etc/opencode/routing.md`, and `plugins/wiki-ingest-{journal,status,transaction}.js`
to the same filenames under `/etc/opencode/plugins/`. They are saved and restored
as symlinks, **never dereferenced on the host**; their target files come from the
saved container image. The allowlist checks both location and literal target before
and after stopping writers. Other external links still fail closed. Internal
relative links such as `sources/*/current` remain unchanged. No manual unlinking
or data reorganization is needed for these generated links.
If an installed older launcher still reports `snapshot contains an external symlink`
for these links, bootstrap both launcher files from a reviewed release containing
this fix using the procedure above. Keep the actual stack version pinned in `.env`;
updating container images alone cannot fix the host's backup helper.

## Manual backup (independent of updates)

1. Stop the stack so the wiki Git repository, manifests, and SQLite state are
   captured at the same point in time:

   ```bash
   ./karpathy-wiki.sh down
   ```

2. Archive the **entire** `DATA_ROOT`, including hidden files (in particular
   `wiki/.git`), `sources/`, `state/`, and `opencode/`. For an absolute data
   root such as `/srv/karpathy-wiki/personal`, a host administrator can use:

   ```bash
   tar -C /srv/karpathy-wiki -cpf personal-data.tar personal
   ```

   Keep the archive outside `DATA_ROOT`. Do not back up just `wiki/*.md` or
    omit `state/ingest.sqlite3`. Include the entire private ingest-journal.

3. Separately save the installation's `.env`, `karpathy-wiki.sh`, any
   `compose.override.yaml`, and every secret file referenced by `.env`
   (including `REDACTIONS_FILE` and `PAPERLESS_TOKEN_FILE` if configured).
   Secrets may be outside `DATA_ROOT`. Store the archives and configuration
   together in access-controlled, encrypted storage. Record the pinned release
   and any reverse-proxy configuration needed to restore the routes.

4. Start the same version again with `./karpathy-wiki.sh up -d`. Check service
   health and the wiki before resuming ingestion.

## Restore test

Use a separate installation directory, `DATA_ROOT`, project name, stack ID,
and proxy network so a restore cannot affect the live instance. Do not point
the restored providers at live upstream sources while testing. Stop the test
stack before extracting; extract the archived data root into an empty directory
and restore `.env`, the launcher, overrides, and secret files. Update the
restored `.env` to refer to the test directories and identity, retain its
original `KARPATHY_WIKI_VERSION`, and set `COMPOSE_PROFILES=` for the initial
check. Preserve file ownership or set `PUID`/`PGID` to the intended host owner.

From the test installation, check the restored repository and Compose
configuration before starting services:

```bash
git -C /path/to/restored/data/wiki fsck --full
./karpathy-wiki.sh config --quiet
./karpathy-wiki.sh up -d
```

Check the restored `index.md` and a representative committed source page in
SilverBullet, verify the Git history, and confirm OpenCode can read the wiki.
Retain the complete private journal and wiki `.git` in backups, including any
older recovery evidence. Inspect unresolved live edits before resuming ingestion.
Then stop the test stack and remove only its own resources. A missing or corrupt
SQLite state file should be investigated before re-enabling providers: deleting
it resets ingest bookkeeping and may cause upstream sources to be reprocessed.

For a real restore, stop the old stack first, restore into an empty `DATA_ROOT`,
restore configuration and secrets, verify the Git repository and Compose
configuration, then start the pinned version. Review release notes and reorganize
data manually before changing pre-1.0 releases; do not restore an older data
layout over a running newer installation.
