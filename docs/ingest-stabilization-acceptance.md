# Ingest stabilization acceptance

## Reproduce

```sh
docker build --target opencode -t kw-opencode:integration -f opencode/Dockerfile .
python3 tests/acceptance/ingest_stabilization.py --model 'openai/gpt-6-luna#high'
```

This is opt-in, incurs provider usage and requires a configured provider.
It uses the repository's `/ingest-new` prompt, agents, tools and skills, with an
explicit additional synthetic acceptance request to correct the current
measurement while preserving the similarly worded historical section.
No production sources, wiki, journals or session database are mounted. The
selected provider credential is exported only into process memory, imported
through a pipe and never printed or persisted in evidence. The disposable
container and its credential database are removed on success or failure.

The fixture contains four short non-sensitive sources (multiple measurements,
a changed statement, a damaged transcript and a test note), 2,000 unrelated
historical overview paragraphs, similar current/archive measurement sections
and a log lacking an EOF newline. A disposable-only pre-write reference rejection
requires the model to reread the targeted section and correct once. A fault injected
only into the disposable journal pauses the first publication after its commit
but before its result record. The harness restarts the whole container, reruns
`/ingest-new`, and requires adoption of the same run and verified resume, not
re-extraction or a duplicate commit.

Assertions cover exactly four source commits/records, unchanged source bytes,
preserved historical paragraphs/tail, clean Git, final `new=0`/`outdated=0`, every
report detail field, a parent report link/run ID, no worker shell/edit calls,
four preparations and a resume, preservation of the archive measurement and
correction of the current measurement, an exercised pre-write rejection,
plus measured worker peak context at or below
32,000 input-plus-cache tokens. Evidence is written to a newly created private
directory below `/tmp/opencode`; only its aggregate `summary.json` should be
published. Session/trace files remain private even though fixtures are synthetic.
If that directory is not writable, select a private external directory with
`--evidence-root "$HOME/.local/state/karpathy-wiki-acceptance"`; do not change
permissions on someone else's temporary directory.

## Evidence status

Execution is in progress; no passing real-model result is claimed yet.
Earlier attempts failed rather than being accepted with partial ingestion:

| Observed blocker | Correction before rerun |
| --- | --- |
| Worker paginated the large overview to its work limit | Targeted queries; no full historical scan |
| Absolute source-page path supplied to prepare | Mandatory pages generated in code; invalid optional paths classified before preparation |
| Redundant post-publication check reconstructed private receipt IDs incorrectly | Publisher owns commit/journal verification; nested draft receipt location documented |
| Read-only call omitted preparation identity | Safe typed pre-access rejection allows one corrected input |
| Model replacement dropped six historical lines inside a broad reference | Tool rejects multi-line context loss; target-line references instead of reconstructed read windows |
| Complete source/content checks passed, but interrupted child was absent from parent transcript | Capture all synthetic stored session IDs, including interrupted workers, before token/count assertions |

The explicit calibration request must be forwarded to its source worker and
included in that source commit, not separately delegated to maintenance.
Session discovery takes an in-memory SQLite backup inside the disposable
container and reads only session IDs. Databases/credentials are not copied to
evidence. Every discovered session is exported privately so peak context also
includes the worker interrupted before its parent received the child result.
Deterministic regression coverage includes input correction, similar/stale
section references, large-page preservation, owned index-lock recovery before
and after HEAD transition, lost stage acknowledgement and journal interruptions
both before and after atomic record publication. These are separate from the
real-model run and from actual OpenChamber local-link behavior.
