# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

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

### Changed

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
