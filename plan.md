# GitHub Issue Implementation Plan

This plan covers all issues currently tracked in the repository: **22 issues in total, 10 open and 12 closed**.

## Completed issues

No implementation work is planned for these issues unless a regression is found:

- [#14](https://github.com/mstroppel/karpathy-wiki/issues/14) — renamed the Nextcloud integration to WebDAV and added ingest tracking.
- [#17](https://github.com/mstroppel/karpathy-wiki/issues/17) — saves analyses as wiki pages with printable PDF views.
- [#20](https://github.com/mstroppel/karpathy-wiki/issues/20) — anonymizes WebDAV input.
- [#23](https://github.com/mstroppel/karpathy-wiki/issues/23) — provides the version-aware installer and update launcher.
- [#24](https://github.com/mstroppel/karpathy-wiki/issues/24) — split ingest into core and plugin distributions (PR [#64](https://github.com/mstroppel/karpathy-wiki/pull/64)).
- [#25](https://github.com/mstroppel/karpathy-wiki/issues/25) — defined the versioned ingest status contract with shared conformance fixtures (PR [#64](https://github.com/mstroppel/karpathy-wiki/pull/64)).
- [#26](https://github.com/mstroppel/karpathy-wiki/issues/26) — integration coverage and daemon hardening.
- [#28](https://github.com/mstroppel/karpathy-wiki/issues/28) — consistent quality checks.
- [#29](https://github.com/mstroppel/karpathy-wiki/issues/29) — closed supply-chain gaps.
- [#47](https://github.com/mstroppel/karpathy-wiki/issues/47) — migrated the stack to OpenCode v2.
- [#53](https://github.com/mstroppel/karpathy-wiki/issues/53) — removed migration code before version 1.0.
- [#56](https://github.com/mstroppel/karpathy-wiki/issues/56) — removed the session exporter; sharing by wiki link and printable PDF view already covers its use cases, and analysis runs directly in OpenCode.
- [#42](https://github.com/mstroppel/karpathy-wiki/issues/42) — defined the provider manifest as the end-to-end plugin contract (PR [#65](https://github.com/mstroppel/karpathy-wiki/pull/65)).

## Current findings

- Paperless already writes `paperless_url` and a visible Paperless link into sanitized source documents. [#50](https://github.com/mstroppel/karpathy-wiki/issues/50) should first verify that the link is preserved in generated wiki pages.
- WebDAV still publishes files one at a time and loads the redaction file only once per process. [#43](https://github.com/mstroppel/karpathy-wiki/issues/43) therefore requires a real generation-based publication design.
- OpenCode writes directly to the wiki and the SilverBullet mount is writable. These are central constraints addressed by [#44](https://github.com/mstroppel/karpathy-wiki/issues/44).

## Phase 2 — Coherent source publication

### [#43](https://github.com/mstroppel/karpathy-wiki/issues/43): Publish WebDAV generations

1. Build each synchronization in a new temporary generation directory.
2. Record the upstream inventory/revisions and redaction fingerprint.
3. Validate and anonymize every file before publication.
4. Atomically switch the active generation only after the full cycle succeeds.
5. Keep the last successful generation active after any failure.
6. Reload redaction configuration every cycle and regenerate on fingerprint changes.
7. Quarantine failed generations without storing source content in reports.
8. Document retention, cleanup, and abandoned-generation recovery.

### [#27](https://github.com/mstroppel/karpathy-wiki/issues/27): Split the Paperless module

Split the current module into clear boundaries for configuration, the HTTP client, anonymization, document modelling/hashing, rendering/frontmatter, persistence, and daemon/health lifecycle. Preserve the CLI and environment semantics while adding focused tests. Use the contracts from Phase 1 rather than introducing another provider-specific format.

### [#50](https://github.com/mstroppel/karpathy-wiki/issues/50): Preserve Paperless links

1. Verify that `paperless_url` survives source-to-wiki rendering.
2. Ensure every Paperless source page has a visible, validated HTTPS link.
3. Add a regression test covering frontmatter and rendered Markdown.

### [#40](https://github.com/mstroppel/karpathy-wiki/issues/40): Use the wiki name in the browser title

Determine the supported OpenCode v2 branding/title mechanism, connect it to `WIKI_NAME`, retain a safe fallback, and add a browser-level regression test.

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

1. **Next:** #43 and #27.
2. **Small independent improvements:** #50 and #40.
3. **Transactional architecture:** #44, delivered incrementally.
4. **Future capabilities:** #18 and #15.
5. **After 1.0:** #46.

Every implementation PR should use a focused Conventional Commit and document behavior changes, security implications, data-layout/migration impact, and test evidence.
