# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Automatic pre-releases on every merge to `main` and a `pre` install/update
  channel to opt in to them.
- The installer creates a `.gitignore` that ignores the `.cache` directory.
- The `/analyse-save` command and `wiki-analysis-save` skill store a finished
  analysis as a wiki page under `analyses/` with a print-optimized HTML view
  under `assets/analyses/` that the browser can print or save as a PDF.

### Changed

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
