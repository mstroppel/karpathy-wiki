"""Synthetic IMAP/archive/extraction tests. No private or live-mail fixtures."""

import imaplib
import json
import os
import sqlite3
import stat
import tempfile
import unittest
from dataclasses import replace
from email import policy
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import Mock, patch

from karpathy_wiki_ingest import health
from karpathy_wiki_ingest.shared import TargetedAnonymizer
from karpathy_wiki_ingest_email import cycle, extract, main, publish
from karpathy_wiki_ingest_email.extract import identity, parse, render, text_body
from karpathy_wiki_ingest_email.mailbox import authorized, checked, connection, literal, receive
from karpathy_wiki_ingest_email.settings import Settings, address
from karpathy_wiki_ingest_email.store import Store, digest

RECIPIENT = "server+fs-wiki@example.com"
REDACTIONS = {"people": [{"values": ["Secret Person"], "replacement": "[PERSON]"}]}


def mail(
    *,
    delivery=RECIPIENT,
    to="reader@example.com",
    sender="sender@example.com",
    body="Secret Person wrote this.",
    html=False,
    attachment=False,
    received=None,
):
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = sender
    message["To"] = to
    message["Subject"] = "Secret Person’s notes"
    message["Message-ID"] = "<example-message@example.com>"
    message["Date"] = "Tue, 13 Oct 2026 12:00:00 +0000"
    if delivery is not None:
        message["X-Original-To"] = delivery
    if received:
        message["Received"] = received
    message.set_content(body, subtype="html" if html else "plain")
    if attachment:
        message.add_attachment(
            b"ATTACHMENT NEVER EXTRACTED",
            maintype="application",
            subtype="pdf",
            filename="Secret Person.pdf",
        )
    return message.as_bytes()


