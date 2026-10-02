# Domain Docs

This repository uses a single-context domain documentation layout.

## Before exploring, read these

- `GLOSSARY.md` at the repository root.
- Relevant decisions in `docs/adr/`.

If these do not exist, proceed silently. Do not flag their absence or
suggest creating them upfront. `/domain-modeling`, reached through
`/grill-with-docs` and `/improve-codebase-architecture`, creates them lazily
when terms or decisions are resolved.

Continue following `CONTRIBUTING.md`, `README.md`, and the existing
architecture and data-layout documentation in `docs/`.

## File structure

- `GLOSSARY.md`: shared domain vocabulary.
- `docs/adr/`: architecture decision records.

## Use the glossary's vocabulary

Use glossary terms in issue titles, refactor proposals, hypotheses, and
test names. Avoid synonyms the glossary explicitly rejects.

If a needed concept is absent, reconsider whether it belongs to the
project's vocabulary or note the gap for `/domain-modeling`.

## Flag ADR conflicts

Surface contradictions with existing ADRs explicitly rather than silently
overriding them, for example:

> Contradicts ADR-0007, but worth reopening because…
