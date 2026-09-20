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
| `OPENCODE_PASSWORD` | Stable OpenCode v2 server password shared with internal API clients |
| `DATA_ROOT` | Persistent instance directory; absolute paths are recommended |
| `PUID`, `PGID` | Host identity used by long-running services |
| `WEBPROXY_NETWORK` | Existing external reverse-proxy network |

`COMPOSE_PROJECT_NAME` and `STACK_ID` should contain lowercase letters, digits,
hyphens, or underscores. Use a different pair and `DATA_ROOT` for every instance.
Every persistent service directory is created below `DATA_ROOT`; no Compose
override file is needed to place a new instance's data on another filesystem.
Use an absolute path in production, for example
`DATA_ROOT=/srv/karpathy-wiki/personal`.

OpenCode v2 requires server authentication even behind a reverse proxy. Fresh
installs and `karpathy-wiki.sh update` generate `OPENCODE_PASSWORD` when it is
missing; manual deployments must replace the example value with a strong random
secret. Keep it private. Open the pairing URL shown by
`docker compose exec opencode opencode pair` once to save the credential in the
browser.

## Profiles

Select optional services with a comma-separated value:

```env
COMPOSE_PROFILES=webdav,paperless,raw-files
```

An installation can run without source adapters and receive files through a
separate trusted process. When the `paperless` profile is active, initialization
automatically installs the corresponding wiki rules and directories.

Ingest tracking discovers adapters from directories below
`/knowledge/sources`. A directory is handled only when the trusted OpenCode image
contains a matching `/etc/opencode/ingest-adapters/<directory>/status.mjs`;
otherwise it is reported as invalid. Adding another source type therefore
requires only its source directory, status module, and source-specific ingest
validation, not a change to `/ingest-new`, its skill, or the generic status tool.

Each status module exports one asynchronous default function. It receives
`sourceRoot` and `wikiSourceRoot` and returns the arrays `new`, `outdated`,
`current`, `conflict`, `revoked`, `orphaned`, and `invalid`. Pending and current
items share these required fields:

| Field | Purpose |
| --- | --- |
| `source_key` | Stable identity and deterministic adapter-local order |
| `source_path` | Absolute file path below the adapter's source directory |
| `source_revision` | Lowercase SHA-256 revision |
| `wiki_path` | Absolute target below the wiki source directory |
| `frontmatter` | Trusted adapter metadata including the same `source_revision` |

The generic tool validates this contract and converts load, execution, and
contract errors into `invalid` results. `frontmatter` must contain only
JSON-compatible values, and `wiki_path` must be unique across every discovered
adapter; collisions are reported as `conflict`. Adapter modules are loaded only
from the read-only OpenCode image; code found in source directories is never
executed.

## Secrets

WebDAV uses rclone's obscured password format. Obscuring is not encryption;
protect the environment file as a credential. The rclone version is pinned in
`docker-bake.hcl` (`RCLONE_VERSION`, currently `1.75.1`):

```bash
docker run --rm rclone/rclone:1.75.1 obscure 'WEBDAV_PASSWORD'
```

Set `WEBDAV_URL`, `WEBDAV_VENDOR`, `WEBDAV_USERNAME`,
`WEBDAV_PASSWORD_OBSCURED`, `WEBDAV_PATH`, and `WEBDAV_SYNC_INTERVAL` for the
source adapter. `WEBDAV_VENDOR` defaults to `nextcloud`; rclone also supports
other WebDAV implementations.

WebDAV files are synchronized into a private staging directory first. The
`webdav-ingest` service applies `REDACTIONS_FILE` locally and publishes only
UTF-8 text files to `sources/webdav`; files that cannot be read as text are
kept out of the source tree and written to `quarantine/webdav`. A failed
upstream synchronization is retried on the next `WEBDAV_SYNC_INTERVAL` and
surfaced through the Compose healthcheck instead of restarting the daemon.

Paperless credentials use files rather than environment values. Set an absolute
path when possible:

```env
PAPERLESS_TOKEN_FILE=/private/personal-wiki/paperless-token
```

`REDACTIONS_FILE` is shared by all ingestion adapters and should also use an
absolute path, for example `/private/personal-wiki/redactions.json`. Apply mode
`0600` to both files. Never place real values below the repository's `secrets/`
directory in a commit.

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

### Caddy without browser pairing

OpenCode v2 always requires Basic Auth, but Caddy can supply that credential to
the upstream so users do not need to enter or store the OpenCode password in
their browsers. First generate the value for the Basic Auth header from the
running container:

```bash
docker compose exec -T opencode sh -c \
  'printf "opencode:%s" "$OPENCODE_PASSWORD" | base64 | tr -d "\n"'
```

Store the output as `OPENCODE_UPSTREAM_AUTH` in Caddy's environment, not in the
Caddyfile or this repository. Pass it to the Caddy container if Caddy also runs
through Compose:

```yaml
services:
  caddy:
    environment:
      OPENCODE_UPSTREAM_AUTH: ${OPENCODE_UPSTREAM_AUTH}
```

Authenticate public requests using Caddy's `basic_auth`, `forward_auth`, or an
equivalent trusted access-control handler, then replace the header sent to
OpenCode:

```caddyfile
chat.example.com {
    # Configure basic_auth, forward_auth, or another access policy here.

    reverse_proxy karpathy-wiki-opencode:4096 {
        header_up Authorization "Basic {$OPENCODE_UPSTREAM_AUTH}"
    }
}
```

The alias must match `${STACK_ID}-opencode`, and Caddy must be attached to
`WEBPROXY_NETWORK`. The `header_up` rule deliberately replaces any client-sent
`Authorization` header. Never use this rule on a publicly accessible route
without separate authentication: Caddy would otherwise grant every visitor
access to OpenCode. Restart or reload Caddy after changing the environment. If
`OPENCODE_PASSWORD` changes, regenerate `OPENCODE_UPSTREAM_AUTH` before restarting
OpenCode to avoid locking out proxy traffic.

## Upgrades

Launcher installations update through the launcher, which rewrites the version
pin in `.env` (keeping `.env.bak`), pulls the new images, and restarts:

```bash
./karpathy-wiki.sh update          # latest release
./karpathy-wiki.sh update 0.2.0    # specific release
```

Pin a release independently per instance:

```env
KARPATHY_WIKI_VERSION=0.1.0
```

Checkouts and multi-instance setups that drive Compose directly pull and
recreate only the selected instance:

```bash
docker compose --env-file /private/personal-wiki.env \
  --project-name personal-wiki pull
docker compose --env-file /private/personal-wiki.env \
  --project-name personal-wiki up -d --remove-orphans
```

Review release notes before changing the version. Back up `DATA_ROOT` before
upgrades that announce data-layout or generated-policy changes.
