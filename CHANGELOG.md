# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- A durable, content-free SQLite state store for ingest (`karpathy_wiki_ingest.state`,
  mounted at `DATA_ROOT/state/ingest.sqlite3`): ingest jobs with state
  transitions, attempts, exponential backoff, and leases, the immutable source
  generations they published, and wiki publications with idempotency keys.
  WebDAV ingest cycles now run as accepted, idempotent jobs: unchanged
  upstream content with unchanged redactions keeps the active generation, a
  restart neither loses accepted work nor executes an accepted cycle twice,
  interrupted cycles are recovered through lease expiry, and failed work backs
  off instead of retrying every interval; accepted jobs whose upstream input
  has been replaced are recorded as superseded. Queue depth, oldest pending
  job age, and recorded generations are exposed through the health record.

- A versioned provider manifest (`contracts/provider-manifest/v1/contract.json`):
  every ingest cycle writes a `manifest.json` into its sanitized source root
  describing source keys, revisions, wiki destinations, frontmatter, claims,
  revocations, and content-free errors. The generic `wiki_ingest_status` tool
  consumes only the manifest and the wiki pages; all provider-specific
  JavaScript adapters were removed from the OpenCode image, so adding a
  provider never requires rebuilding it. Manifest validation is covered by
  shared conformance fixtures for Python and JavaScript, and the contract
  deprecation rules are documented.
- Ingest is split into installable distributions: the plugin-free core
  `karpathy-wiki-ingest` plus the plugin distributions
  `karpathy-wiki-ingest-webdav` and `karpathy-wiki-ingest-paperless`, each
  with its own pinned `pyproject.toml`. The WebDAV and Paperless images now
  contain only the core and their own plugin; the core image remains the base
  for third-party plugins.
- A versioned ingest status contract
  (`contracts/ingest-status/v1/contract.json`) for revisions, revocation
  lists, ID ranges, filenames, intervals, and status values, with shared JSON
  conformance fixtures that are executed by both the Python ingest tests and
  the JavaScript status scanner tests.
- A shared quality gate (`scripts/lint.sh`) with pinned tool versions:
  ruff formatting, linting, and mypy typing for Python, oxfmt and ESLint
  for repository JavaScript, and ShellCheck for all shell scripts. CI runs
  the same script; `tests/test_dependencies.py` validates dependency
  metadata.
- A repository-managed JavaScript manifest (`package.json`) and lockfile,
  covered by Dependabot, and a disposable Compose integration test
  (`tests/integration/run.sh`) that checks stack startup, restart, daemon
  health behavior, and one-shot failure exits in CI.
- The WebDAV ingest daemon handles termination signals, retries failed
  rclone synchronizations on the next interval instead of crashing, and
  writes the shared health record consumed by a new Compose healthcheck.
- Automatic pre-releases on every merge to `main` and a `pre` install/update
  channel to opt in to them.
- A scheduled workflow that opens a pull request bumping the pinned OpenCode
  version and its sha512 download checksums when the npm registry publishes
  a new OpenCode release.
- The installer creates a `.gitignore` that ignores the `.cache` directory.
- The `/analyse-save` command and `wiki-analysis-save` skill store a finished
  analysis as a wiki page under `analyses/` with a print-optimized HTML view
  under `assets/analyses/` that the browser can print or save as a PDF.
- The WebDAV ingest publishes each synchronization as a coherent sanitized
  generation: every cycle builds the complete tree in a private staging
  directory, validates every file with a freshly loaded redaction
  configuration, and only then switches the `current` symlink atomically and
  rewrites the provider manifest. A failed rclone, decoding, redaction,
  validation, or publication step keeps the last successful generation active,
  removed upstream files disappear only with the published replacement
  generation, and each generation records its upstream inventory hashes and
  redaction fingerprint in `.generation.json`. Retention keeps only the
  active generation; abandoned staging directories are discarded at the start
  of the next cycle, and recovery is documented.

### Changed

- The Paperless ingest plugin is split along responsibility boundaries into
  `config`, `client`, `documents`, `storage`, `ingestor`, and `cli` modules
  with the same public API, CLI behavior, and environment semantics; tests
  cover the extracted modules directly.
- The `wiki-ingest` skill preserves the validated HTTPS `paperless_url`
  frontmatter and renders it as a visible Paperless link on every Paperless
  source page; `wiki-lint` reports Paperless pages that lose it.

- `python -m karpathy_wiki_ingest` without a plugin name now runs the sole
  installed plugin instead of defaulting to Paperless; with zero or multiple
  plugins installed, a plugin name is required.
- The ingest images no longer contain the test suite; tests run in CI from
  the repository checkout against installed packages.
- Ingest wheel builds preinstall the hash-pinned setuptools build backend and
  run with `--no-build-isolation`; no index-resolved code is executed during
  the build, and each final image stage copies only its own plugin wheel so
  no image layer contains another plugin's wheel.
- Adapter status results are now validated strictly: unknown adapter result
  keys are rejected instead of silently discarded.
- The OpenCode image download is now verified against pinned sha512
  checksums, and the base images are pinned by digest; the rclone build
  stage is pinned by tag and digest via `docker-bake.hcl`.
- Remove all migration code and documentation from pre-1.0.0 releases; data
  migrations will be introduced with the first major release 1.0.0.
- Migrate OpenCode, its configuration and custom ingest-status tool to v2.

### Removed

- The session exporter (`session-export` Compose profile and image) and its
  documentation. Sharing finished analyses by wiki link and printable PDF view
  already covers the exporter's use cases, and analysis runs directly in
  OpenCode, which has session access. Existing installations can manually
  delete the unused `${DATA_ROOT}/exports/sessions` directory.

### Fixed

- OpenCode prompt execution failed immediately because version 1.18.30 shipped
  a compiled-binary module initialization regression.
- The `raw-files` container failed to start with
  `exec /usr/bin/caddy: operation not permitted` because `cap_drop: ALL`
  removed the file capability Caddy needs to execute.

## [0.1.3] - 2026-09-15

### Fixed

- Silence expected installer fallback 404.

## [0.1.2] - 2026-09-15

### Changed

- Unify ingest into a plugin-based core image.

## [0.1.1] - 2026-09-14

### Changed

- Derive installation identity from the installation directory.

## [0.1.0] - 2026-09-14

### Added

- Initial release.
