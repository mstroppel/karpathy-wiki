# Karpathy Wiki

A self-hosted, continuously curated Markdown knowledge base inspired by
[Andrej Karpathy's LLM wiki pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).
OpenCode reads explicitly selected source material, maintains a linked wiki in
its own Git repository, and SilverBullet provides a read-only browser UI.

The project is designed for multiple independent installations. Every instance
uses the same Compose file and versioned container images while keeping its
configuration, source material, wiki history, credentials, and OpenCode sessions
separate.

## Architecture

```text
WebDAV ------- rclone --------------------> sources/webdav/ ------+
                                                                 |
Paperless ---- local redaction (optional) -> sources/paperless/ --+--> OpenCode
                                                                       |
                                                                       v
                                                                  wiki/ + Git
                                                                       |
                                                                       v
                                                                  SilverBullet

OpenCode sessions -- optional PDF export ------------------------> Nextcloud
```

The core consists of `init`, `opencode`, and `silverbullet`. Source and export
adapters are enabled independently through Compose profiles:

| Profile | Purpose |
| --- | --- |
| `webdav` | Mirror a selected WebDAV folder into the read-only source tree |
| `paperless` | Export tagged OCR text and redact configured personal data locally |
| `session-export` | Archive inactive OpenCode sessions as PDFs in Nextcloud |
| `raw-files` | Expose source files to a trusted reverse proxy |

The experimental analysis UI is deliberately not part of this project. It can
be added later without changing the source and wiki contracts.

## Create Your First Wiki

You need Docker Engine with Docker Compose v2, a reverse proxy attached to a
Docker network, and an account with an
[OpenCode-supported model provider](https://opencode.ai/docs/providers/).
WebDAV, Paperless, and the other Compose profiles are optional and are not
needed for the first start.

1. Download the small installer. It resolves the latest release, downloads its
   `compose.yaml` and `.env.example`, and pins the matching container image
   version in the generated `.env`. A Git checkout is not required:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/mstroppel/karpathy-wiki/main/install.sh \
     | INSTALL_DIR=my-wiki sh
   cd my-wiki
   ```

   `INSTALL_DIR` only sets the local installation directory. Configure the wiki
   name with `WIKI_NAME` and its unique container/DNS prefix with
   `COMPOSE_PROJECT_NAME` and `STACK_ID` in `.env`.

   Review [`install.sh`](install.sh) before piping it to a shell if required by
   your security policy. Set `INSTALL_DIR` or `KARPATHY_WIKI_VERSION` on `sh` to
   choose another directory or a specific release:

   ```bash
   curl -fsSL https://raw.githubusercontent.com/mstroppel/karpathy-wiki/main/install.sh \
     | INSTALL_DIR=personal-wiki KARPATHY_WIKI_VERSION=0.1.0 sh
   ```

2. Edit `.env`. For a minimal installation, set these values and leave
   `COMPOSE_PROFILES` empty:

   ```env
   WIKI_NAME=My Wiki
   WIKI_PUBLIC_URL=https://wiki.example.com
   OPENCODE_PUBLIC_URL=https://chat.example.com
   DATA_ROOT=./data
   OPENCODE_SERVER_PASSWORD=replace-with-a-long-random-password
   COMPOSE_PROFILES=
   ```

   Keep the default `PUID=1000` and `PGID=1000` only if they match the host user
   that should own the wiki files. A password can be generated with
   `openssl rand -base64 32`.

3. Create the shared proxy network if it does not exist, validate the
   configuration, and start the wiki:

   ```bash
   docker network inspect webproxy >/dev/null 2>&1 || docker network create webproxy
   docker compose config --quiet
   docker compose up -d
   ```

   The first start creates `${DATA_ROOT}`, initializes the Markdown wiki as an
   independent Git repository, and starts OpenCode and SilverBullet. Configure
   the reverse proxy with these upstreams (replace `karpathy-wiki` if you changed
   `STACK_ID`):

   ```text
   karpathy-wiki-silverbullet:3000  # WIKI_PUBLIC_URL
   karpathy-wiki-opencode:4096      # OPENCODE_PUBLIC_URL
   ```

4. Open `OPENCODE_PUBLIC_URL`, sign in with user `opencode` and the configured
   password, and connect the model provider in the OpenCode UI. To create the
   first wiki content without an adapter, copy a document into the local source
   directory and explicitly ask OpenCode to ingest it:

   ```bash
   cp /path/to/my-document.pdf ./data/sources/webdav/
   ```

   This path assumes the minimal `DATA_ROOT=./data` setting above. Use the
   configured data directory if you changed it.

   Example prompt:

   ```text
   Import /knowledge/sources/webdav/my-document.pdf into the wiki.
   ```

   OpenCode writes the linked Markdown pages and creates a focused Git commit.
   Read the result at `WIKI_PUBLIC_URL`. Add the `webdav` or `paperless`
   profile later when sources should be synchronized automatically.

OpenCode provider credentials and session state are persisted below
`${DATA_ROOT}`. The services do not publish host ports; the reverse proxy must be
attached to `WEBPROXY_NETWORK`. See [configuration](docs/configuration.md) for
proxy, permissions, profiles, and production path settings.

All persistent service directories use subdirectories of `DATA_ROOT`, so a new
installation does not need a Compose override file. Secret files can remain
elsewhere through `PAPERLESS_TOKEN_FILE` and `REDACTIONS_FILE`.

## Multiple Instances

The stack does not use fixed container names. `COMPOSE_PROJECT_NAME` separates
containers and private networks, while `STACK_ID` creates unique aliases on the
shared reverse-proxy network.

```bash
docker compose --env-file /private/sp-wiki.env --project-name sp-wiki up -d
docker compose --env-file /private/mein-wiki.env --project-name mein-wiki up -d
```

Use absolute `DATA_ROOT`, `PAPERLESS_TOKEN_FILE`, and `REDACTIONS_FILE` paths
when the environment files live outside this checkout. Each instance can pin a
different `KARPATHY_WIKI_VERSION` and upgrade independently.

Example reverse-proxy upstreams for `STACK_ID=sp-wiki`:

```text
sp-wiki-silverbullet:3000
sp-wiki-opencode:4096
sp-wiki-raw-files:8080
```

Only attach trusted proxy infrastructure to `WEBPROXY_NETWORK`. OpenCode also
requires HTTP Basic authentication through `OPENCODE_SERVER_PASSWORD`.
Additional authentication, TLS, and network access restrictions belong in that
reverse proxy; OpenCode and the raw file server must not be exposed directly to
the public internet.

## Configuration

Copy `.env.example` and select profiles with `COMPOSE_PROFILES`:

```env
COMPOSE_PROJECT_NAME=personal-wiki
STACK_ID=personal-wiki
WIKI_NAME=Personal Wiki
DATA_ROOT=/srv/karpathy-wiki/personal
WIKI_PUBLIC_URL=https://wiki.example.com
OPENCODE_PUBLIC_URL=https://chat.example.com
OPENCODE_SERVER_PASSWORD=replace-with-a-long-random-password
COMPOSE_PROFILES=webdav,paperless
PAPERLESS_ENABLED=true
KARPATHY_WIKI_VERSION=latest
```

`PAPERLESS_ENABLED` controls the generated wiki rules and must match whether the
`paperless` profile is enabled. See [configuration](docs/configuration.md),
[Paperless ingestion](docs/paperless.md), [session export](docs/session-export.md),
and [migration](docs/migration.md) for complete setup details.

## Data Layout

```text
${DATA_ROOT}/
├── sources/
│   ├── webdav/
│   └── paperless/
├── wiki/                 # Independent Git repository and SilverBullet space
├── quarantine/
│   └── paperless/        # Content-free Paperless error reports
├── exports/
│   └── sessions/         # Local OpenCode session PDF mirror
└── opencode/
    ├── config/
    ├── data/             # Credentials, sessions, messages, and logs
    └── state/
```

Source directories are read-only inside OpenCode. Generated knowledge is written
only to `wiki/`, and every successful write operation ends in a focused
Conventional Commit.

WebDAV source pages store the normalized path relative to `sources/webdav` and a
SHA-256 hash of the source bytes. `/ingest-new` uses these values to process new
and changed WebDAV and Paperless sources sequentially without silently deleting
knowledge for removed sources.

## Development

Run all local checks:

```bash
python3 -m unittest discover -s tests -v
node --test tests/test_wiki_ingest_status.mjs
(cd paperless-ingest && python3 -m unittest discover -s tests -v)
(cd session-export && python3 -m unittest discover -s tests -v)
sh -n config/init.sh
sh -n install.sh
sh -n session-export/export-session.sh
node --check config/tools/wiki_ingest_status.js
docker compose --env-file .env.example config --quiet
```

GitHub Actions additionally builds every project image. Releases publish
multi-architecture images to GHCR. Pull requests from repository branches publish
`linux/amd64` preview images tagged `pr-<number>`; set
`KARPATHY_WIKI_VERSION=pr-<number>` to test one. Dependabot patch/minor updates
and GitHub Actions updates are automatically approved and squash-merged after all
protected CI checks pass. Major runtime updates remain manual. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow.

## Security Notes

- Never commit `.env`, Paperless tokens, redaction lists, source material, wiki
  content, or exported sessions.
- Paperless redaction is an explicit deny-list, not general anonymization.
  Unknown values and OCR variants can remain in the output. Review samples before
  sending material to an external model provider.
- WebDAV material is not redacted.
- `raw-files` serves all configured sources and should only be enabled behind a
  trusted, authenticated proxy.
- The OpenCode configuration blocks source edits and direct `.git` edits, but it
  is not a substitute for host-level network isolation and least-privilege model
  credentials.

Report vulnerabilities according to [SECURITY.md](SECURITY.md).

## Open Points

- Activate the prepared deployment overlays for both existing installations in
  separate maintenance windows and complete a soak test after each cutover.
- Rotate the legacy Paperless, WebDAV/Nextcloud, and other deployment credentials that
  previously entered a Git history; removing them from the index does not remove
  them from existing commits.
- Move Paperless and its importer from a shared proxy network to a dedicated
  backend network in deployments that still use the shared network temporarily.
- Verify the released `linux/amd64` and `linux/arm64` images during the first
  activation on both hosts.
- Add an identity-aware authentication gateway in front of OpenCode in addition
  to its built-in HTTP Basic authentication.
- Decide whether managed `AGENTS.md` updates should be propagated automatically.
  Initialization currently preserves an existing file to avoid overwriting local
  policy changes.
- Verify controlled Paperless shutdown during an in-flight API or filesystem
  operation; interval waiting already stops promptly.
- Add end-to-end tests with disposable OpenCode, SilverBullet, WebDAV, and
  Paperless test services. Production containers are intentionally not used for
  repository tests.

## Possible Extensions

- Queue and automatically ingest new Paperless revisions one at a time.
- Provide optional source adapters for local directories or S3.
- Add backup/restore verification and migration tooling for the wiki Git history.
- Add an optional, separately tested analysis UI in a future release.
- Add OpenTelemetry-compatible health and operational metrics.

## License

[MIT](LICENSE)
