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
- An AMD64/x86-64 host (published images do not support ARM64)
- Python 3.11+ (launcher), GNU tar (safe updates and rollback)
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
required by your security policy. The installer checks for host Python 3.11+
but does not install host packages; install Python yourself if the check fails.
To install a specific release:

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

`/ingest-new` makes one tool call that processes all selected new/changed sources
sequentially: one fresh worker and Git commit per source. Unusable sources are
reported and skipped; formatting and report-storage problems do not stop the run.
Sources and provenance are verified before publication. No scheduler, second
publisher or transaction protocol is involved; automatic ingestion is post-1.0 (#157).

Private reports under `${DATA_ROOT}/incoming/ingest-journal/batches/<id>` contain
commits, changed pages, findings, contradictions and extraction limits. Chat shows
each source's filename/email subject, short title, status and a 1–5 sentence summary
of changes and uncertainties (first ten sources; all entries in the private report),
links the report and summarizes remaining work. See [ingestion reports](docs/ingest-reports.md).

Readers can see intermediate edits. Other writers must stay idle during ingest.
Uncertain live changes stop for manual inspection; nothing is automatically reset.
The optional [reader mode](docs/runtime-write-isolation.md) disables **all** runtime
authoring, including ingest and analysis/answer saves. Writable manual operation
remains the default.

## Analyses

Scientific analyses (`/analysis`) can be saved from the same conversation with
`/analysis-save` (no arguments required): the analysis is added to the wiki index
and log as a linked page at `WIKI_PUBLIC_URL/analyses/<slug>`
and a print-optimized HTML view at `WIKI_PUBLIC_URL/.fs/assets/analyses/<slug>.html`,
from which the browser's print dialog produces a shareable PDF.
See [analysis export](docs/analysis-export.md).

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
| `email` | Trust an IMAP folder, privately archive eligible mail, delete it on the server, publish redacted body text |
| `raw-files` | Expose source files to a trusted reverse proxy |

See [configuration](docs/configuration.md),
[audio ingestion](docs/audio.md), and
[Paperless ingestion](docs/paperless.md), and [email ingestion](docs/email.md) for profile-specific setup and
production paths. See [architecture](docs/architecture.md)
for service boundaries and [data layout](docs/data-layout.md) for the persistent
folder structure.

See [chat deployment](docs/chat.md) for authentication, proxy setup, supported
workflows, and access boundaries.

## Updates

The launcher downloads and caches the Compose file matching the pinned release
in `.env`. Updates also install both host launcher files from that same release.
After the [one-time launcher bootstrap](docs/backup-restore.md#launcher-bootstrap-and-recovery),
update to the latest release with:

```bash
./karpathy-wiki.sh update
```

To pin a release:

```bash
./karpathy-wiki.sh update 0.2.0
```

Updates report each phase on stderr. Add `-v` or `--verbose` **after `update`**,
before or after the version, for safe operation context and elapsed times:
`./karpathy-wiki.sh update --verbose 0.2.0`. This also combines with `--no-backup`.
Verbose mode never prints raw subprocess output, configuration or environment values.

By default, launcher updates stop the instance for a verified full backup of
`DATA_ROOT`, configuration, external file secrets, and launcher files **before**
changing the version. Backup failure blocks the update; update failure never
automatically resets data. Local Compose changes belong in `compose.override.yaml`.
Use private local storage outside `DATA_ROOT` (`KARPATHY_WIKI_BACKUP_DIR`, default
`./backups`), preferably on an encrypted disk. Backups contain plaintext secrets.
Direct Compose calls bypass this guarantee. Review release notes before upgrading.

**Image contents are omitted by default** to avoid exporting and verifying large
Docker archives. Exact local image IDs and available registry digests are recorded.
Rollback needs those images still cached locally, or registry access to fetch the
saved digests; unavailable images block rollback **before stopping writers**.
Locally built/imported images without registry digests require the local cache or
a separately retained image archive. Use `./karpathy-wiki.sh update [version]
--backup-images` for a self-contained backup including installed image contents.
This cannot be combined with `--no-backup`.

To explicitly skip the update snapshot, use `./karpathy-wiki.sh update [version]
--no-backup` (omit `[version]` to use the configured release channel). **This creates
no new rollback point.** Writers still stop and startup health is checked; failures
require manual recovery or an existing backup. Rollback rescue backups cannot be skipped.

```bash
./karpathy-wiki.sh backup list
./karpathy-wiki.sh rollback <backup-id>
```

Rollback requires explicit confirmation and a separate current-state rescue
backup (always including current images); it restores software, configuration and data together, even after later
updates. **All changes since the chosen snapshot are lost in the restored state.**
Update backups stay at least 30 days and the latest three are always retained;
failed-update and rescue backups are never automatically deleted. See
[backup and restore](docs/backup-restore.md) for requirements, custom-mount and
proxy boundaries, recovery, and a separate-instance restore check.

## Security

- Never commit `.env`, tokens, redaction lists, source material, wiki content, or sessions.
- Paperless redaction is an explicit deny-list, not general anonymization. Review output before sending it to an external model provider.
- WebDAV Markdown and HTML are locally redacted using the configured deny-list before publication to `sources/webdav`; unknown sensitive values may remain. HTML keeps its original format and active content: treat it as untrusted source text, not safe-to-render pages. Review redacted output before sending it to a model provider.
- WebDAV also extracts `.xlsx` tables locally before redaction; originals stay private. Formula caches may be missing or stale: recalculate and save in Excel before upload. See [Excel through WebDAV](docs/webdav-xlsx.md) for visibility, formats and resource limits.
- Put OpenChamber and `raw-files` behind a trusted HTTPS reverse proxy; keep OpenCode private and do not publish container ports.
- The OpenCode configuration is not a substitute for host-level network isolation or least-privilege model credentials.

Report vulnerabilities according to [SECURITY.md](SECURITY.md).

## Roadmap

Work is planned in [GitHub roadmap issue #150](https://github.com/mstroppel/karpathy-wiki/issues/150).
Milestones define release commitments, priority labels define execution order,
and `roadmap:post-1.0` identifies the uncommitted post-1.0 backlog.

## Development

CI builds AMD64 images with per-image GitHub Actions BuildKit caches. Six
integration suites run in isolated parallel jobs; the `Compose integration`
check aggregates their results for branch protection. Unit tests, lint and image
builds run alongside them. On `main`, only after all gates pass, the reusable
pre-release workflow promotes that run's image digests to release tags without
repeating tests or builds. PR preview images retain their `pr-<number>` tags.
The pre-release workflow is called by CI, not dispatched separately.

Local Bake builds do not need Actions credentials. To load images before running
individual integration suites, use e.g.
`sh scripts/build-integration-images.sh opencode openchamber`; the Compose/email
runners accept `SKIP_IMAGE_BUILD=1` when their required images are already loaded.

Run the same checks CI runs:

```bash
python3 -m pip install -r requirements-dev.txt
python3 -m pip install --require-hashes --only-binary=:all: -r ingest/webdav/requirements.txt
python3 -m pip install -e ingest/core -e ingest/webdav -e ingest/paperless -e ingest/audio -e ingest/speech -e ingest/email
npm ci
scripts/install-shellcheck.sh  # add ~/.local/bin to PATH if needed; Linux x86_64
scripts/lint.sh
scripts/test-python.sh
npm test
docker build --target opencode -t kw-opencode:integration -f opencode/Dockerfile .
docker build --target openchamber -t kw-openchamber:integration -f opencode/Dockerfile .
sh tests/integration/analysis-images.sh  # installed PNG/SVG renderer, offline, synthetic fixtures
sh tests/integration/chat.sh  # authentication, discovery, native SSE and restart persistence
sh tests/integration/batch-ingest.sh  # synthetic model, private drafts, context stops, permissions and commits
sh tests/integration/runtime-write-isolation.sh  # pinned-runtime proof of the reader boundary
tests/integration/run.sh  # requires Docker; disposable Compose stack test
sh tests/integration/email.sh  # synthetic TLS IMAP, archive-before-delete, restart, replay
sh tests/integration/launcher-backup.sh  # real Compose update snapshots and delayed rollback
```

The integration runner builds local images and starts a temporary Compose
project with a disposable WebDAV upstream. It verifies redaction, manifest
publication, the status scanner's `new → current → outdated → current` path,
the local confirmed-answer inbox and provider, and restart/failure health
behavior. A deterministic test page exercises the
source-to-wiki contract; the separate synthetic-model ingest test covers worker
publication. It also checks that
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
