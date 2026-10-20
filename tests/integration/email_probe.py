"""Disposable TLS IMAP protocol probe with synthetic mail and real provider code."""

import json
import socketserver
import sqlite3
import ssl
import subprocess
import sys
import threading
from dataclasses import replace
from email import policy
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from karpathy_wiki_ingest_email import cycle
from karpathy_wiki_ingest_email.mailbox import receive
from karpathy_wiki_ingest_email.settings import Settings

ROOT = Path("/data")
RECIPIENT = "server+fs-wiki@example.com"


def fixture(
    field: str,
    *,
    sender: str = "sender@example.com",
    recipient: str = RECIPIENT,
    note: str = "",
) -> bytes:
    message = EmailMessage(policy=policy.SMTP)
    message["From"] = sender
    message["Subject"] = "Secret Person integration fixture"
    # Received BCC mail has no BCC header. No delivery headers are needed.
    message["To" if field == "Bcc" else field] = (
        "reader@example.com" if field == "Bcc" else recipient
    )
    message.set_content(f"Secret Person approved the {field} fixture.{note}")
    message.add_attachment(b"PRIVATE ATTACHMENT", maintype="application", subtype="pdf")
    return message.as_bytes()


MESSAGES = {str(index): fixture(field) for index, field in enumerate(("To", "Cc", "Bcc"), 1)}
# Differing/duplicate delivery metadata must not filter folder mail. Another
# client's non-allowlisted, deleted message must survive targeted deletion;
# general EXPUNGE/CLOSE are deliberately unsupported.
MESSAGES["4"] = b"X-Original-To: other@example.com\r\nX-Original-To: invalid\r\n" + fixture(
    "Delivered-To"
)
MESSAGES["5"] = fixture("To", sender="other@example.com")
COMMANDS: list[str] = []
FIRST_DELETE = True


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        global FIRST_DELETE
        self.wfile.write(b"* OK disposable fixture ready\r\n")
        while line := self.rfile.readline():
            text = line.decode("ascii").strip()
            COMMANDS.append(text)
            tag, command, *args = text.split()
            command = command.upper()
            if command == "CAPABILITY":
                self.wfile.write(b"* CAPABILITY IMAP4rev1 UIDPLUS\r\n")
            elif command == "LOGIN":
                pass
            elif command == "SELECT":
                self.wfile.write(b"* 5 EXISTS\r\n* OK [UIDVALIDITY 17] epoch\r\n")
            elif command == "UID":
                operation = args[0].upper()
                if operation == "SEARCH":
                    self.wfile.write(b"* SEARCH " + " ".join(MESSAGES).encode() + b"\r\n")
                elif operation == "FETCH":
                    uid = args[1]
                    raw = MESSAGES[uid]
                    if "RFC822.SIZE" in text:
                        response = f"* 1 FETCH (UID {uid} RFC822.SIZE {len(raw)})\r\n"
                        self.wfile.write(response.encode())
                    else:
                        response = f"* 1 FETCH (UID {uid} BODY[]<0> {{{len(raw)}}}\r\n"
                        self.wfile.write(response.encode() + raw + b")\r\n")
                elif operation == "STORE":
                    uid = args[1]
                    ledger = sqlite3.connect(ROOT / "incoming/email/mail.sqlite3")
                    delivery = ledger.execute(
                        "SELECT original FROM deliveries WHERE uid=?", (uid,)
                    ).fetchone()
                    ledger.close()
                    assert delivery is not None, "server delete preceded durable ledger"
                    assert (ROOT / f"incoming/email/{delivery[0]}.eml").read_bytes() == MESSAGES[
                        uid
                    ]
                elif operation == "EXPUNGE":
                    uid = args[1]
                    if FIRST_DELETE:
                        FIRST_DELETE = False
                        self.wfile.write(f"{tag} NO injected targeted-delete failure\r\n".encode())
                        continue
                    MESSAGES.pop(uid, None)
                    self.wfile.write(b"* 1 EXPUNGE\r\n")
                else:
                    raise AssertionError(operation)
            elif command == "LOGOUT":
                self.wfile.write(b"* BYE fixture done\r\n")
                self.wfile.write(f"{tag} OK LOGOUT completed\r\n".encode())
                return
            else:
                raise AssertionError(command)
            self.wfile.write(f"{tag} OK {command} completed\r\n".encode())


