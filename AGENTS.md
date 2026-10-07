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

## Agent skills

Keep skills concise and actionable: explicit triggers, ordered steps where
sequence matters, and verifiable completion criteria. Put optional detail behind
task-specific links; preserve required behavior and safety boundaries when pruning.
When creating or editing skills or agent instructions, read
`.opencode/skills/writing-for-agents/SKILL.md`.
See `docs/agents/skills.md` for skill locations, upstream provenance, and candidates.

### Issue tracker

Issues and specs live in GitHub Issues for `mstroppel/karpathy-wiki`. See
`docs/agents/issue-tracker.md`.

### Domain docs

Single-context layout: root `GLOSSARY.md` and `docs/adr/`. See
`docs/agents/domain.md`.
