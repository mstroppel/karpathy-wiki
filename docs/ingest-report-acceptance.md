# Main-session report acceptance evidence

> Historical evidence for the previous inline-report contract. PR #163 now uses
> linked private reports with compact summaries, as explicitly requested by the
> user. The results below do not validate that new behavior; installation-based
> acceptance is pending user testing.

Opt-in execution on 2026-10-06 for PR #163, using **OpenCode 2.0.23**, Linux
amd64, and **`openai/gpt-6-luna#high`**. The command used the repository's exact
`ingest-new` prompt template with `wiki-ingest-orchestrator` as the primary agent
in an isolated `opencode run --standalone --auto --format json` session.

## Isolation and fixtures

Each scenario used a disposable container built from `opencode/Dockerfile`, its
own synthetic wiki Git repository, source directory, journal, and OpenCode
database. Production sources, wiki files, journals, and session databases were
not mounted. Only the selected provider credential was transferred directly to
the disposable runtime through `opencode auth export` / `auth import`; credential
exports were neither printed nor written to evidence files. Containers and their
credential databases were removed after execution. No private session exports or
source documents are included here.

- **Single:** one short synthetic manifest source, processed by one real worker.
  Git verifies exactly one source commit after the fixture's initial commit;
  its hash matches the journal, its source stayed unchanged, and final
  `new=0`, `outdated=0`.
- **Multipart/blocked:** preseeded durable synthetic records for 18 ingested
  results and one blocked result. Summaries contain distinct numbered
  measurements and units, with question and extraction-limit fields. This
  isolates report delivery; it does **not** claim 18 model-driven ingestions.
- **Paused:** one preseeded result and one pending source, with the run already
  at its batch cap. The model assembles the report with `operation: report`,
  displays all details, and keeps the run open with an explicit resume command.
- **Empty:** no sources or records; the model still displays the complete
  zero-source report.
- **Forced read failure:** eight preseeded results. A disposable-only journal
  executor throws on report reads after offset zero. The first chunk is fully
  displayed; the next read fails. The model separates completed ingestion from
  incomplete delivery and reports the first chunk's `next_offset` as the
  continuation point. No fault injection ships in the production plugin.

## Observed results

All five checks below passed against assistant text emitted in the **primary
session**, not tool output or worker messages. Successful reports were checked
against every report line, every detail block, and the final chunk's null offset.
The multipart code-block payloads were also concatenated and compared to the
complete durable report, ignoring presentation whitespace only. The failure case
checked the entire emitted first chunk, the actual tool error, and its recovery
offset.

| Scenario | Effective records | Report bytes | Read calls | Main-session text messages | Peak input + cached input tokens | Total main-session output tokens |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Empty | 0 | 507 | 1 | 1 | 7,219 | 605 |
| Single | 1 | 1,081 | 1 | 1 | 8,044 | 1,117 |
| Multipart/blocked | 19 | 30,593 | 8 | 9 | 22,030 | 9,070 |
| Paused | 1 | 2,385 | 1 | 1 | 7,625 | 1,229 |
| Forced read failure | 8 | 13,474 | 2 (one failed) | 2 | 8,535 | 1,760 |

Tokens are measured from CLI `step_finish` usage and the primary session's API
metadata, not the planner's byte-based estimates. Peak input is the maximum
reported `input + cache.read` for a primary model step; auxiliary requests and
worker usage are excluded. This is reporting evidence for the named runtime and
model, not an old-versus-new performance benchmark or a guarantee for other
models and inputs.

## Failed probes and limits

Initial probes exposed premature termination after a part heading, read-ahead
without delivery, and a continuation offset based on fetched rather than emitted
text. The skill now explicitly alternates complete text delivery with the next
tool call, treats intermediate parts as continuing work, tracks emitted offsets
separately, and denies direct journal-file reads for the orchestrator.

A stress fixture with long runs of identical repeated sentences also suffered
verbatim-copy omissions despite a completeness claim. The passing multipart case
uses distinct synthetic measurements; it does not establish lossless copying for
that repetitive stress pattern. Model-generated report delivery remains subject
to fidelity errors; the durable report file is authoritative. A timed-out probe
was stopped and is not counted as passing evidence. Partial transport/output
truncation was not injected: when its exact delivered prefix cannot be verified,
the skill requires a labelled conservative replay offset and a repetition warning.

Ordinary validation also passed: `scripts/lint.sh`, the documented Node suites
(53 tests including chat bootstrap), `scripts/test-python.sh` (85% aggregate
branch-aware coverage), both documented image builds, the chat integration test,
and `tests/integration/run.sh`. The final code CI also passed Lint, Tests, Images,
and Compose integration. Prompt-contract and deterministic chunk tests remain
separate from these real-model observations.
