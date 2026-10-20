# Email ingestion

The optional `email` profile receives **one IMAP account and folder for this
installation**. It considers all existing messages, read and unread. It publishes
only body text and decoded message headers; no attachment extraction, speech,
OAuth, SMTP receiver, or automatic wiki commits are included.

**The configured account/folder is the trust boundary. There is no recipient
verification or strict mode.** Without the optional recipient allowlist below,
To/Cc/Bcc and delivery headers do not filter mail. Enabling reception can archive
and delete **all eligible existing and new messages in that folder**, subject to
the optional sender and recipient allowlists and size and durability safeguards
below. Use a dedicated mailbox or reliably isolated folder.

## Before enabling destructive reception

1. Use a dedicated mailbox or restricted account with access only to the chosen
   folder. The account needs fetch, flag and targeted-delete rights.
2. Confirm TLS IMAP with certificate verification (default port 993) and password
   or app-password login. Unencrypted IMAP and STARTTLS downgrade paths are not
   supported. Some providers require OAuth and cannot use this connector.
3. Verify the selected folder contains only mail intended for this wiki, including
   its backlog. If you need recipient routing, configure and verify server-side
   delivery rules into a dedicated folder before enabling reception. The optional
   recipient allowlist is a filter, not routing or verification. Missing,
   repeated or differing delivery headers do not prevent ingestion/deletion.
4. Configure the shared deny-list redactions secret. Review output before model
   access: deny-list redaction is not complete anonymization.
5. Back up the private original archive and its SQLite ledger together,
   preferably on encrypted storage. Originals are **unredacted, include all
   attachments, and have no automatic expiry**. Never expose the archive through
   OpenCode, `raw-files`, or another public file server.

Sender filtering requires one exact From mailbox if configured. Domains compare
case-insensitively; local parts and plus tags remain exact. Without an allowlist,
headers are not checked before archiving.

Recipient filtering accepts a message when at least one decoded To, Cc or Bcc
mailbox exactly matches `EMAIL_RECIPIENT_ALLOWLIST`. Domains compare
case-insensitively; local parts and plus tags remain exact, so
`test+email@example.com` does not match `test@example.com`. Delivery headers
(`X-Original-To`, `Delivered-To`, `Envelope-To`, `Received`) are never consulted,
and mail delivered without recipient headers — typically blind-carbon copies —
cannot match and stays on the server. Route BCC-only mail with server-side
delivery rules if you need it. Both allowlists must pass when both are
configured; an empty list disables its filter.

The server must advertise **UIDPLUS**. Deletion uses only `UID STORE` for the
matched UID and `UID EXPUNGE <uid>`; never general `EXPUNGE` or `CLOSE`, which could
delete other clients' marked messages. Servers without targeted deletion fail
closed. `BODY.PEEK` leaves the Seen flag unchanged.

## Configuration

In private `.env`:

```env
COMPOSE_PROFILES=email
EMAIL_IMAP_HOST=imap.example.com
EMAIL_IMAP_PORT=993
EMAIL_IMAP_USERNAME=wiki@example.com
EMAIL_IMAP_PASSWORD=replace-with-app-password
EMAIL_IMAP_FOLDER=INBOX
# Optional exact decoded From mailboxes, comma-separated:
EMAIL_FROM_ALLOWLIST=sender@example.com
# Optional exact decoded To/Cc/Bcc mailboxes, comma-separated:
EMAIL_RECIPIENT_ALLOWLIST=test+email@example.com
EMAIL_POLL_INTERVAL=60s
EMAIL_BATCH_SIZE=25
EMAIL_MAX_BYTES=26214400
EMAIL_TIMEOUT_SECONDS=30
REDACTIONS_FILE=./secrets/redactions.json
```

An empty allowlist disables sender filtering. A configured list applies before
archiving/deletion but is **not authentication**: From is forgeable. Configure
server-side anti-spoofing, SPF/DKIM/DMARC and spam handling. Otherwise spoofed
senders can inject content and incur model costs. Mail is always untrusted data,
never agent instructions. Credentials are provider-only environment variables,
visible to host admins/Docker inspection, not OpenCode. Protect `.env`; use
single-quoted values when necessary to avoid Compose `$` interpolation.

```sh
./karpathy-wiki.sh config --quiet
./karpathy-wiki.sh up -d
```

