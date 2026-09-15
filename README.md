# Karpathy Wiki

A self-hosted Markdown knowledge base inspired by [Andrej Karpathy's LLM wiki
pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).
OpenCode turns explicitly selected source material into linked Markdown pages,
and SilverBullet provides the read-only browser UI. Each installation keeps its
own configuration, sources, wiki history, credentials, and sessions.

## Quick Start

Requirements:

- Docker Engine with Docker Compose v2
- A reverse proxy attached to a Docker network
- An account with an [OpenCode-supported model provider](https://opencode.ai/docs/providers/)

Create an empty directory and run the installer. A Git checkout is not required.
The directory name becomes `COMPOSE_PROJECT_NAME` and `STACK_ID`; use lowercase
letters, digits, and hyphens.

```bash
mkdir my-wiki
cd my-wiki
curl -fsSL https://raw.githubusercontent.com/mstroppel/karpathy-wiki/main/install.sh | sh
```

Review `install.sh` and `karpathy-wiki.sh` before piping them to a shell if
required by your security policy. To install a specific release:

```bash
curl -fsSL https://raw.githubusercontent.com/mstroppel/karpathy-wiki/main/install.sh \
  | KARPATHY_WIKI_VERSION=0.1.0 sh
```

Edit `.env` with at least:

```env
WIKI_NAME=My Wiki
WIKI_PUBLIC_URL=https://wiki.example.com
OPENCODE_PUBLIC_URL=https://chat.example.com
DATA_ROOT=./data
OPENCODE_SERVER_PASSWORD=replace-with-a-long-random-password
COMPOSE_PROFILES=
```

Keep `PUID=1000` and `PGID=1000` only when they match the host user that should
own the wiki files. Generate a password with `openssl rand -base64 32`.

Create the proxy network, validate the configuration, and start the stack:

```bash
docker network inspect webproxy >/dev/null 2>&1 || docker network create webproxy
./karpathy-wiki.sh config --quiet
./karpathy-wiki.sh up -d
```

Configure the reverse proxy with these upstreams, replacing `karpathy-wiki` if
`STACK_ID` was changed:

```text
karpathy-wiki-silverbullet:3000  # WIKI_PUBLIC_URL
karpathy-wiki-opencode:4096     # OPENCODE_PUBLIC_URL
```

Sign in to `OPENCODE_PUBLIC_URL` as `opencode`, connect a model provider, and
ask OpenCode to ingest a source. For a first test without an adapter:

```bash
cp /path/to/document.pdf ./data/sources/webdav/
```

Then ask OpenCode to import `/knowledge/sources/webdav/document.pdf`. Generated
pages are written to `wiki/` and can be read at `WIKI_PUBLIC_URL`.

## Profiles

Set `COMPOSE_PROFILES` in `.env`; profiles can be enabled independently:

| Profile | Purpose |
| --- | --- |
| `webdav` | Mirror and locally redact a selected WebDAV folder |
| `paperless` | Export tagged OCR text and redact configured personal data |
| `session-export` | Archive inactive OpenCode sessions as PDFs in Nextcloud |
| `raw-files` | Expose source files to a trusted reverse proxy |

See [configuration](docs/configuration.md), [Paperless ingestion](docs/paperless.md),
[session export](docs/session-export.md), and [migration](docs/migration.md) for
profile-specific setup and production paths. See [architecture](docs/architecture.md)
for service boundaries and [data layout](docs/data-layout.md) for the persistent
folder structure.

## Updates

The launcher downloads and caches the Compose file matching the pinned release
in `.env`. Update to the latest release with:

```bash
./karpathy-wiki.sh update
```

To pin a release:

```bash
./karpathy-wiki.sh update 0.2.0
```

Back up `DATA_ROOT` and review release notes before upgrades that change the
data layout or generated policies. Local Compose changes belong in
`compose.override.yaml` and survive updates.

## Security

- Never commit `.env`, tokens, redaction lists, source material, wiki content, or exported sessions.
- Paperless redaction is an explicit deny-list, not general anonymization. Review output before sending it to an external model provider.
- WebDAV material is not redacted.
- Put OpenCode and `raw-files` behind a trusted, authenticated reverse proxy; do not expose them directly to the internet.
- The OpenCode configuration is not a substitute for host-level network isolation or least-privilege model credentials.

Report vulnerabilities according to [SECURITY.md](SECURITY.md).

## Development

Run the local checks:

```bash
python3 -m unittest discover -s tests -v
node --test tests/test_wiki_ingest_status.mjs
(cd paperless-ingest && python3 -m unittest discover -s tests -v)
(cd session-export && python3 -m unittest discover -s tests -v)
sh -n config/init.sh && sh -n install.sh && sh -n karpathy-wiki.sh
sh -n session-export/export-session.sh
node --check config/tools/wiki_ingest_status.js
node --check config/tools/wiki_ingest_status_core.mjs
for adapter in config/ingest-adapters/*/status.mjs; do node --check "$adapter"; done
docker compose --env-file .env.example config --quiet
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow.

## License

[MIT](LICENSE)
