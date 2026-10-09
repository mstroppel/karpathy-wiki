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
| `OPENCHAMBER_PUBLIC_URL` | Browser-visible OpenChamber chat URL (proxy documentation; not read by the service) |
| `OPENCHAMBER_UI_PASSWORD` | Separate strong password for browser sign-in |
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
secret. Keep it private: only internal API clients use this credential.
Fresh installs also generate a separate `OPENCHAMBER_UI_PASSWORD`. Existing or
manual installations must set that value themselves before starting this release.
Sign in to OpenChamber with the UI password; no OpenCode browser pairing is needed.

## Profiles

Select optional services with a comma-separated value:

```env
COMPOSE_PROFILES=webdav,audio,paperless,answers,raw-files
```

An installation can run without source providers and receive files through a
separate trusted process. When the `paperless` profile is active, initialization
automatically installs the corresponding wiki rules and directories.
The `answers` profile starts the local answer provider for
[`/gap-review`](gap-review.md) and uses the shared `REDACTIONS_FILE`.

Ingest tracking discovers sources from directories below `/knowledge/sources`.
A directory is compared against its provider manifest (`manifest.json`), which
every ingest cycle writes into the sanitized source tree; directories without a
valid, supported manifest are reported as invalid. Adding another source type
therefore requires only its source directory, its manifest-producing ingest
module, and source-specific ingest validation — not a change to `/ingest-new`,
its skill, the generic status tool, or the OpenCode image.

The manifest lists each published source with these fields (see
[`contracts/provider-manifest`](../contracts/provider-manifest/v1/contract.json)):

| Field | Purpose |
| --- | --- |
| `source_key` | Stable identity within the provider |
| `source_path` | Sanitized source file, relative to the source directory |
| `source_revision` | Lowercase SHA-256 revision |
| `wiki_path` | Target page, relative to the wiki source directory |
| `frontmatter` | Trusted metadata including the same `source_revision` |
| `claim` | Frontmatter values identifying the source on its wiki page |

The generic tool validates this contract and converts load, execution, and
contract errors into `invalid` results. `frontmatter` must contain only
JSON-compatible values, and `wiki_path` must be unique across every discovered
source; collisions are reported as `conflict`. Provider manifests are data:
nothing in a source directory is ever executed.

Status details are returned in bounded pages (10 entries per status by default,
maximum 25), including diagnostics. Use `adapter` by itself to list one source
adapter, and `offset` and `limit` to page through a stable list. Global counts
remain present on adapter-filtered pages. `status_state` filters to one status
bucket so a large diagnostic cannot block retrieval of other status lists. When
ingesting a batch, restart at offset 0 after processing each page because
completed sources leave the pending list. The serialized response is capped at
12 KiB; page size is reduced when
needed. If the page still exceeds the cap at one entry per status,
`page.blocked` is returned. `oversized_records` identifies large pending
records or diagnostics; retrieve each JSON representation in bounded chunks
with `adapter`, `record_chunk_state`, and `record_chunk_offset`, plus
`source_key` or `record_chunk_index`, then append chunks by offset before
parsing. If no record is listed, narrow the query to one adapter.

## Ingestion Runs

`WIKI_RUNTIME_READ_ONLY` defaults to `false` (manual authoring). Set it to `true`
for the [kernel-enforced reader topology](runtime-write-isolation.md); this
disables all wiki/journal/answer writes and is not an auto-ingest switch.
Recreate OpenCode to apply mount changes. A separate trusted publisher is not
implemented yet.

`/ingest-new` orchestrates bulk wiki ingestion in bounded batches. The batch
planner sizes each worker session by an estimated token budget, not by a bare
source count, and stores per-source results in the private journal mounted at
`/knowledge/incoming/ingest-journal` (see [data layout](data-layout.md)). Tune the
budget for the configured model:

| Variable | Default | Description |
| --- | --- | --- |
| `WIKI_INGEST_BATCH_BUDGET_TOKENS` | `32000` | Estimated working context per worker session |
| `WIKI_INGEST_BATCH_MAX_SOURCES` | `1` | Upper cap; one fresh worker/source is always enforced |
| `WIKI_INGEST_RUN_MAX_BATCHES` | `12` | Batches per run before a clean rollover (`0` = unlimited) |

Token values are documented estimates, not measured model tokens; see
[ingestion reports](ingest-reports.md) for the budget model, the report
contract, resume and rollover behavior, and the opt-in real-model acceptance
run.

## Secrets

WebDAV uses rclone's obscured password format. Obscuring is not encryption;
protect the environment file as a credential. Any rclone release produces a
compatible obscured value:

```bash
docker run --rm rclone/rclone:latest obscure 'WEBDAV_PASSWORD'
```

