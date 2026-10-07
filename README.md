# Karpathy Wiki

A self-hosted Markdown knowledge base inspired by [Andrej Karpathy's LLM wiki
pattern](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f).
OpenCode turns explicitly selected source material into linked Markdown pages,
OpenChamber provides the chat interface, and SilverBullet provides the read-only
wiki browser. Each installation keeps its
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
OPENCHAMBER_PUBLIC_URL=https://chat.example.com
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
karpathy-wiki-openchamber:3000  # OPENCHAMBER_PUBLIC_URL
```

Open the chat URL, sign in with `OPENCHAMBER_UI_PASSWORD`, connect your model
provider in Settings, and select a model. OpenCode runs privately behind
OpenChamber; no browser pairing or backend password forwarding is needed.
Then ask it to ingest a source. For a first test with the `webdav`
provider: enable the `webdav` profile (`COMPOSE_PROFILES=webdav` in `.env`,
then `./karpathy-wiki.sh up -d`), configure WebDAV access and redactions (see
[configuration](docs/configuration.md#secrets)), and create a `notes.md` file
(UTF-8 text) inside the WebDAV folder configured via `WEBDAV_PATH`.
The `webdav-ingest` service stages the file through the provider flow; never
place files directly into `data/sources/webdav/`, which only carries the
provider's `manifest.json` and its published sanitized copies. Then ask
OpenCode to import `/knowledge/sources/webdav`. Generated pages are written to
`wiki/` and can be read at `WIKI_PUBLIC_URL`.

## Ingestion reports

`/ingest-new` processes all new and changed sources sequentially, with one
commit per source, with one fresh worker session per source and a documented
soft working-context target. Estimated overshoots warn rather than abort; the
model's actual context limit still applies. Workers propose changes in private drafts; code validates
and publishes complete transactions, generates index/log entries, and reconciles
interrupted commit/journal boundaries without duplicate publication.
Every processed source leaves a durable result record in a private
journal below `${DATA_ROOT}/incoming/ingest-journal`, and the complete per-source
report is written there as a file: source path, commit hash, changed wiki
pages, concrete thematic findings with evidence locations and changes from the
previous wiki, contradictions or open questions, and justified omissions and
extraction limits. After ingestion, the main session links the complete private report and
gives a short summary: overall status, unfinished sources with their blockers,
record count, run ID, and durable report path. Details stay in the file rather
than growing chat context; they are read only on request. Local link support
depends on the client; the operator can also open the stated path. Blocked,
paused, and zero-source runs use the same contract; report-creation failures are
explicitly flagged separately from ingestion status. See
[ingestion reports](docs/ingest-reports.md) for the budget model, resume
behavior, and validation.
Ingestion checks local Git changes before planning and validates source metadata
and committed contents before recording success. Blocked runs stop on the first
error and offer confirmed repair or a scoped rollback of the failed source's
uncommitted drafts before skipping it and continuing with other sources.
Successful source commits remain intact. See [stabilization acceptance](docs/ingest-stabilization-acceptance.md)
for the opt-in synthetic real-model/restart test; no production data is mounted.

## Analyses

Scientific analyses (`/analysis`) can be saved from the same conversation with
`/analysis-save` (no arguments required): the analysis is added to the wiki index
and log as a linked page at `WIKI_PUBLIC_URL/analyses/<slug>`
and a print-optimized HTML view at `WIKI_PUBLIC_URL/.fs/assets/analyses/<slug>.html`,
from which the browser's print dialog produces a shareable PDF. See
[analysis export](docs/analysis-export.md).

## Interactive gap review

Run `/gap-review` in OpenCode to review unsupported claims, contradictions,
missing information, and stale syntheses. Answer or defer its numbered
questions across turns. Only after you confirm the proposed answers does
OpenCode save a Markdown Q&A draft to the local answer inbox; that local save
works without the `answers` profile. Enable the profile for publication: the
provider locally redacts and publishes the draft as a tracked source, which
the normal wiki ingest flow uses to update the wiki. See
[gap review](docs/gap-review.md) for setup and the submission boundary.

## Profiles

Set `COMPOSE_PROFILES` in `.env`; profiles can be enabled independently:

| Profile | Purpose |
| --- | --- |
| `webdav` | Mirror and locally redact a selected WebDAV folder |
| `audio` | Transcribe WebDAV recordings and publish redacted transcripts (local by default, opt-in hosted backend) |
| `answers` | Publish confirmed Q&A drafts from the local answer inbox |
| `paperless` | Export tagged OCR text and redact configured personal data |
| `raw-files` | Expose source files to a trusted reverse proxy |

See [configuration](docs/configuration.md),
[audio ingestion](docs/audio.md), and
[Paperless ingestion](docs/paperless.md) for profile-specific setup and
production paths. See [architecture](docs/architecture.md)
for service boundaries and [data layout](docs/data-layout.md) for the persistent
folder structure.

See [chat deployment](docs/chat.md) for authentication, proxy setup, supported
workflows, and access boundaries.

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
`compose.override.yaml` and survive updates. See [backup and restore](docs/backup-restore.md)
for a consistent backup and a separate-instance restore check.

## Security

- Never commit `.env`, tokens, redaction lists, source material, wiki content, or sessions.
- Paperless redaction is an explicit deny-list, not general anonymization. Review output before sending it to an external model provider.
- WebDAV Markdown and HTML are locally redacted using the configured deny-list before publication to `sources/webdav`; unknown sensitive values may remain. HTML keeps its original format and active content: treat it as untrusted source text, not safe-to-render pages. Review redacted output before sending it to a model provider.
- Put OpenChamber and `raw-files` behind a trusted HTTPS reverse proxy; keep OpenCode private and do not publish container ports.
- The OpenCode configuration is not a substitute for host-level network isolation or least-privilege model credentials.

Report vulnerabilities according to [SECURITY.md](SECURITY.md).

## Roadmap

Work is planned in [GitHub roadmap issue #150](https://github.com/mstroppel/karpathy-wiki/issues/150).
Milestones define release commitments, priority labels define execution order,
and `roadmap:post-1.0` identifies the uncommitted post-1.0 backlog.

## Development

Run the same checks CI runs:

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pip install -e ingest/core -e ingest/webdav -e ingest/paperless -e ingest/audio -e ingest/speech
npm ci
scripts/install-shellcheck.sh  # add ~/.local/bin to PATH if needed; Linux x86_64
scripts/lint.sh
scripts/test-python.sh
node --test tests/test_wiki_ingest_status.mjs tests/test_contract_fixtures.mjs tests/test_wiki_ingest_journal.mjs tests/test_ingest_context_budget.mjs tests/test_wiki_ingest_transaction.mjs tests/test_answer_intake.mjs
node --test tests/test_chat_bootstrap.mjs
docker build --target opencode -t kw-opencode:integration -f opencode/Dockerfile .
docker build --target openchamber -t kw-openchamber:integration -f opencode/Dockerfile .
sh tests/integration/chat.sh  # authentication, discovery, native SSE and restart persistence
tests/integration/run.sh  # requires Docker; disposable Compose stack test
```