def run():
    ROOT.mkdir(exist_ok=True)
    redactions = ROOT / "redactions.json"
    redactions.write_text(
        json.dumps({"people": [{"values": ["Secret Person"], "replacement": "[PERSON]"}]})
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(ROOT / "cert.pem", ROOT / "key.pem")
    client_context = ssl.create_default_context(cafile=str(ROOT / "cert.pem"))
    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    settings = Settings(
        host="localhost",
        port=server.server_address[1],
        username="fixture",
        password="integration-only",
        archive=ROOT / "incoming/email",
        sanitized=ROOT / "sources/email",
        redactions=redactions,
        health=ROOT / "health.json",
        allowlist=("sender@example.com",),
    )
    try:

        def verified_receive(*args):
            try:
                return receive(*args)
            except ValueError as error:
                raise AssertionError("unexpected fixture receive failure") from error

        with (
            patch(
                "karpathy_wiki_ingest_email.mailbox.ssl.create_default_context",
                return_value=client_context,
            ),
            patch("karpathy_wiki_ingest_email.receive", new=verified_receive),
        ):
            assert cycle(settings) > 0  # archive succeeded; injected deletion failure
            first = json.loads((settings.sanitized / "manifest.json").read_text())
            assert len(first["items"]) == 4
            assert cycle(settings) == 0  # persisted pending delete resumes
        assert set(MESSAGES) == {"5"}, "non-allowlisted message was deleted"
        assert len(list(settings.archive.glob("*.eml"))) == 4
        assert all(command.split()[1] not in {"CLOSE", "EXPUNGE"} for command in COMMANDS)
        assert client_context.check_hostname
        for item in first["items"]:
            text = (settings.sanitized / item["source_path"]).read_text()
            assert "Secret Person" not in text
            assert "PRIVATE ATTACHMENT" not in text
        # Explicit offline replay needs no network and preserves revisions/IDs.
        with patch("karpathy_wiki_ingest_email.connection", side_effect=AssertionError("network")):
            assert cycle(replace(settings, host="", username="", password=""), replay=True) == 0
        replayed = json.loads((settings.sanitized / "manifest.json").read_text())
        assert replayed["items"] == first["items"]
        # Exercise the shipped dispatcher/CLI with ONLY offline configuration.
        subprocess.run(
            [sys.executable, "-m", "karpathy_wiki_ingest", "email", "--replay", "all"],
            env={
                "REDACTIONS_FILE": str(settings.redactions),
                "EMAIL_ARCHIVE_ROOT": str(settings.archive),
                "EMAIL_SANITIZED_ROOT": str(settings.sanitized),
                "HEALTH_PATH": str(settings.health),
            },
            check=True,
        )
        assert not json.loads(settings.health.read_text())["failed"]
        # Optional recipient filter on a real TLS connection: decoded To/Cc/Bcc
        # only. Delivery-header-only mail and a BCC copy without recipient
        # headers cannot match and must stay on the server.
        MESSAGES["6"] = fixture("To", recipient="reader@example.com", note=" Unmatched To.")
        MESSAGES["7"] = fixture("Cc", note=" Matched Cc.")
        MESSAGES["8"] = fixture("Delivered-To", note=" Delivery headers never match.")
        with (
            patch(
                "karpathy_wiki_ingest_email.mailbox.ssl.create_default_context",
                return_value=client_context,
            ),
            patch("karpathy_wiki_ingest_email.receive", new=verified_receive),
        ):
            assert cycle(replace(settings, recipients=(RECIPIENT,))) == 0
        # 3 and 4 were already consumed by the sender-filter run; 5 fails the
        # sender filter, and 6/8 have no matching To/Cc/Bcc mailbox.
        assert set(MESSAGES) == {"5", "6", "8"}, f"unexpected server state: {sorted(MESSAGES)}"
        assert len(list(settings.archive.glob("*.eml"))) == 5
        filtered = json.loads((settings.sanitized / "manifest.json").read_text())
        assert len(filtered["items"]) == 5
        (ROOT / "wiki/sources").mkdir(parents=True)
        print("email protocol/archive/restart/replay: OK")
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    run()