Set `WEBDAV_URL`, `WEBDAV_VENDOR`, `WEBDAV_USERNAME`,
`WEBDAV_PASSWORD_OBSCURED`, `WEBDAV_PATH`, and `WEBDAV_SYNC_INTERVAL` for the
source provider. `WEBDAV_VENDOR` defaults to `nextcloud`; rclone also supports
other WebDAV implementations.

WebDAV files are synchronized into a private staging directory first. The
`webdav-ingest` service processes files whose name ends in `.md`, `.html`, or
`.htm` (case-insensitively), applies `REDACTIONS_FILE` locally, and publishes
UTF-8 Markdown and HTML files to `sources/webdav`. Other formats are ignored.
HTML stays HTML: original paths, tags, attributes, formatting, and line endings
are preserved except for targeted replacements. Matching covers literal source,
context-aware decoded HTML entities (including attribute values), and text split across inline
tags; block boundaries provide whitespace. A split match inserts its placeholder
at the first matched source position and removes the remaining matched text,
leaving intervening tags intact. Comments and script/style source are also
checked. Output is checked again with the same matching views before publication.
Overlapping matches across views use the shared anonymizer's configured rule
precedence (longest literal values first, then phone rules), not view order.
Ambiguous no-semicolon named references stay literal in attributes; script/style
raw text is matched literally, without decoding HTML entities.
Matches touching structural markup are rejected rather than changing tag syntax.
This also covers configured values inside declaration or processing-instruction
data: `<!DOCTYPE` and `<?...?>` bodies (including `<![CDATA[...]]>` sections,
whose `]]>` terminator must survive) are protected as markup, so a value there
quarantines the file fail-closed instead of being redacted. Unterminated HTML
constructs consume the input to the end; a value inside one is likewise
rejected. Files that cannot be decoded or fail privacy validation produce
content-free
reports in `quarantine/webdav`; the previous generation remains active.

**Redacted HTML is untrusted source text, not safe-to-render HTML.** Scripts,
event handlers, links, and embedded resources are not removed or executed, and
linked resources are not downloaded. Do not serve these files as trusted pages.
Matching does not interpret JavaScript/CSS escapes, URL encoding, or dynamically
generated content; the deny-list is not a guarantee of complete anonymization.
Review output before sending it to an external model provider.

A failed upstream synchronization is retried on the next
`WEBDAV_SYNC_INTERVAL` and surfaced through the Compose healthcheck instead of
restarting the daemon.
After every successful cycle the provider manifest `sources/webdav/manifest.json`
is refreshed; the name is reserved there.

### Audio recordings

With the `audio` profile enabled, a second connector synchronizes a
WebDAV folder of recordings (`AUDIO_WEBDAV_PATH`, default `Recordings`) into
its own private snapshot, transcribes through the
[`audio-speech` worker](audio.md), applies the shared redactions (including
matches that span transcript segments), and publishes sanitized Markdown to
`sources/audio` as coherent generations. Raw audio, unredacted transcripts,
and the speech-result cache stay in private directories that are never
mounted into OpenCode. The default local backend sends no audio or
transcript to a cloud API; the opt-in hosted backend
(`SPEECH_BACKEND=mistral`) uploads the raw recording to the Mistral
speech-to-text service before redaction, with its own service limits,
per-minute costs, and data-handling terms.
See [audio ingest](audio.md) for all options, the source identity model,
and retention.

## Source Generations

WebDAV sources are published as coherent generations instead of file-by-file.
Every cycle synchronizes the upstream tree into a private staging directory,
anonymizes and validates every file with a freshly loaded redaction
configuration, and only then exposes the complete generation: the `current`
symlink inside `sources/webdav` is switched atomically and the provider
manifest is rewritten immediately afterwards. Manifest items refer to the
immutable generation they describe, so readers cannot combine a manifest from
one generation with files from another. Readers therefore never observe a
partially published synchronization; the last successful generation stays
active when rclone, decoding, redaction, validation, or publication fails, and
removed upstream files disappear only with the successfully published
replacement generation. The redactions file is reloaded every cycle, so
rotating it regenerates every applicable file on the next cycle.

The redaction fingerprint also includes a code-maintained algorithm version.
When a code change alters redaction behavior, bump
`REDACTION_ALGORITHM_VERSION` in `ingest/core/src/karpathy_wiki_ingest/shared.py`.
The next cycle then reprocesses every current redaction-enabled source, even
when its upstream content and redactions configuration are unchanged: WebDAV
regenerates its published files, and Paperless reprocesses selected documents.
Any future ingest provider that uses the shared anonymizer fingerprint will
also participate.

The published layout below `sources/webdav` is:

```text
sources/webdav/
├── manifest.json        # provider manifest of the active generation
├── current              # symlink to the active generation directory
└── generations/<id>/    # complete sanitized trees; only the active one is kept
```

