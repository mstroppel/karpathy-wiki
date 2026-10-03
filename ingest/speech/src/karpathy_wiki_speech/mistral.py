"""Opt-in hosted transcription through the Mistral speech-to-text API.

This module is the only place that talks to a hosted transcription service.
The ``mistral`` speech backend uploads the private staged recording straight
to the transcription endpoint: no public recording URL, no original WebDAV
file name, and no provider metadata ever leave the worker. The unredacted
answer is stored in the private speech cache exactly like a local backend's
result, so the existing local redaction, validation, and publication pipeline
runs afterwards.

Two consequences are deliberate and documented in ``docs/audio.md``:

* raw audio reaches Mistral **before** any transcript redaction, and the
  service's own data-handling terms apply (see the Mistral pricing and
  privacy pages);
* the API forbids combining an explicit language with the timestamps the
  shared timed-segment contract requires, so an explicit ``language`` is
  rejected content-free instead of silently producing untimed text.

Failures are content-free: error messages carry the failure kind (and the
HTTP status) only, never audio, transcript text, response bodies, file
names, or the API key. The API key is a worker-only secret: it appears in
the request header and nowhere else.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from karpathy_wiki_speech.backends import BackendUnavailableError, describe_options
from karpathy_wiki_speech.types import (
    DecodedAudio,
    LimitExceededError,
    Segment,
    TranscriptionOptions,
    TranscriptionResult,
)

DEFAULT_BASE_URL = "https://api.mistral.ai/v1"
DEFAULT_MODEL = "voxtral-mini-latest"
TRANSCRIPTION_PATH = "/audio/transcriptions"
TIMESTAMP_GRANULARITY = "segment"
DIARIZATION_MODE = "service-speakers"

# Hosted-service input limits, as documented by Mistral for audio
# transcription (formats WAV/MP3/FLAC/OGG/WEBM, 60 minutes, 500 MB). They
# are enforced alongside the installation's own recording limits and can be
# lowered per host; raising them may simply move the rejection to the API.
SERVICE_MAX_BYTES = 500 * 1024 * 1024
SERVICE_MAX_DURATION_SECONDS = 60 * 60
SERVICE_ALLOWED_EXTENSIONS: tuple[str, ...] = (".flac", ".mp3", ".ogg", ".webm", ".wav")

DEFAULT_TIMEOUT_SECONDS = 600.0
DEFAULT_MAX_ATTEMPTS = 4
DEFAULT_BACKOFF_SECONDS = 2.0
MAX_BACKOFF_SECONDS = 60.0

CONTENT_TYPES: dict[str, str] = {
    ".flac": "audio/flac",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".wav": "audio/wav",
    ".webm": "audio/webm",
}

# ``(status, lowercase response headers, body)``; raises ``TimeoutError`` on
# a request timeout and ``OSError`` on any other transport failure.
Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, dict[str, str], bytes]]


class HostedError(RuntimeError):
    """A content-free failure of the hosted transcription service."""


class HostedAuthenticationError(HostedError):
    """The service rejected the worker's API key or account."""


class HostedRateLimitError(HostedError):
    """The service kept rate-limiting the worker within the retry budget."""


class HostedTimeoutError(HostedError):
    """The service did not answer within the configured request timeout."""


class HostedConnectionError(HostedError):
    """The service could not be reached from the worker."""


class HostedServiceError(HostedError):
    """The service failed the request for a reason the worker cannot fix."""


class HostedRequestError(HostedError):
    """The service rejected the request itself (bad input, too large, ...)."""


class HostedResponseError(ValueError):
    """The service answered, but not with a usable transcription."""


class HostedInputLimitError(LimitExceededError):
    """The recording violates a hosted-service input limit."""


class TimestampLanguageConflictError(ValueError):
    """Explicit language and timed segments cannot be combined on the service."""


class DiarizationUnavailableError(HostedError):
    """Speaker diarization was requested but the service returned no speakers."""


