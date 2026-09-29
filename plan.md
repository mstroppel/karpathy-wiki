# Planned work

This plan lists only work that remains to be done. Deliver changes in small,
independently deployable PRs.

## Current priorities

### [#15](https://github.com/mstroppel/karpathy-wiki/issues/15): Audio ingest

See [audio ingest concept](docs/audio-ingest-concept.md) for the proposed
WebDAV, local transcription, speaker diarization and publication boundaries.

1. Add an audio provider using the versioned manifest contract and discover
   WebDAV audio files idempotently by hash.
2. Use a replaceable transcription backend with optional speaker diarization;
   emit Markdown with timestamps, speaker labels and provenance.
3. Anonymize transcripts before publication. Make external transcription
   opt-in and explicitly configured. Keep transcription reusable by other
   providers, including email ingest.

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
   local redaction before wiki/model access, and use the shared transcription
   provider for audio attachments when enabled.
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