class FakeIMAP:
    def __init__(self, messages=None, epoch="1"):
        self.messages = dict(messages or {"1": mail()})
        self.capabilities: tuple[bytes, ...] = (b"IMAP4REV1", b"UIDPLUS")
        self.epoch = epoch
        self.commands = []
        self.fail_delete = False
        self.on_store = lambda uid: None

    def select(self, folder):
        self.commands.append(("SELECT", folder))
        return "OK", [b"1"]

    def capability(self):
        return "OK", [b" ".join(self.capabilities)]

    def response(self, name):
        return name, [self.epoch.encode()]

    def uid(self, command, *args):
        self.commands.append((command, *args))
        uid = args[0]
        if command == "SEARCH":
            return "OK", [" ".join(self.messages).encode()]
        if command == "FETCH":
            raw = self.messages[uid]
            if "RFC822.SIZE" in args[1]:
                return "OK", [f"1 (UID {uid} RFC822.SIZE {len(raw)})".encode()]
            return "OK", [(f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        if command == "STORE":
            self.on_store(uid)
            return "OK", [b""]
        if command == "EXPUNGE":
            if self.fail_delete:
                return "NO", [b"secret server response"]
            self.messages.pop(uid, None)
            return "OK", [b""]
        raise AssertionError(command)


class EmailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.redactions = self.root / "redactions.json"
        self.redactions.write_text(json.dumps(REDACTIONS))
        self.anonymizer = TargetedAnonymizer.from_file(self.redactions)
        self.settings = Settings(
            host="imap.example.com",
            username="wiki@example.com",
            password="private-password",
            recipient=RECIPIENT,
            delivery_header="X-Original-To",
            trusted=True,
            archive=self.root / "private",
            sanitized=self.root / "sources",
            redactions=self.redactions,
            health=self.root / "health.json",
        )
        self.store = Store(self.settings.archive)
        self.addCleanup(lambda: self.store.close())

    def seed(self, raw=None, uid="1"):
        raw = raw or mail()
        return self.store.archive(raw, identity(parse(raw)), "scope", "1", uid)

    def manifest(self):
        return json.loads((self.settings.sanitized / "manifest.json").read_text())

    def test_exact_plus_delivery_not_to_cc_bcc_authorizes(self):
        for field in ("To", "Cc", "Bcc"):
            message = parse(mail(delivery=None))
            if field == "To":
                message.replace_header(field, RECIPIENT)
            else:
                message[field] = RECIPIENT
            self.assertFalse(authorized(message.as_bytes(), self.settings))
            message["X-Original-To"] = RECIPIENT
            self.assertTrue(authorized(message.as_bytes(), self.settings))
        for destination in (
            "server@example.com",
            "server+other@example.com",
            "SERVER+fs-wiki@example.com",
        ):
            self.assertFalse(authorized(mail(delivery=destination), self.settings))
        self.assertTrue(authorized(mail(delivery="server+fs-wiki@EXAMPLE.COM"), self.settings))

    def test_missing_ambiguous_duplicate_and_untrusted_delivery_rejected(self):
        self.assertFalse(authorized(mail(delivery=None), self.settings))
        self.assertFalse(
            authorized(mail(delivery=RECIPIENT + ", other@example.com"), self.settings)
        )
        raw = b"X-Original-To: " + RECIPIENT.encode() + b"\r\n" + mail()
        self.assertFalse(authorized(raw, self.settings))
        self.assertFalse(authorized(mail(), replace(self.settings, trusted=False)))
        with self.assertRaises(ValueError):
            authorized(mail(delivery="not-an-address"), self.settings)

    def test_optional_sender_allowlist_requires_single_exact_from(self):
        restricted = replace(self.settings, allowlist=("sender@example.com",))
        self.assertTrue(authorized(mail(), restricted))
        self.assertFalse(authorized(mail(sender="other@example.com"), restricted))
        self.assertFalse(
            authorized(mail(sender="sender@example.com, other@example.com"), restricted)
        )
        self.assertTrue(authorized(mail(sender="Allowed Name <sender@example.com>"), restricted))

    def test_plain_preferred_and_attachments_and_embedded_mail_not_extracted(self):
        message = parse(mail(attachment=True))
        message.add_attachment(parse(mail(body="NESTED SECRET")))
        text = render(message.as_bytes(), "opaque", self.anonymizer)
        self.assertIn("[PERSON]", text)
        self.assertNotIn("Secret Person", text)
        self.assertNotIn("ATTACHMENT NEVER EXTRACTED", text)
        self.assertNotIn("NESTED SECRET", text)
        alternative = parse(mail(body="plain preferred"))
        alternative.add_alternative("<p>HTML not preferred</p>", subtype="html")
        self.assertIn("plain preferred", text_body(alternative))

    def test_html_converted_before_redaction_without_active_content(self):
        raw = mail(
            html=True,
            body=(
                "<p>Secret <b>Person</b> &amp; notes</p><script>HIDDEN</script>"
                '<style>HIDDEN</style><img src="https://example.com/tracker"><p>tail</p>'
            ),
        )
        text = render(raw, "opaque", self.anonymizer)
        self.assertIn("[PERSON] & notes", text)
        self.assertNotIn("HIDDEN", text)
        self.assertNotIn("tracker", text)
        self.assertNotIn("<p>", text)

    def test_all_decoded_metadata_including_bcc_is_redacted(self):
        message = parse(mail())
        message["Bcc"] = "Secret Person <hidden@example.com>"
        message["X-Private-Note"] = "Secret Person"
        text = render(message.as_bytes(), "opaque", self.anonymizer)
        self.assertIn("Bcc: [PERSON] <hidden@example.com>", text)
        self.assertIn("X-Private-Note: [PERSON]", text)
        self.assertNotIn("Secret Person", text)

    def test_empty_malformed_and_bad_charset_fail_locally(self):
        cases = [
            mail(body="   "),
            b"Content-Type: multipart/mixed; boundary=missing\r\n\r\nbroken",
            b"Content-Type: text/plain; charset=ascii\r\n\r\n\xff",
            b"Content-Type: application/pdf\r\n\r\ndata",
        ]
        for raw in cases:
            with self.subTest(raw=raw[:20]), self.assertRaises((ValueError, UnicodeError)):
                text_body(parse(raw))

    def test_original_bytes_durable_and_private_before_server_store(self):
        raw = mail(attachment=True)
        client = FakeIMAP({"1": raw})

        def on_store(uid):
            delivery = self.store.db.execute("SELECT * FROM deliveries").fetchone()
            self.assertIsNotNone(delivery)
            self.assertEqual(self.store.read(delivery["original"]), raw)
            other = sqlite3.connect(self.settings.archive / "mail.sqlite3")
            self.assertEqual(other.execute("SELECT COUNT(*) FROM deliveries").fetchone()[0], 1)
            other.close()

        client.on_store = on_store
        with patch("karpathy_wiki_ingest_email.store.os.fsync", wraps=os.fsync) as sync:
            self.assertEqual(receive(client, self.settings, self.store), (1, 0))
            self.assertGreaterEqual(sync.call_count, 2)
        self.assertEqual(stat.S_IMODE(self.settings.archive.stat().st_mode), 0o700)
        original = next(self.settings.archive.glob("*.eml"))
        self.assertEqual(stat.S_IMODE(original.stat().st_mode), 0o600)
        self.assertEqual(original.read_bytes(), raw)
        self.assertTrue(
            any(command[0] == "EXPUNGE" and command[1] == "1" for command in client.commands)
        )
        self.assertTrue(any("BODY.PEEK" in str(command) for command in client.commands))

    def test_no_uidplus_or_uidvalidity_means_no_fetch_or_delete(self):
        client = FakeIMAP()
        client.capabilities = (b"IMAP4REV1",)
        with self.assertRaises(ValueError):
            receive(client, self.settings, self.store)
        self.assertFalse(client.commands)
        client.capabilities = (b"UIDPLUS",)
        client.epoch = ""
        with self.assertRaises(ValueError):
            receive(client, self.settings, self.store)
        self.assertFalse(any(command[0] == "STORE" for command in client.commands))

    def test_unmatched_allowlist_and_oversized_are_not_archived_or_deleted(self):
        messages = {
            "1": mail(delivery="other@example.com"),
            "2": mail(sender="other@example.com"),
            "3": mail(body="x" * 10000),
            "4": mail(),
        }
        settings = replace(self.settings, max_bytes=2000, allowlist=("sender@example.com",))
        client = FakeIMAP(messages)
        self.assertEqual(receive(client, settings, self.store), (1, 1))
        self.assertEqual(set(client.messages), {"1", "2", "3"})
        self.assertEqual(len(list(self.settings.archive.glob("*.eml"))), 1)

    def test_cursor_rotation_prevents_poison_and_nonmatching_starvation(self):
        client = FakeIMAP(
            {"1": mail(body="x" * 10000), "2": mail(delivery="other@example.com"), "3": mail()}
        )
        settings = replace(self.settings, batch=1, max_bytes=2000)
        self.assertEqual(receive(client, settings, self.store), (0, 1))
        self.assertEqual(receive(client, settings, self.store), (1, 0))
        self.assertEqual(receive(client, settings, self.store), (0, 0))
        self.assertEqual(receive(client, settings, self.store), (0, 0))
        self.assertEqual(receive(client, settings, self.store), (0, 1))

    def test_growing_backlog_reserves_newest_slots_without_skipping_old_mail(self):
        client = FakeIMAP({str(uid): mail(delivery="other@example.com") for uid in range(1, 101)})
        client.messages["3"] = mail(body="old matching message")
        settings = replace(self.settings, batch=4)
        for poll in range(6):
            with self.subTest(poll=poll):
                first = 101 + 4 * poll
                for uid in range(first, first + 4):
                    client.messages[str(uid)] = mail(delivery="other@example.com")
                latest = str(first + 3)
                client.messages[latest] = mail(body=f"new message {latest}")
                client.commands.clear()
                self.assertEqual(receive(client, settings, self.store), (2 if poll == 1 else 1, 0))
                fetched = [
                    command[1]
                    for command in client.commands
                    if command[0] == "FETCH" and "RFC822.SIZE" in command[2]
                ]
                self.assertEqual(
                    fetched, [str(2 * poll + 1), str(2 * poll + 2), latest, str(first + 2)]
                )
                self.assertNotIn(latest, client.messages)
                # Both lane cursors survive actual process-store restart.
                self.store.close()
                self.store = Store(self.settings.archive)
        self.assertNotIn("3", client.messages)

    def test_single_slot_alternates_newest_and_backlog_across_restarts(self):
        client = FakeIMAP({str(uid): mail(delivery="other@example.com") for uid in range(1, 21)})
        client.messages["3"] = mail(body="old matching message")
        settings = replace(self.settings, batch=1)
        for poll in range(6):
            latest = str(21 + poll)
            client.messages[latest] = mail(body=f"new message {latest}")
            client.commands.clear()
            self.assertEqual(
                receive(client, settings, self.store), (1 if poll % 2 or poll == 4 else 0, 0)
            )
            fetched = [
                command[1]
                for command in client.commands
                if command[0] == "FETCH" and "RFC822.SIZE" in command[2]
            ]
            self.assertEqual(fetched, [latest if poll % 2 else str(poll // 2 + 1)])
            self.store.close()
            self.store = Store(self.settings.archive)
        self.assertNotIn("3", client.messages)

    def test_archive_write_or_state_failure_never_flags_message(self):
        for failure in (OSError("private data"), sqlite3.OperationalError("private data")):
            client = FakeIMAP()
            with patch.object(self.store, "archive", side_effect=failure):
                if isinstance(failure, sqlite3.Error):
                    with self.assertRaises(sqlite3.Error):
                        receive(client, self.settings, self.store)
                else:
                    self.assertEqual(receive(client, self.settings, self.store), (0, 1))
            self.assertFalse(any(command[0] == "STORE" for command in client.commands))

    def test_fsync_failure_keeps_message_and_creates_no_committed_original(self):
        client = FakeIMAP()
        with patch("karpathy_wiki_ingest_email.store.fsync_directory", side_effect=OSError):
            self.assertEqual(receive(client, self.settings, self.store), (0, 1))
        self.assertFalse(any(command[0] == "STORE" for command in client.commands))
        self.assertFalse(self.store.sources())

    def test_pending_delete_restarts_without_fetch_or_duplicate_extraction(self):
        client = FakeIMAP()
        client.fail_delete = True
        self.assertEqual(receive(client, self.settings, self.store), (1, 1))
        self.assertEqual(extract(self.store, self.anonymizer, 25), 0)
        publish(self.store, self.settings.sanitized, self.anonymizer)
        first = self.manifest()
        client.fail_delete = False
        client.commands.clear()
        # Release/reopen the actual persisted ledger, as after daemon restart.
        self.store.close()
        self.store = Store(self.settings.archive)
        self.assertEqual(receive(client, self.settings, self.store), (0, 0))
        self.assertFalse(any(command[0] == "FETCH" for command in client.commands))
        self.assertEqual(len(self.store.sources()), 1)
        publish(self.store, self.settings.sanitized, self.anonymizer)
        self.assertEqual(self.manifest(), first)

    def test_corrupt_archive_never_grants_pending_deletion(self):
        client = FakeIMAP()
        client.fail_delete = True
        receive(client, self.settings, self.store)
        next(self.settings.archive.glob("*.eml")).write_bytes(b"corrupt")
        client.commands.clear()
        self.assertEqual(receive(client, self.settings, self.store), (0, 1))
        self.assertFalse(any(command[0] == "STORE" for command in client.commands))

    def test_uidvalidity_reuse_archives_new_bytes_instead_of_old_delete(self):
        client = FakeIMAP()
        client.fail_delete = True
        receive(client, self.settings, self.store)
        new = mail(body="new epoch contents")
        client.messages = {"1": new}
        client.epoch = "2"
        client.fail_delete = False
        self.assertEqual(receive(client, self.settings, self.store), (1, 0))
        self.assertEqual(len(self.store.sources()), 2)
        self.assertIn(new, [path.read_bytes() for path in self.settings.archive.glob("*.eml")])

    def test_pending_delete_rechecks_current_allowlist_and_delivery_header(self):
        client = FakeIMAP()
        client.fail_delete = True
        receive(client, self.settings, self.store)
        client.fail_delete = False
        for settings in (
            replace(self.settings, allowlist=("other@example.com",)),
            replace(self.settings, delivery_header="Delivered-To"),
        ):
            client.commands.clear()
            self.assertEqual(receive(client, settings, self.store), (0, 0))
            self.assertFalse(any(command[0] == "STORE" for command in client.commands))
            self.assertIn("1", client.messages)
        self.assertEqual(receive(client, self.settings, self.store), (0, 0))
        self.assertFalse(client.messages)

    def test_pathological_mime_cannot_block_archive_or_delete(self):
        raw = (
            f"X-Original-To: {RECIPIENT}\r\nFrom: sender@example.com\r\n"
            "Content-Type: multipart/mixed; boundary=b0\r\n\r\n"
        ).encode()
        for index in range(300):
            raw += (
                f"--b{index}\r\nContent-Type: multipart/mixed; boundary=b{index + 1}\r\n\r\n"
            ).encode()
        raw += b"text"
        for index in reversed(range(301)):
            raw += f"\r\n--b{index}--\r\n".encode()
        client = FakeIMAP({"1": raw})
        self.assertEqual(receive(client, self.settings, self.store), (1, 0))
        self.assertFalse(client.messages)
        self.assertEqual(next(self.settings.archive.glob("*.eml")).read_bytes(), raw)
        self.assertEqual(extract(self.store, self.anonymizer, 25), 1)

    def test_identity_exception_follows_durable_archive_with_extra_ledger(self):
        self.assertEqual(self.store.db.execute("PRAGMA synchronous").fetchone()[0], 3)
        raw = mail()

        def failing_identity(message):
            self.assertEqual(next(self.settings.archive.glob("*.eml")).read_bytes(), raw)
            raise RecursionError

        with patch("karpathy_wiki_ingest_email.store.message_identity", new=failing_identity):
            self.assertEqual(receive(FakeIMAP({"1": raw}), self.settings, self.store), (1, 0))
        self.assertEqual(self.store.sources()[0]["identity"], "raw:" + digest(raw))

    def test_transit_duplicates_archive_all_originals_but_one_source(self):
        first, second = mail(received="first hop"), mail(received="second hop")
        self.assertNotEqual(digest(first), digest(second))
        self.assertEqual(identity(parse(first)), identity(parse(second)))
        receive(FakeIMAP({"1": first, "2": second}), self.settings, self.store)
        self.assertEqual(len(self.store.sources()), 1)
        self.assertEqual(len(list(self.settings.archive.glob("*.eml"))), 2)
        self.assertEqual(extract(self.store, self.anonymizer, 25), 0)
        publish(self.store, self.settings.sanitized, self.anonymizer)
        self.assertEqual(len(self.manifest()["items"]), 1)
        self.assertNotEqual(identity(parse(first)), identity(parse(mail(body="changed"))))

    def test_identical_raw_duplicates_do_not_overwrite_or_duplicate_archive(self):
        first = self.seed()
        second = self.seed(uid="2")
        self.assertEqual(first, second)
        self.assertEqual(len(list(self.settings.archive.glob("*.eml"))), 1)
        self.assertEqual(len(self.store.sources()), 1)

    def test_local_failure_stays_replayable_after_server_deletion(self):
        client = FakeIMAP({"1": mail(body=" ")})
        self.assertEqual(receive(client, self.settings, self.store), (1, 0))
        self.assertFalse(client.messages)
        self.assertEqual(extract(self.store, self.anonymizer, 25), 1)
        self.assertEqual(extract(self.store, self.anonymizer, 25), 1)
        publish(self.store, self.settings.sanitized, self.anonymizer)
        self.assertEqual(self.manifest()["items"], [])
        self.assertEqual(len(self.manifest()["errors"]), 1)
        self.assertNotIn("Secret Person", json.dumps(self.manifest()))
        for _ in range(4):
            self.store.failed(self.store.sources()[0]["id"], "ValueError")
        self.assertEqual(extract(self.store, self.anonymizer, 25), 1)
        self.store.rearm(None)
        self.assertEqual(self.store.sources()[0]["attempts"], 0)

    def test_malformed_body_archived_before_delete_and_quarantined_locally(self):
        raw = (
            f"X-Original-To: {RECIPIENT}\r\nFrom: sender@example.com\r\n"
            "Content-Type: multipart/mixed; boundary=missing\r\n\r\nbroken"
        ).encode()
        client = FakeIMAP({"1": raw})
        self.assertEqual(receive(client, self.settings, self.store), (1, 0))
        self.assertFalse(client.messages)
        self.assertEqual(extract(self.store, self.anonymizer, 25), 1)
        self.assertEqual(next(self.settings.archive.glob("*.eml")).read_bytes(), raw)

    def test_manifest_opaque_identifiers_and_publication_idempotency(self):
        self.seed()
        extract(self.store, self.anonymizer, 25)
        self.assertEqual(publish(self.store, self.settings.sanitized, self.anonymizer), 0)
        manifest = self.manifest()
        item = manifest["items"][0]
        text = (self.settings.sanitized / item["source_path"]).read_text()
        self.assertNotIn("Secret Person", text)
        self.assertNotIn(RECIPIENT, json.dumps(manifest))
        self.assertNotIn("example-message", json.dumps(manifest))
        self.assertEqual(item["source_sha256"], digest(text.encode()))
        self.assertEqual(item["wiki_path"], f"email/{item['source_key']}/index.md")
        self.assertEqual(publish(self.store, self.settings.sanitized, self.anonymizer), 0)
        self.assertEqual(self.manifest(), manifest)

    def test_stricter_redaction_rechecks_projection_and_prunes_old_generation(self):
        self.seed(mail(body="Secret Person has a NEWSECRET"))
        extract(self.store, self.anonymizer, 25)
        publish(self.store, self.settings.sanitized, self.anonymizer)
        old = self.settings.sanitized / self.manifest()["items"][0]["source_path"]
        changed = {
            "people": [{"values": ["Secret Person", "NEWSECRET"], "replacement": "[PERSON]"}]
        }
        self.redactions.write_text(json.dumps(changed))
        publish(self.store, self.settings.sanitized, TargetedAnonymizer.from_file(self.redactions))
        self.assertFalse(old.exists())
        current = self.settings.sanitized / self.manifest()["items"][0]["source_path"]
        self.assertNotIn("NEWSECRET", current.read_text())

    def test_replay_changes_revision_not_source_identity_and_uses_no_network(self):
        self.seed()
        extract(self.store, self.anonymizer, 25)
        publish(self.store, self.settings.sanitized, self.anonymizer)
        first = self.manifest()["items"][0]
        self.redactions.write_text(
            json.dumps({"people": [{"values": ["Secret Person"], "replacement": "[CHANGED]"}]})
        )
        self.store.close()
        with patch(
            "karpathy_wiki_ingest_email.connection", side_effect=AssertionError("network forbidden")
        ):
            self.assertEqual(cycle(self.settings, replay=True, source_id=first["source_key"]), 0)
        self.store = Store(self.settings.archive)
        second = self.manifest()["items"][0]
        self.assertEqual(second["source_key"], first["source_key"])
        self.assertEqual(second["wiki_path"], first["wiki_path"])
        self.assertNotEqual(second["source_revision"], first["source_revision"])
        with self.assertRaises(ValueError):
            self.store.rearm("unknown")

    def test_failed_replay_retains_last_safe_projection_and_immutable_file(self):
        self.seed()
        extract(self.store, self.anonymizer, 25)
        publish(self.store, self.settings.sanitized, self.anonymizer)
        first = self.manifest()["items"][0]
        original_path = self.settings.sanitized / first["source_path"]
        original_text = original_path.read_text()
        self.store.rearm(first["source_key"])
        with patch("karpathy_wiki_ingest_email.render", side_effect=ValueError("private")):
            self.assertEqual(extract(self.store, self.anonymizer, 25), 1)
        self.assertEqual(publish(self.store, self.settings.sanitized, self.anonymizer), 1)
        self.assertEqual(self.manifest()["items"], [first])
        self.assertEqual(self.manifest()["revoked"], [])
        self.assertEqual(original_path.read_text(), original_text)
        self.assertEqual(self.manifest()["errors"][0]["error"], "ValueError")
        self.assertEqual(extract(self.store, self.anonymizer, 25), 1)  # retry backoff

    def test_failed_replay_never_retains_projection_rejected_by_current_privacy_gate(self):
        self.seed()
        extract(self.store, self.anonymizer, 25)
        publish(self.store, self.settings.sanitized, self.anonymizer)
        self.store.rearm(None)
        with patch("karpathy_wiki_ingest_email.render", side_effect=ValueError("private")):
            extract(self.store, self.anonymizer, 25)
        rejecting = Mock(spec=self.anonymizer)
        rejecting.anonymize.side_effect = ValueError("private")
        rejecting.fingerprint = "b" * 64
        rejecting.locate.return_value = []
        self.assertGreater(publish(self.store, self.settings.sanitized, rejecting), 0)
        self.assertEqual(self.manifest()["items"], [])
        self.assertEqual(len(self.manifest()["revoked"]), 1)

    def test_mime_and_manifest_privacy_failures_revoke_source(self):
        self.seed()
        extract(self.store, self.anonymizer, 25)
        publish(self.store, self.settings.sanitized, self.anonymizer)
        rejecting = Mock(spec=self.anonymizer)
        rejecting.anonymize.side_effect = ValueError("private")
        rejecting.fingerprint = "a" * 64
        rejecting.locate.return_value = []
        self.assertEqual(publish(self.store, self.settings.sanitized, rejecting), 1)
        self.assertEqual(self.manifest()["items"], [])
        self.assertEqual(len(self.manifest()["revoked"]), 1)
        rejecting.locate.return_value = ["deny-match"]
        with self.assertRaises(ValueError):
            publish(self.store, self.settings.sanitized, rejecting)

    def test_lock_and_original_integrity_fail_closed(self):
        with self.assertRaises(BlockingIOError):
            Store(self.settings.archive)
        with self.assertRaises(ValueError):
            self.store.read("../bad")
        original = self.seed()
        path = self.settings.archive / f"{original}.eml"
        path.unlink()
        path.symlink_to(self.redactions)
        with self.assertRaises(OSError):
            self.store.read(original)

    def test_pending_extraction_limit_and_published_integrity(self):
        self.seed(uid="1")
        self.seed(mail(body="other"), uid="2")
        self.assertEqual(extract(self.store, self.anonymizer, 1), 0)
        self.assertEqual(sum(row["output"] is not None for row in self.store.sources()), 1)
        extract(self.store, self.anonymizer, 25)
        publish(self.store, self.settings.sanitized, self.anonymizer)
        item = self.manifest()["items"][0]
        (self.settings.sanitized / item["source_path"]).write_text("tampered")
        with self.assertRaises(ValueError):
            publish(self.store, self.settings.sanitized, self.anonymizer)

    def test_checked_and_literal_reject_unsafe_fetch(self):
        with self.assertRaises(OSError):
            checked(("NO", ["private"]))
        for response in (
            [],
            [(b"BODY[]", b"raw")],
            [(b"UID 2 BODY[]", b"raw")],
            [(b"UID 1", "not bytes")],
        ):
            with self.assertRaises(ValueError):
                literal(response, "1")

    def test_tls_login_logout_no_close_even_on_error(self):
        client = Mock()
        client.login.return_value = ("OK", [])
        client.logout.side_effect = imaplib.IMAP4.error("private server data")
        with patch(
            "karpathy_wiki_ingest_email.mailbox.imaplib.IMAP4_SSL", return_value=client
        ) as factory:
            with connection(self.settings) as actual:
                self.assertIs(actual, client)
            context = factory.call_args.kwargs["ssl_context"]
            self.assertTrue(context.check_hostname)
            client.close.assert_not_called()
            client.logout.assert_called_once()

    def test_server_outage_still_extracts_local_originals(self):
        self.seed()
        self.store.close()
        with patch(
            "karpathy_wiki_ingest_email.connection",
            side_effect=imaplib.IMAP4.error("private-password"),
        ):
            with self.assertLogs("karpathy-wiki-email") as logs:
                self.assertGreater(cycle(self.settings), 0)
        self.store = Store(self.settings.archive)
        self.assertEqual(len(self.manifest()["items"]), 1)
        self.assertNotIn("private-password", " ".join(logs.output))

    def test_cli_once_replay_and_content_free_failure_health(self):
        with (
            patch("karpathy_wiki_ingest_email.Settings.from_env", return_value=self.settings),
            patch("sys.argv", ["email", "--once"]),
            patch("karpathy_wiki_ingest_email.cycle", return_value=0),
        ):
            main()
        self.assertEqual(json.loads(self.settings.health.read_text())["failed"], 0)
        with (
            patch("karpathy_wiki_ingest_email.Settings.from_env", return_value=self.settings),
            patch("sys.argv", ["email", "--replay", "all"]),
            patch("karpathy_wiki_ingest_email.cycle", side_effect=ValueError("private-password")),
            self.assertLogs("karpathy-wiki-email") as logs,
            self.assertRaises(SystemExit),
        ):
            main()
        self.assertNotIn("private-password", " ".join(logs.output))
        self.assertEqual(json.loads(self.settings.health.read_text())["failed"], 1)


class SettingsTests(unittest.TestCase):
    def env(self):
        return {
            "EMAIL_IMAP_HOST": "imap.example.com",
            "EMAIL_IMAP_USERNAME": "wiki@example.com",
            "EMAIL_IMAP_PASSWORD": " password with spaces ",
            "EMAIL_RECIPIENT": RECIPIENT,
            "EMAIL_DELIVERY_HEADER": "X-Original-To",
            "EMAIL_DELIVERY_HEADER_TRUSTED": "1",
            "REDACTIONS_FILE": "/run/secrets/redactions",
        }

    def test_env_tls_defaults_and_password_not_stripped(self):
        with patch.dict(os.environ, self.env(), clear=True):
            settings = Settings.from_env()
        self.assertEqual(settings.port, 993)
        self.assertEqual(settings.password, " password with spaces ")
        self.assertEqual(settings.interval, 60)
        self.assertEqual(settings.allowlist, ())

    def test_unsafe_env_rejected(self):
        for key, value in [
            ("EMAIL_DELIVERY_HEADER_TRUSTED", "0"),
            ("EMAIL_DELIVERY_HEADER", "To"),
            ("EMAIL_DELIVERY_HEADER", "X\r\nInjected"),
            ("EMAIL_IMAP_PASSWORD", ""),
            ("EMAIL_BATCH_SIZE", "0"),
            ("EMAIL_RECIPIENT", "a@example.com,b@example.com"),
            ("EMAIL_FROM_ALLOWLIST", "invalid"),
        ]:
            with (
                self.subTest(key=key),
                patch.dict(os.environ, {**self.env(), key: value}, clear=True),
                self.assertRaises(ValueError),
            ):
                Settings.from_env()

    def test_overlapping_roots_fail_and_offline_needs_no_mail_credentials(self):
        with patch.dict(os.environ, {"REDACTIONS_FILE": "/run/secrets/redactions"}, clear=True):
            self.assertEqual(Settings.from_env(offline=True).host, "")
        with (
            patch.dict(
                os.environ,
                {
                    **self.env(),
                    "EMAIL_ARCHIVE_ROOT": "/data/sources/private",
                    "EMAIL_SANITIZED_ROOT": "/data/sources",
                },
                clear=True,
            ),
            self.assertRaises(ValueError),
        ):
            Settings.from_env()

    def test_health_matches_email_duration(self):
        with patch.dict(os.environ, {"EMAIL_POLL_INTERVAL": "5m"}, clear=True):
            self.assertEqual(health.interval(), 300)

    def test_addresses_are_exact_not_substrings(self):
        self.assertEqual(address("server+tag@EXAMPLE.COM"), "server+tag@example.com")
        with self.assertRaises(ValueError):
            address("Display Name <server+tag@example.com>")
