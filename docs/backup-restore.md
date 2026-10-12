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
inactive-profile and orphan writers. It archives the entire data root (hidden
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
inspection and manual removal; there is no skip-backup option.

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
writable binds and escaping symlinks are rejected before update. Move persistent
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
    omit `state/ingest.sqlite3`. Include the entire private ingest-journal and
    `publisher/` queue, ownership and replay evidence; do not reset ownership
    bookkeeping to unblock a restored publisher.

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
Then stop the test stack and remove only its own resources. A missing or corrupt
SQLite state file should be investigated before re-enabling providers: deleting
it resets ingest bookkeeping and may cause upstream sources to be reprocessed.

For a real restore, stop the old stack first, restore into an empty `DATA_ROOT`,
restore configuration and secrets, verify the Git repository and Compose
configuration, then start the pinned version. Review release notes and reorganize
data manually before changing pre-1.0 releases; do not restore an older data
layout over a running newer installation.