The integration runner builds local images and starts a temporary Compose
project with a disposable WebDAV upstream. It verifies redaction, manifest
publication, the status scanner's `new → current → outdated → current` path,
the local confirmed-answer inbox and provider, and restart/failure health
behavior. A deterministic test page exercises the
source-to-wiki contract; model-driven wiki publication and the future
publisher/worker pipeline are not part of this suite yet. It also checks that
one-shot Paperless fails against an unreachable disposable endpoint. The
runner removes its project, network, and data on exit.

`scripts/lint.sh` performs the formatting, linting, and type checks: ruff
(format, lint) and mypy for Python, oxfmt and ESLint for the repository
JavaScript, ShellCheck plus `sh -n` for shell scripts, and `node --check` for
the shipped JavaScript. Tool versions are pinned: Python tooling in
`requirements-dev.txt`, JavaScript tooling in `package-lock.json`, and
ShellCheck 0.11.0 in `scripts/install-shellcheck.sh` (verified SHA-256). The
Python test script requires at least 80% aggregate branch-aware coverage and
writes `coverage.xml`, which CI uploads as an artifact. Formatting applies
to JavaScript, JSON, and TOML, and intentionally excludes Markdown, YAML
workflows, and Compose files.

Third-party tool versions are centralized in `docker-bake.hcl` (rclone) and
the Dockerfiles (OpenCode, whose npm tarball download is verified against
pinned sha512 checksums and whose version is bumped automatically by a
scheduled workflow when a new OpenCode release is published); dependency
metadata is validated by `tests/test_dependencies.py`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the contribution workflow.

## License

[MIT](LICENSE)
