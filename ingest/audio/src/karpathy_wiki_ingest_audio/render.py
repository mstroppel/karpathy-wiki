"""Sanitized Markdown rendering for transcribed audio sources.

Frontmatter and body carry only sanitized, validated metadata: the redacted
recording title, the detected or configured language, the sanitized relative
WebDAV origin, the source audio SHA-256, the processing backend/model and
options, and per-segment entries ``[hh:mm:ss-hh:mm:ss] Speaker N: text``.
Raw audio, decoded audio, and unredacted transcript text never enter this
document; the publisher anonymizes the whole rendered document again as a
final pass.
"""

from __future__ import annotations

import json
from collections import Counter

from karpathy_wiki_speech.types import Segment

SOURCE_FORMAT_VERSION = 1


def format_timestamp(ms: int) -> str:
    """``00:01:23`` style timestamp; hours are included, never dropped."""
    total_seconds = max(ms, 0) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def sanitize_title(title: str, fallback: str) -> str:
    collapsed = " ".join(title.split())
    return collapsed[:120] or fallback


def render_transcript_document(
    title: str,
    language: str | None,
    origin: str,
    audio_sha256: str,
    segments: list[Segment],
    backend: str,
    model: str,
    options: dict[str, object],
    counts: Counter[str],
) -> str:
    """Render frontmatter plus the redacted, timestamped transcript."""
    safe_title = sanitize_title(title, "Transkript")
    count_text = ", ".join(f"{kind}: {counts[kind]}" for kind in sorted(counts)) or "keine"
    entries = []
    for segment in segments:
        stamp = f"[{format_timestamp(segment.start_ms)}-{format_timestamp(segment.end_ms)}]"
        if segment.speaker_id is None:
            speaker = ""
        elif segment.speaker_id < 0:
            # Ambiguous or overlapping speech: never invent a speaker.
            speaker = "Unbekannt: "
        else:
            speaker = f"Sprecher {segment.speaker_id + 1}: "
        text = " ".join(segment.text.split())
        if text:
            entries.append(f"{stamp} {speaker}{text}")
        elif segment.speaker_id is not None:
            # A fully redacted segment keeps its timing entry without text:
            # the matched words were removed, not duplicated.
            entries.append(stamp)
    body = "\n".join(entries) or "(leeres Transkript)"
    return (
        "---\n"
        f"source_adapter: audio\n"
        f"source_audio_sha256: {json.dumps(audio_sha256)}\n"
        f"source_language: {json.dumps(language) if language else 'null'}\n"
        f"source_origin: {json.dumps(origin, ensure_ascii=False)}\n"
        f"speech_backend: {json.dumps(backend)}\n"
        f"speech_model: {json.dumps(model)}\n"
        f"speech_options: {json.dumps(options, ensure_ascii=False, sort_keys=True)}\n"
        "anonymized: true\n"
        "machine_generated: true\n"
        "---\n\n"
        f"# {safe_title}\n\n"
        "Dieses Transkript wurde automatisch lokal erzeugt (Sprecheretiketten und "
        "Text sind maschinengeneriert) und sollte von einem Menschen geprüft werden.\n\n"
        f"Relative WebDAV-Herkunft: {json.dumps(origin, ensure_ascii=False)}\n\n"
        f"Ersetzte konfigurierte Werte: {count_text}.\n\n"
        f"{body}\n"
    )
