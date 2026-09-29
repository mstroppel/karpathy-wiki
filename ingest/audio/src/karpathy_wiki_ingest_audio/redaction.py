"""Span-aware transcript redaction shared by audio and future providers.

Redaction runs on a continuous text view built across segment boundaries,
with a mapping from text spans back to timed segments. A configured
deny-list match that spans segments removes all matched text and emits its
replacement once; timing and speaker labels are preserved; the original
matched words never remain in any segment.

The shared anonymizer supplies the identical matching rules used by
``TargetedAnonymizer.anonymize()`` through its ``locate()`` method. The
rendered document is anonymized again as a final pass, so residual matches
still fail publication the same way they do for every provider.
"""

from __future__ import annotations

import re
from collections import Counter

from karpathy_wiki_ingest.shared import DenyMatch
from karpathy_wiki_speech.types import Segment

_WHITESPACE_RE = re.compile(r"\s+")


def segment_texts(segments: list[Segment]) -> list[str]:
    return [" ".join(segment.text.split()) for segment in segments]


def continuous_offsets(pieces: list[str]) -> tuple[str, list[tuple[int, int]]]:
    """Build the space-joined view and each piece's ``(start, end)`` offsets.

    The locator returns matches in the coordinates of the joined view;
    offsets are what project them back onto individual segments.
    """
    view = " ".join(pieces)
    offsets: list[tuple[int, int]] = []
    cursor = 0
    for piece in pieces:
        start = view.index(piece, cursor)
        offsets.append((start, start + len(piece)))
        cursor = start + len(piece)
    return view, offsets


def project_matches(
    pieces: list[str],
    offsets: list[tuple[int, int]],
    matches: list[DenyMatch],
) -> list[str]:
    """Apply original-coordinate matches to the pieces they cover.

    A match covering several pieces removes the matched characters from
    every covered piece and inserts its replacement exactly once, in the
    first covered piece. Matches are evaluated in ``anonymize()`` order
    (start position, longer match first) and are non-overlapping by
    construction of the shared rules.
    """
    removed: list[list[tuple[int, int]]] = [[] for _ in pieces]
    replacement_by_piece: dict[int, str] = {}
    for match in sorted(matches, key=lambda match: (match.start, -match.end)):
        coverage = [
            index
            for index, (start, end) in enumerate(offsets)
            if min(match.end, end) - max(match.start, start) > 0
        ]
        if not coverage:
            continue
        first_piece = coverage[0]
        if first_piece not in replacement_by_piece:
            replacement_by_piece[first_piece] = match.replacement
        for index in coverage:
            start, end = offsets[index]
            removed[index].append((max(match.start, start) - start, min(match.end, end) - start))
    output: list[str] = []
    for index, piece in enumerate(pieces):
        spans = removed[index]
        if not spans:
            output.append(piece)
            continue
        replacement = replacement_by_piece.get(index)
        parts: list[str] = []
        cursor = 0
        emitted = False
        for start, end in sorted(set(spans)):
            if start < cursor:
                # Overlapping splice inside one piece: keep the wider removal.
                start = cursor
            if end <= start:
                continue
            parts.append(piece[cursor:start])
            if replacement is not None and not emitted:
                parts.append(replacement)
                emitted = True
            cursor = end
        parts.append(piece[cursor:])
        output.append(_WHITESPACE_RE.sub(" ", "".join(parts)).strip())
    return output


def redact_transcript(
    segments: list[Segment],
    locator: object,
) -> tuple[list[Segment], Counter[str]]:
    """Redact every segment using deny-list matches on the continuous view.

    Returns redacted segments with unchanged timing and speaker labels plus
    the redaction counters in the shared anonymizer's categories. A match
    whose text lies entirely inside a removed region of an earlier match
    cannot occur because the shared locator yields non-overlapping matches.
    """
    pieces = segment_texts(segments)
    view, offsets = continuous_offsets(pieces)
    located: list[DenyMatch] = locator.locate(view)  # type: ignore[attr-defined]
    categories = Counter(match.category for match in located)
    redacted = project_matches(pieces, offsets, located)
    redacted_segments = [
        Segment(
            start_ms=segment.start_ms,
            end_ms=segment.end_ms,
            text=text,
            speaker_id=segment.speaker_id,
        )
        for segment, text in zip(segments, redacted, strict=True)
    ]
    return redacted_segments, categories