Manifest items reference their sanitized file as
`generations/<id>/<relative path>`, while `wiki_path` and the `source_path`
frontmatter stay keyed by the stable relative upstream path, so wiki pages
never change their destination when a new generation is published. Each
generation directory records a
`.generation.json` metadata file with its creation time, the redaction
configuration fingerprint, and the upstream inventory hashes; it never
contains upstream content.

Retention keeps only the generation the manifest describes; staging
directories abandoned by interrupted cycles are discarded at the start of the
next cycle. To recover manually, delete every entry of
`sources/webdav/generations` except the directory `current` resolves to, then
restart the service. Upgrading installations with an older flat layout remove
the previous contents of `sources/webdav` once; the next cycle republishes
them as a generation. The names `manifest.json`, `current`, `generations`,
and `.generation.json` are reserved inside `sources/webdav`.

### Durable ingest state

The `webdav-ingest` service records every cycle in a durable, content-free
SQLite state store at `INGEST_STATE_PATH` (mounted at
`/data/state/ingest.sqlite3`). Each cycle becomes an accepted job keyed by the
upstream inventory hashes and the redaction configuration fingerprint, so:

- unchanged upstream content with unchanged redactions keeps the active
  generation instead of republishing it every interval;
- a restart neither loses accepted work nor executes an accepted cycle twice;
  leases of interrupted cycles expire and the cycle is recovered without a
  second publication;
- failed work backs off exponentially instead of retrying every interval; a
  job whose rejected input is unchanged becomes dead after repeated attempts
  and keeps the failure visible until the source content changes;
- the queue depth, oldest pending job age, and recorded generations are
  exposed through the Compose healthcheck's health record.

The store records identifiers, revisions, counts, and content-free error
strings only — never source content or credentials. When the store is
unavailable, the daemon logs the failure and publishes without state instead
of losing ingest availability; the health record then reports the failure.
Deleting the database resets only the bookkeeping: the next cycle re-records
the active generation and re-coordinates from there, and republishes a
generation when the published one no longer matches the upstream content.
See [data layout](data-layout.md) for the persistent location.

Paperless credentials use files rather than environment values. Set an absolute
path when possible:

```env
PAPERLESS_TOKEN_FILE=/private/personal-wiki/paperless-token
```

The hosted transcription API key uses the same file pattern:

```env
MISTRAL_API_KEY_FILE=/private/personal-wiki/mistral-api-key
```

It is mounted into the speech worker service alone; the audio provider,
OpenCode, logs, published metadata, and cache/worker fingerprints never see
it. Only the request header carries the key (see [audio ingest](audio.md)).
Compose needs this secret file to exist even in local-only mode, so the
unset default is the tracked, intentionally empty placeholder
`secrets/mistral-api-key.example`; hosted mode then fails content-free until
a real key file is configured. The `MISTRAL_API_KEY` environment variable is
read only when the worker runs outside Compose.

`REDACTIONS_FILE` is shared by all ingestion providers and should also use an
absolute path, for example `/private/personal-wiki/redactions.json`. Apply mode
`0600` to these files. Never place real values below the repository's `secrets/`
directory in a commit.

For structured `people` entries, the configured first and last names are redacted
even when they appear alone (for example, in a sign-off), as well as in full-name
variants, including concatenated forms such as `AntonHotz` and `HotzAnton`.
Matching is case-insensitive and uses word boundaries; leading and
paired trailing underscores used as Markdown emphasis delimiters are also
handled, without matching names inside underscore-separated identifiers. German
genitive (`Antons`) and English possessive (`Anton's` or `Anton’s`) first-name
forms are also covered. For first names ending in a sibilant letter such as `s`,
`x`, `z`, or `ß`, apostrophe-only forms such as `Felix'` and `Felix’` are covered.
A common first name elsewhere in a document is therefore also redacted. If two
entries use the same first name with different replacements, the redaction
configuration is rejected because a standalone name cannot identify which person
it refers to. If entries share a last name but use different replacements, the
standalone last name is replaced with `[PERSON]`; full-name variants retain their
configured replacements.

## Reverse Proxy

Compose creates these aliases on `WEBPROXY_NETWORK`:

```text
${STACK_ID}-silverbullet
${STACK_ID}-openchamber
${STACK_ID}-raw-files
```

The corresponding ports are `3000`, `3000`, and `8080`. Apply authentication and
source-network restrictions at the reverse proxy. Do not publish container ports
directly from Compose.

OpenChamber uses its own browser login. OpenCode stays on the private stack
network; do not proxy it or inject backend credentials into browser requests.
See [chat deployment](chat.md) for the Caddy example and SSE/WebSocket requirements.

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
