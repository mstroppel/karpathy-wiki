# Migration From SP-Wiki and Mein-Wiki

Migrate one installation at a time. Keep the old Compose project stopped but
available for rollback until the new stack has passed its soak test. Never move
or publish source material as part of the software repository.

## Before Migration

1. Back up both complete data roots and verify the backup can be listed and
   restored.
2. Rotate every credential that has previously entered a Git history, including
   Paperless tokens and credentials stored in tracked `.env` files.
3. Create private environment and secret files outside this repository.
4. Create the external reverse-proxy network if it does not already exist.
5. For Paperless, create a dedicated external backend network and attach the
   Paperless application to it.
6. Stop the old instance before attaching its writable wiki directory to the new
   stack.

## SP-Wiki Mapping

Use profiles:

```env
COMPOSE_PROJECT_NAME=sp-wiki
STACK_ID=sp-wiki
COMPOSE_PROFILES=nextcloud,session-export,raw-files
PAPERLESS_ENABLED=false
```

Map the old paths as follows:

| Old | New below `DATA_ROOT` |
| --- | --- |
| `raw/nextcloud` | `sources/nextcloud` |
| `wiki` | `wiki` |
| `opencode-config` | `opencode-config` |
| `opencode-share` | `opencode-share` |
| `session-exports` | `session-exports` |

The canonical source path changes from `/knowledge/raw` to
`/knowledge/sources`. Existing wiki pages that record raw paths or download URLs
should be migrated in one reviewed Wiki commit after startup.

## Mein-Wiki Mapping

Use profiles:

```env
COMPOSE_PROJECT_NAME=mein-wiki
STACK_ID=mein-wiki
COMPOSE_PROFILES=nextcloud,paperless
PAPERLESS_ENABLED=true
PAPERLESS_NETWORK=paperless-backend
```

The existing layout already closely matches the canonical structure:

| Old | New below `DATA_ROOT` |
| --- | --- |
| `knowledge/sources` | `sources` |
| `knowledge/wiki` | `wiki` |
| `quarantine` | `quarantine` |
| `opencode-config` | `opencode-config` |
| `opencode-share` | `opencode-share` |

Move the Paperless token and redaction list to private, untracked files and set
their absolute paths in the instance environment.

## Validation

For each instance:

```bash
docker compose --env-file /private/INSTANCE.env config --quiet
docker compose --env-file /private/INSTANCE.env --project-name INSTANCE up -d
docker compose --env-file /private/INSTANCE.env --project-name INSTANCE ps
docker compose --env-file /private/INSTANCE.env --project-name INSTANCE logs --tail=100
```

Verify source mounts are read-only inside OpenCode, the wiki Git status is clean,
SilverBullet is read-only, proxy routes target the new `${STACK_ID}-*` aliases,
and optional services complete at least one successful cycle.
