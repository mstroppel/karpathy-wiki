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
Nextcloud ---- rclone --------------------> sources/nextcloud/ ---+
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
| `nextcloud` | Mirror a selected WebDAV folder into the read-only source tree |
| `paperless` | Export tagged OCR text and redact configured personal data locally |
| `session-export` | Archive inactive OpenCode sessions as PDFs in Nextcloud |
| `raw-files` | Expose source files to a trusted reverse proxy |

The experimental analysis UI is deliberately not part of this project. It can
be added later without changing the source and wiki contracts.

## Quick Start

Requirements:

- Docker Engine with Docker Compose v2
- An existing external proxy network, named `webproxy` by default
- An OpenCode-supported model provider

Create the proxy network once if your reverse proxy does not create it:

```bash
docker network create webproxy
```

Configure and start one instance:

```bash
cp .env.example .env
# Edit the URLs, DATA_ROOT, and OPENCODE_SERVER_PASSWORD at minimum.
docker compose config --quiet
docker compose up -d
```

OpenCode provider credentials and session state are persisted below
`${DATA_ROOT}`. Complete provider authentication through the OpenCode UI after
the first start.

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
COMPOSE_PROFILES=nextcloud,paperless
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
│   ├── nextcloud/
│   └── paperless/
├── wiki/                 # Independent Git repository and SilverBullet space
├── quarantine/           # Content-free Paperless error reports
├── session-exports/
├── opencode-config/
├── opencode-share/
└── opencode-state/
```

Source directories are read-only inside OpenCode. Generated knowledge is written
only to `wiki/`, and every successful write operation ends in a focused
Conventional Commit.

## Development

Run all local checks:

```bash
python3 -m unittest discover -s tests -v
(cd paperless-ingest && python3 -m unittest discover -s tests -v)
(cd session-export && python3 -m unittest discover -s tests -v)
sh -n config/init.sh
sh -n session-export/export-session.sh
node --check config/tools/wiki_ingest_status.js
docker compose --env-file .env.example config --quiet
```

GitHub Actions additionally builds every project image. Releases publish
multi-architecture images to GHCR. Dependabot patch/minor updates and GitHub
Actions updates are approved and squash-merged only after all protected CI checks
pass. Major runtime updates remain manual. See
[CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow.

## Security Notes

- Never commit `.env`, Paperless tokens, redaction lists, source material, wiki
  content, or exported sessions.
- Paperless redaction is an explicit deny-list, not general anonymization.
  Unknown values and OCR variants can remain in the output. Review samples before
  sending material to an external model provider.
- Nextcloud material is not redacted.
- `raw-files` serves all configured sources and should only be enabled behind a
  trusted, authenticated proxy.
- The OpenCode configuration blocks source edits and direct `.git` edits, but it
  is not a substitute for host-level network isolation and least-privilege model
  credentials.

Report vulnerabilities according to [SECURITY.md](SECURITY.md).

## Open Points

- Perform the first real migration and soak test of both existing installations.
- Publish the first tagged release and verify the generated `linux/amd64` and
  `linux/arm64` images on both hosts.
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
- Add revision-aware ingestion for changed Nextcloud files.
- Provide optional source adapters for local directories, S3, or other WebDAV
  services.
- Add backup/restore verification and migration tooling for the wiki Git history.
- Add an optional, separately tested analysis UI in a future release.
- Add OpenTelemetry-compatible health and operational metrics.

## License

[MIT](LICENSE)
