# Planned work

This plan lists only work that remains to be done. Deliver changes in small,
independently deployable PRs.

## Current priorities

### [#15](https://github.com/mstroppel/karpathy-wiki/issues/15): CUDA and speaker diarization

1. Ship an opt-in CUDA speech image with compatible, pinned CUDA/cuDNN,
   CTranslate2, PyTorch, and pyannote dependencies, plus a Compose override
   reserving an NVIDIA GPU. Document host driver and NVIDIA Container Toolkit
   requirements and the GPU device/compute settings.
2. Complete the diarization runtime: pin model revisions, document model
   license acceptance and token setup, configure the processing device, and
   verify sequential transcription/diarization with GPU-memory release between
   stages.
3. Benchmark real models on the target GPU with short, consented one- and
   two-speaker recordings. Measure VRAM, processing time, and transcription
   and diarization quality before choosing a supported GPU model preset.
4. Verify real-model behavior on the host: interrupted sync/worker/restart,
   diarization disabled, ambiguous or overlapping speakers, and content-free
   failures that preserve the last published generation.

### [#26](https://github.com/mstroppel/karpathy-wiki/issues/26): Integration coverage

1. Add a disposable audio-profile integration test using the fake speech
   backend: WebDAV recording → provider/worker queue → sanitized manifest →
   wiki status. Exercise worker configuration changes, cache repair, source
   removal, restarts, and failure/health behavior across real containers.
2. Validate the speech image on an ARM64 runtime, including native dependency
   imports and a small real audio decode/transcription smoke test.
3. Reconcile the remaining #26 acceptance criteria with the test evidence
   and close the issue once its operational coverage gaps are verified.

### [#18](https://github.com/mstroppel/karpathy-wiki/issues/18): Multi-language support

1. Add central `WIKI_LANGUAGE`/`WIKI_LOCALE` settings.
2. Apply language selection to chat responses, wiki defaults, skills, analyses
   and PDF output; centralize prompts and generated UI text.
3. Require explicit translation of source documents rather than translating
   them implicitly.
4. Test German, English and unsupported-locale fallbacks.

## Further user-facing extensions

### [#78](https://github.com/mstroppel/karpathy-wiki/issues/78): Email ingest

1. Configure a mailbox and one exact recipient address (including any `+tag`).
   Process only messages demonstrably delivered to that address; do not treat
   other aliases, recipients or header-only matches as authorization to ingest.
2. Fetch new messages with least-privilege credentials, deduplicate and recover
   across restarts without losing or republishing messages. Define how messages
   addressed to multiple recipients and messages without trustworthy delivery
   recipient metadata are handled.
3. Convert the selected message body and supported attachments to sanitized
   source entries with provenance and a versioned provider manifest. Apply
   local redaction before wiki/model access, and use the shared
   `karpathy-wiki-speech` worker for audio attachments when enabled.
4. Cover exact-address filtering, malformed messages, attachment handling,
   duplicates, failures and restarts with tests; document mailbox setup,
   retention and deletion behavior.

## After the first stable 1.0 release

### [#108](https://github.com/mstroppel/karpathy-wiki/issues/108): Isolate wiki-agent shell execution

Run compound Git and inspection commands for writing wiki agents in an isolated
runner with only the wiki checkout, read-only sanitized sources and scratch
space mounted. Keep OpenCode credentials, sessions, state, server environment
and the Docker socket inaccessible; restrict network access and bound runtime
and output. Verify credential isolation, read-only sources, focused commits and
accurate command failures in an integration test.

### [#46](https://github.com/mstroppel/karpathy-wiki/issues/46): Version and migrate generated security policy

Separate mandatory release-managed policy from user-editable instructions and
record policy/schema versions. Implement explicit, idempotent migrations and
upgrade logs; detect missing, newer, modified and conflicting files. Document
rollback and release-note verification, and test fresh installs, repeated
initialization, profile changes, forward migration, failure and rollback.

Before 1.0, installations reorganize data manually according to release notes;
do not add automatic migrations, legacy aliases or data-layout moves.
