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

To install the latest pre-release instead of the latest stable release, set
`KARPATHY_WIKI_CHANNEL=pre`. The default channel is `stable`:

```bash
curl -fsSL https://raw.githubusercontent.com/mstroppel/karpathy-wiki/main/install.sh \
  | KARPATHY_WIKI_CHANNEL=pre sh
```

Edit `.env` with at least:

```env
WIKI_NAME=My Wiki
WIKI_PUBLIC_URL=https://wiki.example.com
OPENCODE_PUBLIC_URL=https://chat.example.com
DATA_ROOT=./data
COMPOSE_PROFILES=
```

Keep `PUID=1000` and `PGID=1000` only when they match the host user that should
own the wiki files.

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

Sign in to `OPENCODE_PUBLIC_URL`, connect a model provider, and
ask OpenCode to ingest a source. For a first test without an adapter:

```bash
cp /path/to/document.pdf ./data/sources/webdav/
```

Then ask OpenCode to import `/knowledge/sources/webdav/document.pdf`. Generated
pages are written to `wiki/` and can be read at `WIKI_PUBLIC_URL`.

## Analyses

Scientific analyses (`/analyse`) can be saved into the wiki with
`/analyse-save`: the analysis becomes a linked page at `WIKI_PUBLIC_URL/analyses/<slug>`
and a print-optimized HTML view at `WIKI_PUBLIC_URL/.fs/assets/analyses/<slug>.html`,
from which the browser's print dialog produces a shareable PDF. See
[analysis export](docs/analysis-export.md).

## Profiles

Set `COMPOSE_PROFILES` in `.env`; profiles can be enabled independently:

| Profile | Purpose |
| --- | --- |
| `webdav` | Mirror and locally redact a selected WebDAV folder |
| `paperless` | Export tagged OCR text and redact configured personal data |
| `raw-files` | Expose source files to a trusted reverse proxy |

See [configuration](docs/configuration.md) and
[Paperless ingestion](docs/paperless.md) for profile-specific setup and
production paths. See [architecture](docs/architecture.md)
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

- Never commit `.env`, tokens, redaction lists, source material, wiki content, or sessions.
- Paperless redaction is an explicit deny-list, not general anonymization. Review output before sending it to an external model provider.
- WebDAV material is not redacted.
- Put OpenCode and `raw-files` behind a trusted, authenticated reverse proxy; do not expose them directly to the internet.
- The OpenCode configuration is not a substitute for host-level network isolation or least-privilege model credentials.

Report vulnerabilities according to [SECURITY.md](SECURITY.md).

## Development

Run the same checks CI runs:

```bash
python3 -m pip install -r requirements-dev.txt
npm install
scripts/lint.sh
python3 -m unittest discover -s tests -v
node --test tests/test_wiki_ingest_status.mjs
(cd ingest && python3 -m unittest discover -s tests -v)
tests/integration/run.sh  # requires Docker; disposable Compose stack test
```

`scripts/lint.sh` performs the formatting, linting, and type checks: ruff
(format, lint) and mypy for Python, Prettier and ESLint for the repository
JavaScript, ShellCheck plus `sh -n` for shell scripts, and `node --check` for
the shipped JavaScript. Tool versions are pinned: Python tooling in
`requirements-dev.txt`, JavaScript tooling in `package-lock.json`, ShellCheck
`0.11.0` (the version CI installs). Formatting intentionally excludes
Markdown, YAML workflows, and Compose files.

Third-party tool versions are centralized in `docker-bake.hcl` (rclone) and
the Dockerfiles (OpenCode, whose npm tarball download is verified against
pinned sha512 checksums); dependency metadata is validated by
`tests/test_dependencies.py`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow.

## License

[MIT](LICENSE)
