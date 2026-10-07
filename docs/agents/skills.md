# Repository skills

## Locations

- `.opencode/skills/`: contributor workflows discovered by OpenCode in this checkout.
- `.github/skills/`: GitHub Copilot review instructions; see
  [CONTRIBUTING.md](../../CONTRIBUTING.md#code-review).
- `config/skills/`: shipped wiki workflows, installed into the deployed stack.

The contributor authoring skill is committed with the repository; cloning is
sufficient. For authoring, read
[`writing-for-agents`](../../.opencode/skills/writing-for-agents/SKILL.md).
The root `AGENTS.md` provides the same pointer for agents without automatic
discovery of `.opencode/skills/`.

## Upstream provenance

Issue [#174](https://github.com/mstroppel/karpathy-wiki/issues/174) proposed
`npx skills add https://github.com/mattpocock/skills --skill write-a-skill`.
Upstream removed `write-a-skill`, replaced it with `writing-great-skills`, and
then renamed that skill to `writing-for-agents`; see the
[upstream changelog](https://github.com/mattpocock/skills/blob/6fd947921b935b7e1e69293a200400f0fdd5c15f/CHANGELOG.md).

This repository carries a concise local adaptation of
[`writing-for-agents`](https://github.com/mattpocock/skills/blob/6fd947921b935b7e1e69293a200400f0fdd5c15f/skills/productivity/writing-for-agents/SKILL.md)
and its sibling `SKILL-MECHANICS.md`, pinned to upstream commit
`6fd947921b935b7e1e69293a200400f0fdd5c15f`. The upstream MIT license is retained
beside the skill. The adaptation turns the reference into an authoring workflow,
uses this repository's locations, and keeps packaging guidance in the short
main file. Updates should be reviewed against that revision and the local
authoring rules rather than overwriting the adaptation with a bulk install.

## Installation and updates

`npx skills add` installs upstream skill files; it does not make them npm
dependencies. The CLI supports `skills update`, but Dependabot's
[supported ecosystems](https://docs.github.com/en/code-security/dependabot/ecosystems-supported-by-dependabot/supported-ecosystems-and-repositories)
do not include the skills lockfile (`skills-lock.json`). Adding the `skills`
CLI to `package.json` would let Dependabot update the installer, not the
installed instructions; see the [CLI documentation](https://github.com/vercel-labs/skills#other-commands).

Keep this local adaptation committed and review updates manually: compare
upstream changes since the pinned revision, incorporate relevant guidance while
preserving repository-specific links and safety rules, update the provenance
revision and license if needed, then check links and walk through the skill.
For future unmodified upstream skills, consider CLI-managed installs with a
separate scheduled update PR workflow. Review instruction changes before merging;
do not auto-merge them solely because installer or runtime tests pass.

## Candidates for later work

Add a skill when a recurring task needs instructions beyond existing docs.
These candidates are proposals, not installed dependencies:

| Candidate | Source | Useful when | Acceptance check before adding |
| --- | --- | --- | --- |
| `diagnosing-bugs` | `mattpocock/skills`, `skills/engineering/diagnosing-bugs` | Investigating provider or orchestration failures | Reproduce with synthetic fixtures, isolate a cause, verify a fix, and keep diagnostics content-free. |
| `tdd` | `mattpocock/skills`, `skills/engineering/tdd` | Changing ingest contracts or transaction behavior | Demonstrate a failing behavioral test and a passing fix using this repository's Python or Node tooling. |
| `provider-contract-review` | Write locally | Adding or changing a source provider | Walk through manifest/status contracts, atomic publication, revocation, and content-free errors; link the canonical contracts and existing review rules. |

Prioritize `diagnosing-bugs` if repeat investigations expose a gap. Extend the
existing code-review skill for provider checks unless that workflow needs its
own invocation. Review upstream instructions and license terms before adoption.
