"""Speech backend implementations for the timed-segment interface.

Local backends live here; the opt-in hosted Mistral backend (which uploads
the staged recording to a cloud API) lives in
:mod:`karpathy_wiki_speech.mistral` next to its request/answer handling.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from karpathy_wiki_speech.types import (
    DecodedAudio,
    Segment,
    TranscriptionOptions,
    TranscriptionResult,
    result_identity,  # noqa: F401 - re-exported for backend implementers
)


class BackendUnavailableError(RuntimeError):
    """A real backend's optional dependency is not installed."""


class SpeechBackend(Protocol):
    """Replaceable speech-processing interface (concept step 2).

    Implementations receive only the decoded audio facts, the file path, and
    options; they return timed segments. Anything provider-specific
    (identity, titles, paths, redactions) stays outside this interface.
    """

    def describe(self, options: TranscriptionOptions) -> dict[str, object]: ...

    def transcribe(
        self,
        path: str,
        decoded: DecodedAudio,
        options: TranscriptionOptions,
    ) -> TranscriptionResult: ...


class FakeBackend:
    """Deterministic multi-speaker backend for tests and CI.

    It segments ``decoded.input_bytes`` into fixed slices and annotates them
    with alternating speaker ids when diarization is requested. The text is
    derived only from the filename stem, which tests control; real audio
    content is never required.
    """

    def describe(self, options: TranscriptionOptions) -> dict[str, object]:
        return {
            "backend": "fake",
            "model": "fake-1",
            "options": describe_options(options),
        }

    def transcribe(
        self,
        path: str,
        decoded: DecodedAudio,
        options: TranscriptionOptions,
    ) -> TranscriptionResult:
        duration_ms = int(decoded.duration_seconds * 1000)
        step = 1000
        blocks = max(duration_ms // step, 1)
        name = Path(path).stem
        segments: list[Segment] = []
        for index in range(blocks):
            start = index * step
            end = duration_ms if index == blocks - 1 else (index + 1) * step
            speaker = index % 2 if options.diarize else None
            segments.append(
                Segment(
                    start_ms=start,
                    end_ms=end,
                    text=f"{name} segment {index + 1}",
                    speaker_id=speaker,
                )
            )
        return TranscriptionResult(
            segments=segments,
            language=options.language,
            backend="fake",
            model="fake-1",
            options=describe_options(options),
        )


def describe_options(options: TranscriptionOptions) -> dict[str, object]:
    return {
        "language": options.language,
        "model": options.model,
        "compute_type": options.compute_type,
        "device": options.device,
        "diarize": options.diarize,
    }


def import_faster_whisper() -> Any:
    try:
        import faster_whisper  # type: ignore[import-not-found]
    except ImportError as error:  # pragma: no cover - exercised via extras
        raise BackendUnavailableError(
            "faster-whisper is not installed; install karpathy-wiki-speech[whisper]"
        ) from error
    return faster_whisper


class FasterWhisperBackend:
    """Local transcription with faster-whisper; diarization via pyannote.

    Construction fails with :class:`BackendUnavailableError` when the
    ``whisper`` extra is missing, so providers can fall back without leaking
    any content. Transcription and diarization run sequentially; a model
    instance is kept for reuse across recordings (the worker releases GPU
    memory between stages through CTranslate2 itself). Diarization labels
    anonymous speaker turns only; clusters are never mapped to identities.
    """

    def __init__(self) -> None:
        self._model: Any = None
        self._model_key: str | None = None
        self._diarization_pipeline: Any = None

    def describe(self, options: TranscriptionOptions) -> dict[str, object]:
        faster_whisper = import_faster_whisper()
        return {
            "backend": "faster-whisper",
            "model": options.model or self._default_model(),
            "faster_whisper_version": faster_whisper.__version__,
            "options": describe_options(options),
        }

    @staticmethod
    def _default_model() -> str:
        # Small multilingual model with int8: usable on CPU and small GPUs.
        return "small"

    def _model_for(self, options: TranscriptionOptions) -> Any:
        faster_whisper = import_faster_whisper()
        model_name = options.model or self._default_model()
        compute_type = options.compute_type or (
            "int8_float16" if options.device != "cpu" else "int8"
        )
        device = options.device or "auto"
        key = f"{model_name}:{compute_type}:{device}"
        if self._model is None or self._model_key != key:
            self._model = faster_whisper.WhisperModel(
                model_name, device=device, compute_type=compute_type
            )
            self._model_key = key
        return self._model

    def _diarize(self, path: str, options: TranscriptionOptions) -> list[tuple[float, float, int]]:
        """Return anonymous speaker turns as ``(start, end, speaker)`` triples.

        pyannote.audio identifies speaker *turns*, not people. The pipeline
        is instantiated once per process and never sees anything but the
        audio file. Requires a pinned pyannote.audio installation plus an HF
        token accepted for the diarization model license (see the audio
        ingest docs); this extension point fails content-free otherwise.
        """
        if self._diarization_pipeline is None:
            try:
                from pyannote.audio import Pipeline  # type: ignore[import-not-found]
            except ImportError as error:
                raise BackendUnavailableError(
                    "speaker diarization requires a pinned pyannote.audio install;"
                    " see the audio ingest documentation"
                ) from error
            import os

            token = os.getenv("HUGGINGFACE_TOKEN", "").strip()
            self._diarization_pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1", use_auth_token=token or None
            )
        assert self._diarization_pipeline is not None
        annotation = self._diarization_pipeline(path)
        turns: list[tuple[float, float, int]] = []
        speakers: dict[str, int] = {}

        def cluster_id(label: Any) -> int:
            name = str(label)
            if name not in speakers:
                speakers[name] = len(speakers)
            return speakers[name]

        for turn, _, label in annotation.itertracks(yield_label=True):
            turns.append((turn.start, turn.end, cluster_id(label)))
        turns.sort()
        return turns

    def transcribe(
        self,
        path: str,
        decoded: DecodedAudio,
        options: TranscriptionOptions,
    ) -> TranscriptionResult:
        model = self._model_for(options)
        segments: list[Segment] = []
        language: str | None = options.language
        raw_kwargs: dict[str, Any] = {
            "beam_size": 1,
            "vad_filter": True,
        }
        if options.language:
            raw_kwargs["language"] = options.language
        raw_segments, info = model.transcribe(path, **raw_kwargs)
        if not options.language:
            language = getattr(info, "language", None)
        turns = self._diarize(path, options) if options.diarize else []
        turns_by_index = 0

        for raw in raw_segments:
            text = " ".join(str(raw.text).split()).strip()
            if not text:
                continue
            start_ms = int(raw.start * 1000)
            end_ms = int(raw.end * 1000)
            speaker_id = None
            if turns:
                overlapping = [
                    (start, end, speaker)
                    for start, end, speaker in turns
                    if start < raw.end and end > raw.start
                ]
                if len(overlapping) == 1:
                    speaker_id = overlapping[0][2]
                elif len(overlapping) > 1:
                    # Ambiguous or overlapping speech: never invent a speaker.
                    speaker_id = -1
                turns_by_index += 1
            segments.append(
                Segment(
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=text,
                    speaker_id=speaker_id,
                )
            )
        return TranscriptionResult(
            segments=segments,
            language=language,
            backend="faster-whisper",
            model=options.model or self._default_model(),
            options=describe_options(options),
        )