def api_key_from_env() -> str:
    """Read the worker-only hosted API secret from its file (or the environment).

    The key is never logged, never written to results or cache keys, and
    never enters worker identity; only the request carries it. Compose mounts
    the secret into the speech worker service alone.
    """
    direct = os.getenv("MISTRAL_API_KEY", "").strip()
    if direct:
        return direct
    path = os.getenv("MISTRAL_API_KEY_FILE", "").strip()
    if not path:
        raise ValueError("the hosted transcription API key is not configured")
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        raise ValueError("the hosted transcription API key file is not readable") from None
    if not value:
        raise ValueError("the hosted transcription API key file is empty")
    return value


def build_request_body(
    *,
    model: str,
    diarize: bool,
    filename: str,
    content_type: str,
    content: bytes,
    boundary: str,
) -> bytes:
    """Multipart body for the transcription endpoint.

    Only the requested model, the timestamp granularity, the diarization
    flag, and the audio bytes are sent. The upload name is generic
    (``audio.<ext>``), so no WebDAV file name or provider metadata is
    transmitted.
    """
    fields: list[tuple[str, str]] = [
        ("model", model),
        ("timestamp_granularities", TIMESTAMP_GRANULARITY),
    ]
    if diarize:
        fields.append(("diarize", "true"))
    parts: list[bytes] = [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'
        ).encode()
        for name, value in fields
    ]
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode()
        + content
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts)


def upload_filename(path: str) -> str:
    """Generic upload name derived from the extension only."""
    extension = Path(path).suffix.lower()
    return f"audio{extension}" if extension else "audio"


def content_type_of(path: str) -> str:
    return CONTENT_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def default_transport() -> Transport:
    """HTTP transport backed by the pinned ``httpx`` runtime dependency."""

    def post(
        url: str, headers: dict[str, str], body: bytes, timeout: float
    ) -> tuple[int, dict[str, str], bytes]:
        try:
            import httpx  # type: ignore[import-not-found]
        except ImportError as error:
            raise BackendUnavailableError(
                "httpx is not installed; install karpathy-wiki-speech[mistral]"
            ) from error
        try:
            response = httpx.post(url, content=body, headers=headers, timeout=timeout)
        except httpx.TimeoutException:
            # from None: transport errors can echo request details.
            raise TimeoutError("the hosted transcription request timed out") from None
        except httpx.HTTPError:
            raise OSError("the hosted transcription request failed") from None
        return (
            response.status_code,
            {key.lower(): value for key, value in response.headers.items()},
            response.content,
        )

    return post


def decode_response(raw: bytes) -> dict[str, Any]:
    """Parse one successful answer as a JSON object, or fail content-free."""
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise HostedResponseError("the hosted service returned an unreadable response") from None
    if not isinstance(payload, dict):
        raise HostedResponseError("the hosted service returned no JSON object")
    return payload


