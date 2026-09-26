# Backup and restore

Back up each installation independently. A remote source sync is not a wiki
backup: generated pages, their Git history, ingest state, credentials, and
OpenCode sessions live in the installation. Before changing releases or data
layout, make a backup you can restore.

## Backup

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
   omit `state/ingest.sqlite3`.

3. Separately save the installation's `.env`, `karpathy-wiki.sh`,
   `export-opencode-sessions.sh`, any `compose.override.yaml`, and every secret
   file referenced by `.env`
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
