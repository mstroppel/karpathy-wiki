# Planned work

This plan lists only work that remains to be done. Deliver changes in small,
independently deployable PRs.

## Current priorities

### [#15](https://github.com/mstroppel/karpathy-wiki/issues/15): Audio ingest

The provider, worker handoff, redaction, and publication are implemented
(see [audio ingest](docs/audio.md)). Remaining host-side work:

1. Run the bounded local transcription spike on the target host: measure
   VRAM, processing time, and diarization quality with short, consented
   one- and two-speaker recordings; confirm the CUDA runtime before
   choosing the default model preset.
2. Ship the opt-in CUDA speech image (pinned PyTorch/pyannote stack with an
   NVIDIA GPU reservation) and the diarization model license/consent flow.
3. Verify real-model behavior on the host: interrupted sync/worker/restart
   and diarization-disabled-or-uncertain cases, then close the issue.

### [#26](https://github.com/mstroppel/karpathy-wiki/issues/26): Integration coverage

Reassess the remaining acceptance criteria against the coverage reporting and
disposable source-to-wiki test added in #119. Cover any remaining operational
failure or restart gap with a focused test, then update or close the issue.

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
