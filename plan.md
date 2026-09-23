# Planned work

This plan lists only work that remains to be done. Deliver changes in small,
independently deployable PRs.

## Transactional ingestion and publishing — [#44](https://github.com/mstroppel/karpathy-wiki/issues/44)

1. Extend durable job coordination to Paperless and future providers. Add an
   exclusive publisher lease and expose queue depth, oldest job age, last
   successful source generation and publication, and failed/retried work.
2. Add a serialized publisher using isolated Git worktrees. Validate patch
   paths, provenance, schema and content; reject or explicitly rebase and
   revalidate stale-base patches. Record the source generation, wiki base,
   model/job identity, validation result and resulting commit for each
   publication.
3. Move model work to restricted workers that read immutable source generations
   and wiki base revisions and return patches. Workers must not commit,
   publish, contact source systems or make arbitrary outbound requests; route
   model traffic through an allowlisted gateway with size, timeout and spend
   limits.
4. Atomically serve complete published wiki revisions and mount them read-only
   in SilverBullet.
5. Put application services on private networks, expose only an authenticated
   gateway on the external proxy network, and use service credentials for
   internal API calls.
6. Test concurrent and duplicate jobs, worker and validation failures, stale
   bases, publisher restart and recovery, publication, serving and rollback.
   Document backup and restore for state, Git, manifests, configuration and
   credentials; verify a restore. Document manual upgrade and rollback steps
   for installations whose data layout changes.

## Supporting work

### [#42](https://github.com/mstroppel/karpathy-wiki/issues/42): Verify external provider deployment

Provide a buildable third-party example in a separate repository or fixture
that installs the plugin, publishes its manifest and is enabled without
modifying or rebuilding the core OpenCode image. Test this deployment path.

### [#26](https://github.com/mstroppel/karpathy-wiki/issues/26): Finish operational test coverage

Report coverage in CI and exercise successful ingest-to-wiki publication in a
disposable-service integration test. Expand failure and restart scenarios as
the publisher and worker boundaries are introduced.

### [#28](https://github.com/mstroppel/karpathy-wiki/issues/28): Pin the shell toolchain

Pin and validate the ShellCheck version used in CI so the documented local
lint gate and CI run the same version.

### [#68](https://github.com/mstroppel/karpathy-wiki/issues/68): Specify OpenCode update policy

Clarify what “minor and build version updates” means for the pinned OpenCode
release, compare it with the existing scheduled update workflow, and implement
only the missing update behavior and verification.

## User-facing extensions

### [#18](https://github.com/mstroppel/karpathy-wiki/issues/18): Multi-language support

1. Add central `WIKI_LANGUAGE`/`WIKI_LOCALE` settings.
2. Apply language selection to chat responses, wiki defaults, skills, analyses
   and PDF output; centralize prompts and generated UI text.
3. Require explicit translation of source documents rather than translating
   them implicitly.
4. Test German, English and unsupported-locale fallbacks.

### [#15](https://github.com/mstroppel/karpathy-wiki/issues/15): Audio ingest

1. Add an audio provider using the versioned manifest contract and discover
   WebDAV audio files idempotently by hash.
2. Use a replaceable transcription backend with optional speaker diarization;
   emit Markdown with timestamps, speaker labels and provenance.
3. Anonymize transcripts before publication. Make external transcription
   opt-in and explicitly configured.

## After the first stable 1.0 release

### [#46](https://github.com/mstroppel/karpathy-wiki/issues/46): Version and migrate generated security policy

Separate mandatory release-managed policy from user-editable instructions and
record policy/schema versions. Implement explicit, idempotent migrations and
upgrade logs; detect missing, newer, modified and conflicting files. Document
rollback and release-note verification, and test fresh installs, repeated
initialization, profile changes, forward migration, failure and rollback.

Before 1.0, installations reorganize data manually according to release notes;
do not add automatic migrations, legacy aliases or data-layout moves.
