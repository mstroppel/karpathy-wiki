# GitHub Issue Implementation Plan

This plan tracks the remaining open issues after Phase 2.
[#40](https://github.com/mstroppel/karpathy-wiki/issues/40) was closed as
upstream-blocked: OpenCode v2 exposes no supported mechanism to
change the web UI browser title (no config field, environment variable, or
plugin hook; the title is a static build asset), so `WIKI_NAME` cannot be
connected to the chat browser title until OpenCode ships such a mechanism.

## Current findings

- WebDAV now publishes coherent generations (PR for [#43](https://github.com/mstroppel/karpathy-wiki/issues/43));
  the Paperless module is split along responsibility boundaries
  ([#27](https://github.com/mstroppel/karpathy-wiki/issues/27)) and Paperless
  links survive source-to-wiki rendering ([#50](https://github.com/mstroppel/karpathy-wiki/issues/50)).
- OpenCode writes directly to the wiki and the SilverBullet mount is writable. These are central constraints addressed by [#44](https://github.com/mstroppel/karpathy-wiki/issues/44).

## Phase 3 — Transactional ingestion and publishing

### [#44](https://github.com/mstroppel/karpathy-wiki/issues/44): Refactor into a transactional pipeline

This issue should be delivered as independently deployable work packages, not as a flag-day rewrite:

1. **Durable state:** SQLite tables for jobs, leases, source generations, publications, retries, and idempotency keys.
2. **Queue:** recoverable jobs, backoff, restart recovery, and one active publisher lease.
3. **Restricted workers:** workers read immutable inputs and return validated patches with provenance; they cannot commit, publish, access source systems, or make arbitrary outbound requests.
4. **Serialized publisher:** isolated Git worktrees, stale-base detection, path/provenance/content validation, and one focused commit per publication.
5. **Immutable releases:** atomically publish complete wiki revisions and mount published data read-only in SilverBullet.
6. **Network boundaries:** keep services on private networks and expose only an authenticated gateway through the external proxy network.
7. **Analysis boundary:** share analyses through wiki links and printable PDF views; analysis runs directly in OpenCode with session access. The session PDF mirror was already removed (#56).
8. **Operational verification:** test restart recovery, duplicate jobs, concurrent jobs, failed validation, rollback, backup, and restore.

Each package must preserve existing data and remain independently testable.

## Phase 4 — User-facing extensions

### [#18](https://github.com/mstroppel/karpathy-wiki/issues/18): Multi-language support

1. Add central `WIKI_LANGUAGE`/`WIKI_LOCALE` settings.
2. Separate language selection for chat responses, wiki defaults, skills, analyses, and PDF output.
3. Centralize prompts and generated UI text.
4. Do not translate source documents implicitly; make translation explicit.
5. Test German, English, and unsupported-locale fallbacks.

### [#15](https://github.com/mstroppel/karpathy-wiki/issues/15): Audio ingest

After the package and plugin contracts are stable:

1. Add an audio provider with a versioned manifest.
2. Discover audio files from WebDAV and process them idempotently by hash.
3. Use a replaceable transcription backend with optional speaker diarization.
4. Emit Markdown with timestamps, speaker labels, and provenance.
5. Apply anonymization after transcription and before publication.
6. Keep external transcription opt-in and explicitly configured.

## Post-1.0 work

### [#46](https://github.com/mstroppel/karpathy-wiki/issues/46): Version and migrate generated security policy

This issue must remain deferred until the first stable 1.0 release, in line with the repository policy. At that point implement:

- versioned release-managed security policy;
- separately stored user-editable instructions;
- persistent policy/schema versions;
- explicit, idempotent migrations and upgrade logs;
- detection of missing, newer, modified, or conflicting files;
- documented rollback and release-note verification;
- tests for fresh installs, repeated initialization, profile changes, forward migration, failure, and rollback.

No pre-1.0 migration code, legacy aliases, or automatic data-layout moves should be added as part of this plan.

## Suggested priority

1. **Transactional architecture:** #44, delivered incrementally.
2. **Future capabilities:** #18 and #15.
3. **After 1.0:** #46.

Every implementation PR should use a focused Conventional Commit and document behavior changes, security implications, data-layout/migration impact, and test evidence.
