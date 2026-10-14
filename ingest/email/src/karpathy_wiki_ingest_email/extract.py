"""MIME body selection and local text rendering. Never fetch external resources."""

from __future__ import annotations

import hashlib
import json
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from html.parser import HTMLParser

from karpathy_wiki_ingest.shared import TargetedAnonymizer

EXTRACTION_VERSION = "email-text-v1"


def parse(raw: bytes) -> EmailMessage:
    return BytesParser(policy=policy.default).parsebytes(raw)


def identity(message: EmailMessage) -> str:
    """Deduplicate identical MIME content, not transit headers or Message-ID alone.

    Reused Message-IDs with different content remain distinct. Every differing
    raw delivery is still archived before deletion, even when its source exists.
    """
    headers = [
        (name.lower(), str(value))
        for name, value in message.items()
        if name.lower()
        in {"from", "to", "cc", "bcc", "date", "subject", "message-id", "mime-version"}
        or name.lower().startswith("content-")
    ]
    payload = message.get_payload()
    content: object
    if isinstance(payload, list):
        content = [part.as_bytes(policy=policy.default).hex() for part in message.iter_parts()]
    else:
        content = payload
    return hashlib.sha256(
        json.dumps([headers, content], ensure_ascii=False).encode("utf-8")
    ).hexdigest()


class TextHTML(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden and tag in {"br", "p", "div", "li", "tr", "h1", "h2", "h3"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.hidden:
            self.hidden -= 1
        if not self.hidden and tag in {"p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def text_body(message: EmailMessage) -> str:
    if any(part.defects for part in message.walk()):
        raise ValueError("malformed MIME")
    part = message.get_body(preferencelist=("plain", "html"))
    if part is None or part.get_content_type() not in {"text/plain", "text/html"}:
        raise ValueError("no supported message body")
    raw = part.get_payload(decode=True)
    if not isinstance(raw, bytes):
        raise ValueError("body is not bytes")
    text = raw.decode(part.get_content_charset() or "ascii", errors="strict")
    if part.get_content_type() == "text/html":
        parser = TextHTML()
        parser.feed(text)
        parser.close()
        text = "".join(parser.parts)
    if not text.strip():
        raise ValueError("empty message body")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def render(raw: bytes, source_id: str, anonymizer: TargetedAnonymizer) -> str:
    message = parse(raw)
    # Decode MIME/header encodings BEFORE redaction; include BCC and delivery
    # provenance as requested. Header values are data, never agent instructions.
    metadata = "\n".join(f"- {name}: {str(value)}" for name, value in message.items())
    document = (
        f"# Email source {source_id}\n\n"
        "## Message metadata\n\n"
        f"{metadata}\n\n## Message text\n\n{text_body(message)}\n"
    )
    return anonymizer.anonymize(document)[0]
