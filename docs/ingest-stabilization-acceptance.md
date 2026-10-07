# Ingest stabilization acceptance

## Reproduce

```sh
docker build --target opencode -t kw-opencode:integration -f opencode/Dockerfile .
python3 tests/acceptance/ingest_stabilization.py --model 'openai/gpt-6-luna#high'
```

This is opt-in, incurs provider usage and requires a configured provider.
It uses the repository's exact `/ingest-new` prompt, agents, tools and skills.
No production sources, wiki, journals or session database are mounted. The
selected provider credential is exported only into process memory, imported
through a pipe and never printed or persisted in evidence. The disposable
container and its credential database are removed on success or failure.

The fixture contains four short non-sensitive sources (multiple measurements,
a changed statement, a damaged transcript and a test note), 2,000 unrelated
historical overview paragraphs and a log lacking an EOF newline. A fault injected
only into the disposable journal pauses the first publication after its commit
but before its result record. The harness restarts the whole container, reruns
`/ingest-new`, and requires adoption of the same run and verified resume, not
re-extraction or a duplicate commit.

Assertions cover exactly four source commits/records, unchanged source bytes,
preserved historical paragraphs/tail, clean Git, final `new=0`/`outdated=0`, every
report detail field, a parent report link/run ID, no worker shell/edit calls,
four preparations and a resume, plus measured worker peak context at or below
32,000 input-plus-cache tokens. Evidence is written to a newly created private
directory below `/tmp/opencode`; only its aggregate `summary.json` should be
published. Session/trace files remain private even though fixtures are synthetic.
If that directory is not writable, select a private external directory with
`--evidence-root "$HOME/.local/state/karpathy-wiki-acceptance"`; do not change
permissions on someone else's temporary directory.

## Evidence status

Execution is in progress; no passing real-model result is claimed yet.
Deterministic regression coverage includes input correction, similar/stale
section references, large-page preservation, owned index-lock recovery before
and after HEAD transition, lost stage acknowledgement and journal interruptions
both before and after atomic record publication. These are separate from the
real-model run and from actual OpenChamber local-link behavior.
