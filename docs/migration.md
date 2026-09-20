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
5. Stop the old instance before attaching its writable wiki directory to the new
   stack.

## SP-Wiki Mapping

Use profiles:

```env
COMPOSE_PROJECT_NAME=sp-wiki
STACK_ID=sp-wiki
COMPOSE_PROFILES=webdav,session-export,raw-files
```

Map the old paths as follows:

| Old | New below `DATA_ROOT` |
| --- | --- |
| `raw/nextcloud` | `sources/webdav` |
| `wiki` | `wiki` |
| `opencode-config` | `opencode/config` |
| `opencode-share` | `opencode/data` |
| `opencode-state` | `opencode/state` |
| `session-exports` | `exports/sessions` |

The canonical source path changes from `/knowledge/raw` to
`/knowledge/sources`. Existing wiki pages that record raw paths or download URLs
should be migrated in one reviewed Wiki commit after startup. During the
transition, an installation may mount the same source directory read-only at
`/knowledge/raw`; the bundled OpenCode policy permits reads but denies writes on
that compatibility path.

## Mein-Wiki Mapping

Use profiles:

```env
COMPOSE_PROJECT_NAME=mein-wiki
STACK_ID=mein-wiki
COMPOSE_PROFILES=webdav,paperless
```

The existing layout already closely matches the canonical structure:

| Old | New below `DATA_ROOT` |
| --- | --- |
| `knowledge/sources` | `sources` |
| `knowledge/wiki` | `wiki` |
| `quarantine` | `quarantine/paperless` |
| `opencode-config` | `opencode/config` |
| `opencode-share` | `opencode/data` |
| `opencode-state` | `opencode/state` |

Move the Paperless token and redaction list to private, untracked files and set
their absolute paths in the instance environment.

## Upgrade From the Previous Data Layout

The init service automatically migrates these legacy directories when upgrading
an existing Karpathy Wiki installation:

| Legacy below `DATA_ROOT` | Current below `DATA_ROOT` |
| --- | --- |
| `opencode-config` | `opencode/config` |
| `opencode-share` | `opencode/data` |
| `opencode-state` | `opencode/state` |
| `session-exports` | `exports/sessions` |
| `sources/nextcloud` | `sources/webdav` |
| Files in `quarantine` | `quarantine/paperless` |

Stop the old stack and back up the complete data root before upgrading. Compose
may create empty current directories before init runs; these are safe migration
destinations. If both a legacy and current path contain data, init stops without
merging or overwriting either directory. Resolve that conflict manually from the
backup before restarting the stack.

## Upgrade To OpenCode v2

OpenCode v2 migrates credentials and v1 sessions in `opencode/data` to its new
database on startup. This is a one-way data migration. Stop the stack and back
up the complete `opencode` directory before upgrading, then allow the OpenCode
container to finish migration before testing existing sessions or enabling the
session exporter. Keep the backup until the upgraded instance has passed its
soak test; rolling the image back does not reverse the database migration.
Before starting v2, add a strong random `OPENCODE_PASSWORD` to the instance
environment. The `karpathy-wiki.sh update` command generates one when it is
missing; manual deployments must set it themselves. After startup, use
`docker compose exec opencode opencode pair` to obtain the browser pairing URL.

The init service does not rewrite the independently versioned wiki. Before using
`/ingest-new`, migrate existing `wiki/sources/nextcloud` pages in one reviewed
wiki commit: move each page below `wiki/sources/webdav` to the source-relative
path with an additional `index.md` and add `source_adapter: webdav`, the
normalized relative `source_path`, and the current SHA-256 `source_revision` to
its frontmatter. Also update the generated `wiki/AGENTS.md` references from
`/knowledge/sources/nextcloud` and `sources/nextcloud` to their WebDAV
counterparts. The status tool reports remaining legacy pages and source paths as
invalid so a batch import cannot accidentally duplicate their knowledge.

## Adoption of the Analysis Export

Existing installations keep their generated `wiki/AGENTS.md`. To route analysis
savings the same way as fresh installs, append one sentence to the delegation
paragraph of `wiki/AGENTS.md` in a reviewed wiki commit so it reads
"… ausdrücklich wissenschaftliche Analysen an `wiki-analysis` und das Speichern
fertiger Analysen an `wiki-analysis-save`." No data migration is required; see
[analysis export](analysis-export.md).

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
