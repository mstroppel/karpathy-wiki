# Email ingestion

The optional `email` profile receives **one IMAP account and folder for this
installation**. It considers all existing messages, read and unread. It publishes
only body text and decoded message headers; no attachment extraction, speech,
OAuth, SMTP receiver, or automatic wiki commits are included.

## Before enabling destructive reception

1. Use a dedicated mailbox or restricted account with access only to the chosen
   folder. The account needs fetch, flag and targeted-delete rights.
2. Confirm TLS IMAP with certificate verification (default port 993) and password
   or app-password login. Unencrypted IMAP and STARTTLS downgrade paths are not
   supported. Some providers require OAuth and cannot use this connector.
3. In default **strict** mode, inspect actual mail delivered **To, CC and BCC**
   to your exact plus address.
   The receiving server must strip sender-supplied copies of the delivery header
   and set **exactly one** authoritative address preserving the plus tag.
   `Delivered-To` and `X-Original-To` are examples, not inherently trusted headers.
   Verify your server's configuration. Forwarding or repeated headers can be
   ambiguous. To/CC/BCC alone never authorize reception; BCC usually disappears.
4. Configure the shared deny-list redactions secret. Review output before model
   access: deny-list redaction is not complete anonymization.
5. Back up the private original archive and its SQLite ledger together,
   preferably on encrypted storage. Originals are **unredacted, include all
   attachments, and have no automatic expiry**. Never expose the archive through
   OpenCode, `raw-files`, or another public file server.

In strict mode, missing/repeated/multiple-address delivery metadata, other aliases
and other plus tags do not match. Domains compare case-insensitively; local parts remain
exact. Sender filtering requires one exact From mailbox if configured.

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
EMAIL_RECIPIENT=server+fs-wiki@example.com
EMAIL_RECIPIENT_VERIFICATION=strict
EMAIL_DELIVERY_HEADER=X-Original-To
# Set ONLY after verifying server-specific header trust above:
EMAIL_DELIVERY_HEADER_TRUSTED=1
# Optional exact decoded From mailboxes, comma-separated:
EMAIL_FROM_ALLOWLIST=sender@example.com
EMAIL_POLL_INTERVAL=60s
EMAIL_BATCH_SIZE=25
EMAIL_MAX_BYTES=26214400
EMAIL_TIMEOUT_SECONDS=30
REDACTIONS_FILE=./secrets/redactions.json
```

### Dedicated-folder mode (explicit trust decision)

Only for a dedicated mailbox or restricted account/folder, set:

```env
EMAIL_RECIPIENT_VERIFICATION=disabled
```

**This can ingest and delete ALL eligible existing and new messages in the
selected folder**, subject to `EMAIL_FROM_ALLOWLIST`, size limits and the other
safety gates. The configured account/folder, not a message header, is the trust
boundary. `EMAIL_RECIPIENT` is optional and does not filter messages in this mode
(if supplied, it must still be a valid single mailbox). Neither To/Cc nor From
becomes authenticated recipient proof. Delivery headers and
`EMAIL_DELIVERY_HEADER_TRUSTED=1` are not required; disabling verification does
not mark any header trusted. Credentials, verified TLS, UIDPLUS, verified durable
original/ledger storage before deletion and privacy gates remain mandatory.
Only `strict` and `disabled` are valid modes; a missing header or trust flag never
implicitly enables disabled mode.

After changing mode, validate and recreate the service to load the new environment:

```sh
./karpathy-wiki.sh config --quiet
./karpathy-wiki.sh up -d --force-recreate email-ingest
```

To switch back, set `EMAIL_RECIPIENT_VERIFICATION=strict`, configure the exact
`EMAIL_RECIPIENT` and verified authoritative `EMAIL_DELIVERY_HEADER`, set
`EMAIL_DELIVERY_HEADER_TRUSTED=1`, then validate/recreate as above. Pending deletes
are rechecked against the current mode and sender filter; unproven deliveries
remain on the server in strict mode. Already deleted mail cannot be restored by
changing mode; the private archive is retained. Changing the recipient also
changes the delivery tracking scope; surviving server messages are considered
again with current policy and the archive still deduplicates exact originals.

### Filtering and activation

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

**Activation can delete old matching mail.** Unmatched, non-allowlisted,
oversized, incomplete or unproven messages stay on the server. Batches reserve
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

Pending server deletes recheck the archived message against the current recipient
verification mode and From allowlist, archive integrity and UIDVALIDITY.
MIME identity processing occurs only after the original
is durable; pathological MIME falls back to exact-original-byte deduplication.
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
