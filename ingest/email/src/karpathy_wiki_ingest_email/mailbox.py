"""TLS IMAP reception. No Seen flag changes and never a mailbox-wide EXPUNGE."""

from __future__ import annotations

import hashlib
import imaplib
import json
import re
import ssl
from contextlib import contextmanager
from email import policy
from email.parser import BytesHeaderParser
from typing import Any

from .settings import Settings, mailboxes
from .store import Store


def checked(result: tuple[Any, Any]) -> Any:
    status, data = result
    if status != "OK":
        # Responses may contain credentials/addresses/message text. Never log them.
        raise OSError("IMAP command failed")
    return data


@contextmanager
def connection(settings: Settings):
    client = imaplib.IMAP4_SSL(
        settings.host,
        settings.port,
        ssl_context=ssl.create_default_context(),
        timeout=settings.timeout,
    )
    try:
        checked(client.login(settings.username, settings.password))
        yield client
    finally:
        # logout, not close(): CLOSE would expunge unrelated deleted messages.
        try:
            client.logout()
        except (OSError, imaplib.IMAP4.error):
            pass


def authorized(raw: bytes, settings: Settings) -> bool:
    # Authorization concerns envelope/header evidence, not body validity. Bad
    # MIME bodies can still be archived and recovered after server deletion.
    message = BytesHeaderParser(policy=policy.default).parsebytes(raw)
    if message.defects:
        return False
    if settings.recipient_verification == "strict":
        if not settings.trusted:
            return False
        values = message.get_all(settings.delivery_header, [])
        # Reject duplicated/ambiguous delivery metadata, even if one value matches.
        if len(values) != 1 or mailboxes([str(values[0])]) != [settings.recipient]:
            return False
    elif settings.recipient_verification != "disabled":
        return False
    if settings.allowlist:
        senders = message.get_all("From", [])
        return len(senders) == 1 and mailboxes([str(senders[0])]) in [
            [sender] for sender in settings.allowlist
        ]
    return True


def literal(data: Any, uid: str) -> bytes:
    chunks = [entry for entry in data if isinstance(entry, tuple)]
    if len(chunks) != 1 or not isinstance(chunks[0][1], bytes):
        raise ValueError("unexpected IMAP fetch response")
    match = re.search(rb"\bUID (\d+)\b", chunks[0][0])
    if match is None or match[1].decode("ascii") != uid:
        raise ValueError("IMAP fetch UID mismatch")
    return chunks[0][1]


def delete(client: Any, store: Store, scope: str, epoch: str, uid: str, original: str) -> None:
    # Check the exact durable original again, including recovery after restart.
    store.read(original)
    checked(client.uid("STORE", uid, "+FLAGS.SILENT", r"(\Deleted)"))
    checked(client.uid("EXPUNGE", uid))
    store.deleted(scope, epoch, uid)


def receive(client: Any, settings: Settings, store: Store) -> tuple[int, int]:
    # Refresh capabilities AFTER authentication; imaplib's cached capabilities
    # are pre-login and represented as strings, not response bytes.
    capabilities = checked(client.capability())
    if not capabilities or not isinstance(capabilities[-1], bytes):
        raise ValueError("IMAP capabilities required")
    if b"UIDPLUS" not in capabilities[-1].upper().split():
        raise ValueError("IMAP UIDPLUS required for targeted deletion")
    checked(client.select('"' + settings.folder.replace("\\", "\\\\").replace('"', '\\"') + '"'))
    status, epoch_data = client.response("UIDVALIDITY")
    if status != "UIDVALIDITY" or not epoch_data or not epoch_data[0]:
        raise ValueError("IMAP UIDVALIDITY required")
    epoch = epoch_data[0].decode("ascii")
    if not epoch.isdecimal():
        raise ValueError("invalid UIDVALIDITY")
    scope = hashlib.sha256(
        json.dumps(
            [settings.host, settings.port, settings.username, settings.folder, settings.recipient]
        ).encode()
    ).hexdigest()
    uids_data = checked(client.uid("SEARCH", None, "ALL"))
    uids = uids_data[0].decode("ascii").split()
    if any(not uid.isdecimal() or int(uid) <= 0 for uid in uids):
        raise ValueError("invalid IMAP UID")
    ordered = sorted(set(uids), key=int)
    cursor_scope = f"{scope}:{epoch}"
    cursor = store.cursor(cursor_scope)
    rotating = [uid for uid in ordered if int(uid) > cursor] + [
        uid for uid in ordered if int(uid) <= cursor
    ]
    # Reserve half the batch for newest mail, independently of the backlog
    # cursor. A growing tail must not delay new arrivals or skip older work.
    newest_count = settings.batch // 2
    if settings.batch == 1:
        # Persist alternating turns so restarts cannot starve either lane.
        turn_scope = f"{cursor_scope}:newest-turn"
        newest_count = store.cursor(turn_scope)
        store.advance(turn_scope, str(1 - newest_count))
    newest = list(reversed(ordered[-newest_count:])) if newest_count else []
    newest_set = set(newest)
    backlog = [uid for uid in rotating if uid not in newest_set][: settings.batch - len(newest)]
    backlog_set = set(backlog)
    archived = failed = 0
    for uid in backlog + newest:
        try:
            previous = store.delivery(scope, epoch, uid)
            if previous is not None:
                if not previous["deleted"] and authorized(
                    store.read(previous["original"]), settings
                ):
                    delete(client, store, scope, epoch, uid, previous["original"])
                continue
            data = checked(client.uid("FETCH", uid, "(UID RFC822.SIZE)"))
            sizes = [
                re.search(rb"\bRFC822.SIZE (\d+)\b", item)
                for item in data
                if isinstance(item, bytes)
            ]
            if len(sizes) != 1 or sizes[0] is None:
                raise ValueError("IMAP size required")
            size = int(sizes[0][1])
            if size > settings.max_bytes or size <= 0:
                raise ValueError("message exceeds size limit")
            raw = literal(
                checked(client.uid("FETCH", uid, f"(UID BODY.PEEK[]<0.{settings.max_bytes + 1}>)")),
                uid,
            )
            if len(raw) != size:
                raise ValueError("incomplete IMAP message")
            if not authorized(raw, settings):
                continue
            original = store.archive(raw, None, scope, epoch, uid)
            archived += 1
            delete(client, store, scope, epoch, uid, original)
        except (OSError, ValueError, RecursionError, imaplib.IMAP4.error):
            failed += 1
        finally:
            # Only backlog work advances the cursor; newest-first processing
            # must not jump over old mail. Rejected/poison messages advance too.
            # SQLite failures intentionally escape and stop destructive work.
            if uid in backlog_set:
                store.advance(cursor_scope, uid)
    return archived, failed
