# Interactive wiki gap review

Enable the local answer provider with `COMPOSE_PROFILES=answers` (or append
`,answers` to existing profiles). Configure `REDACTIONS_FILE` as for WebDAV,
then restart the stack. The profile is optional; the initial review works
without it, but answer submission requires the running `answers-ingest` service.

In OpenCode, use `/gap-review` or explicitly ask for an interactive gap review.
The first pass reads the wiki and reports numbered, evidence-linked questions
about missing information, unsupported claims, contradictions, and outdated
syntheses. Reply with answers, skips, or deferrals in the same conversation.
Review the summary and explicitly confirm submission; answering a question
alone does not submit it.

The confirmed Markdown Q&A is saved in
`${DATA_ROOT}/incoming/answers/<unique-name>.md` (mounted at
`/knowledge/incoming/answers` for OpenCode). Each answered question includes
its finding, affected pages, evidence, and attribution to the user. Unanswered
questions and unresolved conflicts remain marked as open. The provider reads
this inbox read-only, applies the configured local redaction rules, and writes
immutable source revisions plus a versioned manifest under
`${DATA_ROOT}/sources/answers`. The source tree is read-only to OpenCode;
never copy or edit files there directly. The draft inbox remains private and
is not served by SilverBullet or the raw-files profile. Like other deny-list
redaction, unknown private values can remain; review the sanitized source
before sending it to an external model provider.

The agent checks `wiki_ingest_status` for its submitted filename and passes
the published source to `wiki-ingest`; only a completed wiki commit and
`current` status count as an imported answer. If publication is delayed or
blocked, retry ingestion after fixing the provider. Drafts are retained in
the inbox so restarts and redaction-rule changes can republish them. Do not
delete a submitted draft while it is published: a missing draft makes the
provider unhealthy rather than silently dropping its source. The provider
does not automatically modify the wiki or resolve conflicting claims.
