# Repository Guidelines

This repository provisions a self-hosted OpenCode wiki stack. Follow
`CONTRIBUTING.md`, `README.md`, and `docs/` for architecture and data layout.

## Migrations

This project is pre-1.0.0 and carries no data migrations: do not add new
migration code, migration documentation, or legacy-path fallbacks (aliases,
renames, or automatic data-layout moves) until the first stable release 1.0.0.
Installations upgrade by reorganizing data manually per the release notes.

## Validation

Before opening a pull request, run the validation commands documented in
`README.md` and include behavior changes, security implications, migration
impact, and test evidence in the pull request description.