def _seconds(value: object, field: str) -> int:
    """Convert one API timestamp in seconds to whole milliseconds."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise HostedResponseError(f"the hosted service returned no {field} timestamp")
    if value < 0:
        raise HostedResponseError(f"the hosted service returned a negative {field} timestamp")
    return int(round(value * 1000))


def parse_transcription(
    payload: object,
    *,
    diarize: bool,
) -> tuple[list[Segment], str | None, str]:
    """Map one hosted answer onto the shared timed-segment contract.

    Returns ``(segments, language, model)``. Speaker labels are mapped to
    anonymous cluster ids in first-appearance order; the service's own labels
    are never stored. Diarization requests without any returned speaker label
    are rejected instead of publishing an unlabeled transcript as diarized.
    """
    if not isinstance(payload, dict):
        raise HostedResponseError("the hosted service returned no JSON object")
    model = _provenance(payload.get("model"), "model provenance")
    text = payload.get("text")
    if not isinstance(text, str):
        raise HostedResponseError("the hosted service returned no transcript text")
    language = payload.get("language")
    detected = None if language is None else _provenance(language, "language")
    raw_segments = payload.get("segments", [])
    if not isinstance(raw_segments, list):
        raise HostedResponseError("the hosted service returned no segment list")
    segments: list[Segment] = []
    labels: dict[str, int] = {}
    labeled = False
    for raw in raw_segments:
        if not isinstance(raw, dict):
            raise HostedResponseError("the hosted service returned a malformed segment")
        start_ms = _seconds(raw.get("start"), "start")
        end_ms = _seconds(raw.get("end"), "end")
        if end_ms < start_ms:
            raise HostedResponseError("the hosted service returned a reversed segment")
        segments.append(
            Segment(
                start_ms=start_ms,
                end_ms=end_ms,
                text=_segment_text(raw.get("text")),
                speaker_id=_speaker_id(raw.get("speaker_id"), diarize, labels),
            )
        )
        labeled = labeled or raw.get("speaker_id") is not None
    if text.strip() and not segments:
        raise HostedResponseError("the hosted service returned no timed segments")
    if diarize and segments and not labeled:
        raise DiarizationUnavailableError(
            "the hosted service returned no speaker labels for a diarization request"
        )
    return segments, detected, model


def _provenance(value: object, field: str) -> str:
    """One bounded, printable provenance string from the hosted answer."""
    if not isinstance(value, str) or not value.strip() or len(value) > 120:
        raise HostedResponseError(f"the hosted service returned no usable {field}")
    if any(ord(character) < 32 for character in value):
        raise HostedResponseError(f"the hosted service returned no usable {field}")
    return value.strip()


def _segment_text(value: object) -> str:
    if not isinstance(value, str):
        raise HostedResponseError("the hosted service returned a segment without text")
    return " ".join(value.split()).strip()


def _speaker_id(value: object, diarize: bool, labels: dict[str, int]) -> int | None:
    if not diarize:
        return None
    if value is None:
        # Diarization is on, this turn's speaker is unknown: never invent one.
        return -1
    if not isinstance(value, str):
        raise HostedResponseError("the hosted service returned an unusable speaker label")
    return labels.setdefault(value, len(labels))


class MistralClient:
    """One bounded-retry call to the hosted transcription endpoint.

    Only transient failures (unreachable service, timeouts, rate limits,
    server errors) are retried, with exponential backoff and a bounded
    budget: every retry can cost another paid transcription. Authentication
    failures, rejected requests, and malformed answers fail immediately and
    content-free.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_seconds: float = DEFAULT_BACKOFF_SECONDS,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not api_key:
            raise ValueError("the hosted transcription API key is missing")
        if not base_url.startswith("https://"):
            raise ValueError("the hosted transcription endpoint must use HTTPS")
        if timeout_seconds <= 0 or max_attempts <= 0 or backoff_seconds < 0:
            raise ValueError("the hosted transcription retry configuration is invalid")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._max_attempts = max_attempts
        self._backoff_seconds = backoff_seconds
        self._transport = transport or default_transport()
        self._sleep = sleep

    def transcribe(self, path: str, *, model: str, diarize: bool) -> dict[str, Any]:
        with open(path, "rb") as handle:
            content = handle.read()
        boundary = f"karpathywiki{os.urandom(16).hex()}"
        body = build_request_body(
            model=model,
            diarize=diarize,
            filename=upload_filename(path),
            content_type=content_type_of(path),
            content=content,
            boundary=boundary,
        )
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        }
        url = f"{self._base_url}{TRANSCRIPTION_PATH}"
        attempt = 0
        while True:
            attempt += 1
            last = attempt >= self._max_attempts
            try:
                status, response_headers, raw = self._transport(
                    url, headers, body, self._timeout_seconds
                )
            except TimeoutError:
                if last:
                    raise HostedTimeoutError("the hosted transcription request timed out") from None
                self._pause({}, attempt)
                continue
            except OSError:
                if last:
                    raise HostedConnectionError(
                        "the hosted transcription service is unreachable"
                    ) from None
                self._pause({}, attempt)
                continue
            if status == 200:
                return decode_response(raw)
            if status in (401, 403):
                raise HostedAuthenticationError(
                    "the hosted transcription service rejected the API key"
                ) from None
            if status in (408, 429) or status >= 500:
                if last:
                    if status == 429:
                        raise HostedRateLimitError(
                            "the hosted transcription service kept rate-limiting the worker"
                        ) from None
                    raise HostedServiceError(
                        f"the hosted transcription service failed (HTTP {status})"
                    ) from None
                self._pause(response_headers, attempt)
                continue
            raise HostedRequestError(
                f"the hosted transcription request was rejected (HTTP {status})"
            ) from None

    def _pause(self, headers: Mapping[str, str], attempt: int) -> None:
        """Exponential backoff, honoring a bounded ``Retry-After`` hint."""
        delay = self._backoff_seconds * (2 ** (attempt - 1))
        retry_after = headers.get("retry-after", "")
        try:
            delay = max(delay, float(retry_after))
        except ValueError:
            pass
        self._sleep(min(delay, MAX_BACKOFF_SECONDS))