Changing `.env` requires recreating the service (`up -d` above), not just
restarting it. To pause destructive reception, run
`./karpathy-wiki.sh stop email-ingest`; remove `email` from `COMPOSE_PROFILES`
to keep it disabled on future starts. Stopping cannot undo server deletion;
verified originals remain in the private archive.

**Activation can delete all eligible old mail in the folder.** Non-matching,
oversized or incomplete messages stay on the server. Batches reserve
half their slots (rounded down) for newest UIDs; the remainder advances an
independent rotating backlog cursor. Batch size 1 alternates backlog and newest
turns, persisted across restarts. Growing backlogs and poison/nonmatching messages
cannot monopolize new-mail reception, and older mail continues to be scanned. Limits
default to 25 MiB per original including attachments, 25 candidates per
receive/extraction batch, and 30 seconds per network operation. Polling waits
after a cycle; cycles never overlap. Tests never contact a live mailbox.

## Durability and recovery

`${DATA_ROOT}/incoming/email` is private (`0700`). Immutable `.eml` files (`0600`)
have opaque names. Originals are verified after writing; the file **and parent
directory** are fsynced, then a provider-local SQLite EXTRA-synchronous ledger
commit records the delivery **before** deletion. Disk/state/integrity failures
never permit deletion. A crash before the ledger commit can leave an orphan
original; keep it for operator inspection. Pending deletions resume by
account/folder/UIDVALIDITY/UID with an integrity recheck. Changed UIDVALIDITY
cannot authorize deletion of a reused UID from an old epoch.

Every distinct raw delivery is retained, even if its source exists. Deduplication
uses selected headers and MIME content, excluding transport headers. Identical
copies with differing transit headers share a stable opaque source ID; reused
Message-IDs with changed content remain distinct. Message-ID alone is never a
content identity. Failed deletion does not re-publish a completed source.

**Server deletion follows durable original/ledger storage, not extraction,
redaction or a wiki commit.** Malformed MIME, bad charsets, empty bodies and
privacy failures remain recoverable locally. Extraction errors back off one
hour, stop after five attempts, and remain unhealthy/content-free errors until
repair/replay. An unreachable IMAP server does not prevent local extraction.
No stateless destructive or publication fallback exists.

Pending server deletes recheck the archived message against the current sender
and recipient allowlists as well as archive integrity. MIME identity processing
occurs only after the original is durable; pathological MIME falls back to
exact-original-byte deduplication.
Failed replay retains the last successful projection, subject to the current
privacy gate, and reports the failure without discarding safe published text.

Body extraction prefers `text/plain`, otherwise converts HTML locally to text,
preserves signatures/quotes, ignores attachments/embedded messages and loads no
external resources. All decoded headers, including BCC and delivery provenance,
are visible **after redaction**. The complete document crosses the privacy gate;
public IDs/claims are opaque and the whole manifest gets a residual-match check.
Raw addresses, filenames, Message-IDs and original paths are never public IDs.

Coherent generations publish through an atomic manifest at
`${DATA_ROOT}/sources/email/manifest.json`, owning wiki subtree `email`. Previous
projections are removed after publication; originals are never pruned. Stricter
redactions recheck/re-redact stored projections on the next cycle; restoring
previously removed values requires replay. Revocations do not erase already
committed wiki knowledge; request cleanup explicitly in the normal wiki workflow.

## Explicit offline replay

Stop the daemon before replay; an exclusive archive lock prevents concurrent
receive/replay. Re-extract one source ID from the manifest, or all sources:

```sh
./karpathy-wiki.sh stop email-ingest
./karpathy-wiki.sh run --rm email-ingest email --replay all
# Or: ./karpathy-wiki.sh run --rm email-ingest email --replay <source-id>
./karpathy-wiki.sh up -d email-ingest
```

Replay requires archive/ledger, redactions and publication directory, not IMAP
credentials. It neither contacts nor deletes server mail. Stable IDs keep the
wiki destination; revisions change only when text changes. Algorithm upgrades
never automatically re-extract old originals. Inspect errors and replay after
repair. Use `/ingest-new` for new/changed sources afterward; automatic wiki
ingestion remains disabled. Removing originals sacrifices replay and can block
pending deletes; there is no archive purge command.
