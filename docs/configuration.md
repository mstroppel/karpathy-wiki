# Configuration

Karpathy Wiki uses one Compose file for every installation. Keep instance
configuration in an untracked environment file and pin `KARPATHY_WIKI_VERSION`
when reproducible upgrades are required.

## Required Instance Values

| Variable | Description |
| --- | --- |
| `COMPOSE_PROJECT_NAME` | Unique Compose project and private network prefix |
| `STACK_ID` | Unique DNS alias prefix on the proxy network |
| `WIKI_NAME` | Human-readable title written during first initialization |
| `WIKI_PUBLIC_URL` | Browser-visible SilverBullet base URL |
| `OPENCODE_PUBLIC_URL` | Browser-visible OpenCode base URL |
| `DATA_ROOT` | Persistent instance directory; absolute paths are recommended |
| `PUID`, `PGID` | Host identity used by long-running services |
| `WEBPROXY_NETWORK` | Existing external reverse-proxy network |
| `OPENCODE_SERVER_PASSWORD` | Long random password protecting the OpenCode server and API |

`COMPOSE_PROJECT_NAME` and `STACK_ID` should contain lowercase letters, digits,
hyphens, or underscores. Use a different pair and `DATA_ROOT` for every instance.
Every persistent service directory is created below `DATA_ROOT`; no Compose
override file is needed to place a new instance's data on another filesystem.
Use an absolute path in production, for example
`DATA_ROOT=/srv/karpathy-wiki/personal`.

## Profiles

Select optional services with a comma-separated value:

```env
COMPOSE_PROFILES=webdav,paperless,session-export,raw-files
```

An installation can run without source adapters and receive files through a
separate trusted process. Set `PAPERLESS_ENABLED=true` exactly when the
`paperless` profile is active so initialization installs the corresponding wiki
rules and directories.

## Secrets

WebDAV uses rclone's obscured password format. Obscuring is not encryption;
protect the environment file as a credential:

```bash
docker run --rm rclone/rclone:1.75.1 obscure 'WEBDAV_PASSWORD'
```

Set `WEBDAV_URL`, `WEBDAV_VENDOR`, `WEBDAV_USERNAME`,
`WEBDAV_PASSWORD_OBSCURED`, `WEBDAV_PATH`, and `WEBDAV_SYNC_INTERVAL` for the
source adapter. `WEBDAV_VENDOR` defaults to `nextcloud`; rclone also supports
other WebDAV implementations. The legacy `nextcloud` profile and `NEXTCLOUD_*`
source variables remain accepted for one migration release.

Paperless credentials and redaction values use files rather than environment
values. Set absolute paths when possible:

```env
PAPERLESS_TOKEN_FILE=/private/personal-wiki/paperless-token
REDACTIONS_FILE=/private/personal-wiki/redactions.json
```

Apply mode `0600` to both files. Never place real values below the repository's
`secrets/` directory in a commit.

`OPENCODE_SERVER_PASSWORD` currently uses an environment value because OpenCode
expects that variable directly. Protect the environment file with mode `0600`.

## Reverse Proxy

Compose creates these aliases on `WEBPROXY_NETWORK`:

```text
${STACK_ID}-silverbullet
${STACK_ID}-opencode
${STACK_ID}-raw-files
```

The corresponding ports are `3000`, `4096`, and `8080`. Apply authentication and
source-network restrictions at the reverse proxy. Do not publish container ports
directly from Compose.

## Upgrades

Pin a release independently per instance:

```env
KARPATHY_WIKI_VERSION=0.1.0
```

Then pull and recreate only that instance:

```bash
docker compose --env-file /private/personal-wiki.env \
  --project-name personal-wiki pull
docker compose --env-file /private/personal-wiki.env \
  --project-name personal-wiki up -d --remove-orphans
```

Review release notes before changing the version. Back up `DATA_ROOT` before
upgrades that announce data-layout or generated-policy changes.