class MistralBackend:
    """Hosted transcription backend (concept: replaceable speech backend).

    Diarization is delegated to the service and mapped to anonymous speaker
    turns; clusters are never mapped to identities. Every output-affecting
    knob (backend name, model, endpoint, timestamp granularity) is reported
    through :meth:`describe`, which the worker folds into its processing
    identity: switching between the local and the hosted backend, or between
    hosted models, reprocesses unchanged recordings and never mixes cache
    results. Valid completed results replay from that cache without another
    paid request.
    """

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        max_bytes: int = SERVICE_MAX_BYTES,
        max_duration_seconds: int = SERVICE_MAX_DURATION_SECONDS,
        allowed_extensions: tuple[str, ...] = SERVICE_ALLOWED_EXTENSIONS,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_bytes = max_bytes
        self._max_duration_seconds = max_duration_seconds
        self._allowed_extensions = allowed_extensions
        self._client = MistralClient(
            api_key=api_key,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            max_attempts=max_attempts,
            transport=transport,
            sleep=sleep,
        )

    @classmethod
    def from_env(cls) -> MistralBackend:
        return cls(
            api_key=api_key_from_env(),
            base_url=os.getenv("SPEECH_MISTRAL_BASE_URL", "").strip() or DEFAULT_BASE_URL,
            timeout_seconds=_float_env("SPEECH_MISTRAL_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
            max_attempts=_int_env("SPEECH_MISTRAL_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS),
            max_bytes=_int_env("SPEECH_MISTRAL_MAX_BYTES", SERVICE_MAX_BYTES),
            max_duration_seconds=_int_env(
                "SPEECH_MISTRAL_MAX_DURATION_SECONDS", SERVICE_MAX_DURATION_SECONDS
            ),
        )

    @staticmethod
    def _default_model() -> str:
        # Multilingual transcription model behind the audio/transcriptions
        # endpoint; pinned by name so a model swap is an explicit choice.
        return DEFAULT_MODEL

    def describe(self, options: TranscriptionOptions) -> dict[str, object]:
        return {
            "backend": "mistral",
            "model": options.model or self._default_model(),
            "api": {
                "base_url": self._base_url,
                "timestamp_granularity": TIMESTAMP_GRANULARITY,
                "diarization": DIARIZATION_MODE,
            },
            "options": describe_options(options),
        }

    def transcribe(
        self,
        path: str,
        decoded: DecodedAudio,
        options: TranscriptionOptions,
    ) -> TranscriptionResult:
        if options.language:
            # The API cannot combine ``language`` with ``timestamp_granularities``.
            # Timings are required by the shared contract, so an explicit
            # language is refused before anything is uploaded or billed.
            raise TimestampLanguageConflictError(
                "the hosted transcription service cannot combine an explicit language "
                "with the timestamps the timed-segment contract requires; "
                "leave AUDIO_LANGUAGE empty to use the service's language detection"
            )
        self._check_input(path, decoded)
        payload = self._client.transcribe(
            path, model=options.model or self._default_model(), diarize=options.diarize
        )
        segments, language, model = parse_transcription(payload, diarize=options.diarize)
        return TranscriptionResult(
            segments=segments,
            language=language,
            backend="mistral",
            model=model,
            options=describe_options(options),
        )

    def _check_input(self, path: str, decoded: DecodedAudio) -> None:
        """Enforce hosted-service input limits before any upload happens."""
        extension = Path(path).suffix.lower()
        if extension not in self._allowed_extensions:
            raise HostedInputLimitError("the hosted service does not accept this audio format")
        if decoded.input_bytes > self._max_bytes:
            raise HostedInputLimitError("the recording exceeds the hosted service size limit")
        if decoded.duration_seconds > self._max_duration_seconds:
            raise HostedInputLimitError("the recording exceeds the hosted service duration limit")


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name, "").strip()
    return int(value) if value else default


def _float_env(name: str, default: float) -> float:
    value = os.getenv(name, "").strip()
    return float(value) if value else default
