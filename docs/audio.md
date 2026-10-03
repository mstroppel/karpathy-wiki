# Audio ingest

Issue #15: selected WebDAV audio recordings become transcribed,
optionally speaker-diarized Markdown sources. Transcription runs on the
speech worker of this stack with one of two explicitly selected backends:
the default local backend (faster-whisper with CTranslate2) keeps every
recording on this host, while the opt-in hosted backend
([#134](https://github.com/mstroppel/karpathy-wiki/issues/134)) uploads the
raw recording to the Mistral speech-to-text service **before** redaction.
The existing wiki ingest flow imports the published sources; transcription
alone never writes wiki pages.

See `docs/audio-ingest-concept.md` for the full concept and boundaries.

## Enable the audio profile

1. Upload the consented recordings to a WebDAV folder (default
   `Recordings`; the audio connector uses the same WebDAV credentials as
   the Markdown provider but keeps its own private raw directory).
2. Configure WebDAV access and `REDACTIONS_FILE` as for the WebDAV source
   provider (see [configuration](configuration.md#secrets)).
3. Enable the profile:

   ```env
   COMPOSE_PROFILES=webdav,audio
   AUDIO_WEBDAV_PATH=Recordings
   ```

4. Start the stack and run one cycle manually first:

   ```bash
   ./karpathy-wiki.sh up -d
   docker compose --profile audio run --rm audio-ingest audio --once
   ```

Then ask OpenCode to ingest `/knowledge/sources/audio` like any other
source. Recordings that fail discovery, limits, transcription, or redaction
never publish; the last successful generation stays active and the failure
is reported content-free through the health record and retry backoff.

## Processing options

| Variable | Default | Purpose |
| --- | --- | --- |
| `AUDIO_WEBDAV_PATH` | `Recordings` | WebDAV folder scanned for audio files |
| `AUDIO_SYNC_INTERVAL` | `1h` | Pause after each completed daemon cycle (for example, `1m`) |
| `AUDIO_LANGUAGE` | auto-detect | ISO code passed to the speech worker (see below) |
| `AUDIO_DIARIZE` | `0` | Request anonymous speaker-turn diarization |
| `AUDIO_MAX_BYTES` | `536870912` | Reject larger recordings content-free |
| `AUDIO_MAX_DURATION_SECONDS` | `14400` | Reject longer recordings content-free |
| `SPEECH_BACKEND` | `faster-whisper` | Speech backend: `faster-whisper`, `mistral` (opt-in hosted), or `fake` |
| `SPEECH_MODEL` | `small` | faster-whisper model preset (see below) |
| `SPEECH_DEVICE` | `cpu` | Only `cpu` is currently supported by the shipped speech image |
| `SPEECH_COMPUTE_TYPE` | `int8` | CTranslate2 compute type for CPU transcription |
| `HUGGINGFACE_TOKEN` | – | Only needed for diarization model downloads |

Cycles run sequentially: the daemon synchronizes WebDAV, waits for each
recording's speech result (or failure/timeout), and finishes publication
before waiting `AUDIO_SYNC_INTERVAL`. Setting it to `1m` means the next
sync starts one minute after the previous cycle finishes, even when
transcription takes longer than a minute. Interval ticks do not start
overlapping cycles or accumulate scheduled jobs. Unchanged recordings
reuse cached results, and the speech worker processes queued requests one
at a time. A provider timeout does not cancel the worker's request; retries
reuse the queued request or its completed result.

Model presets: start with `small` (multilingual, `int8` on CPU).
Only CPU deployment is currently supported; CUDA deployment is tracked in
[#130](https://github.com/mstroppel/karpathy-wiki/issues/130).
Larger models and Whisper V3 Turbo quality presets should be benchmarked
on the actual host before adoption (the concept doc records the host-trial
caveat). Model files are downloaded
once into the persistent model cache (`DATA_ROOT/models/audio`); their
licenses require acceptance for diarization models.

Speaker diarization with pyannote.audio identifies speaker *turns*, not
people; ambiguous or overlapping speech is marked as unknown rather than
inventing a speaker, and clusters are never mapped to identities. It
additionally requires a pinned pyannote installation on the speech worker
image and an HF token accepted for the model license; until that host trial
happens, diarization-enabled requests fail content-free.

## Hosted transcription (opt-in, Mistral)

Issue [#134](https://github.com/mstroppel/karpathy-wiki/issues/134) adds a
second backend that transcribes through Mistral's hosted speech-to-text
service instead of a local model. It is off by default and only used when
explicitly selected:

```env
COMPOSE_PROFILES=webdav,audio
SPEECH_BACKEND=mistral
SPEECH_MISTRAL_MODEL=voxtral-mini-latest
MISTRAL_API_KEY_FILE=./secrets/mistral-api-key
```

Create the key file and keep it private (mode `0600`), like the Paperless
token:

```bash
printf '%s' 'YOUR-MISTRAL-API-KEY' > ./secrets/mistral-api-key
chmod 600 ./secrets/mistral-api-key
```

`MISTRAL_API_KEY_FILE` names the *host* file that Compose mounts into the
speech worker as `/run/secrets/mistral_api_key`. Without it, the secret falls
back to the tracked, intentionally empty placeholder
`secrets/mistral-api-key.example`, because Compose requires the secret file
to exist even in local-only mode: the audio profile starts without any
Mistral credential, and hosted mode then fails content-free (the key file is
empty) until a real key file is configured. Running the worker outside
Compose may instead set the `MISTRAL_API_KEY` environment variable; the
standard Compose service does not pass that variable through, so Compose
deployments configure the key through the file.

| Variable | Default | Purpose |
| --- | --- | --- |
| `SPEECH_MISTRAL_MODEL` | `voxtral-mini-latest` | Hosted model selection (transcription model id) |
| `MISTRAL_API_KEY_FILE` | `secrets/mistral-api-key.example` | Worker-only API secret file, mounted into the speech worker alone |
| `SPEECH_MISTRAL_BASE_URL` | `https://api.mistral.ai/v1` | Transcription endpoint |
| `SPEECH_MISTRAL_MAX_BYTES` | `524288000` | Hosted service input size limit (500 MB) |
| `SPEECH_MISTRAL_MAX_DURATION_SECONDS` | `3600` | Hosted service input duration limit (60 min) |
| `SPEECH_MISTRAL_TIMEOUT_SECONDS` | `600` | Per-request timeout |
| `SPEECH_MISTRAL_MAX_ATTEMPTS` | `4` | Bounded retry budget for transient failures |

### What leaves the host, and when

This mode is an external service, not local-only processing:

- the staged recording (raw audio) is uploaded over HTTPS to the Mistral
  transcription endpoint **before** any transcript redaction runs. Deny-list
  redaction protects published text, not the audio that was already sent;
- Mistral bills transcription per audio minute, and Mistral's terms of
  service and privacy policy apply to the uploaded recordings. Review both
  (and the [pricing](https://mistral.ai/pricing) page) before enabling this
  for consented recordings;
- what is never sent: WebDAV file names or folder names, provider metadata,
  the redaction configuration, wiki content, or the API key in anything but
  the request header. The upload is the audio bytes under the generic name
  `audio.<ext>`, referenced by content hash locally;
- the API key is a worker-only secret: the Compose file mounts it into the
  `audio-speech` service alone, and it never appears in logs, published
  metadata, cache keys, or worker identity;
- the unredacted answer is stored in the private speech cache exactly like a
  local result. The existing local redaction, validation, and publication
  pipeline then runs unchanged, so published sources stay sanitized and
  unredacted transcripts stay inside `DATA_ROOT/speech`.

Local mode (`SPEECH_BACKEND=faster-whisper`, the default) still sends
nothing off the host. Switching between the two modes is an explicit
configuration change plus a restart of the speech worker.

### Service input limits

The hosted service accepts WAV, MP3, FLAC, OGG, and WEBM, at most 500 MB
and 60 minutes per request (Mistral's documented limits). Those limits are
enforced alongside the installation's own recording limits
(`AUDIO_MAX_BYTES`, `AUDIO_MAX_DURATION_SECONDS`): the tighter bound wins,
and a recording that violates them is rejected content-free before anything
is uploaded or billed. The defaults can be lowered per host
(`SPEECH_MISTRAL_MAX_BYTES`, `SPEECH_MISTRAL_MAX_DURATION_SECONDS`);
raising them above the documented service limits only moves the rejection
to the API. Note that the general extension list also allows `.m4a`, `.mp4`,
and `.opus`, which this service does not document support for; recordings
in those formats fail content-free in this mode.

### Timestamps, language, and speakers

- Segment timestamps are always requested, because the shared timed-segment
  contract requires them. The API documents that timestamps cannot be
  combined with an explicit `language`, so an explicit `AUDIO_LANGUAGE` is
  rejected content-free (`TimestampLanguageConflictError`) before any upload
  instead of silently producing untimed text. Leave `AUDIO_LANGUAGE` empty:
  the service detects the language and the detected code is recorded in the
  result and the published provenance.
- `AUDIO_DIARIZE=1` is supported through the service's own diarization and
  is mapped to the same anonymous speaker-turn ids the local backend emits
  (`Sprecher N`, unknown turns stay unknown). Service speaker labels are
  never stored or published. If the service answers a diarization request
  without any speaker label, the request fails content-free
  (`DiarizationUnavailableError`) rather than publishing an unlabeled
  transcript as if it were diarized. As locally, labels identify speaker
  turns, not people.

### Processing identity and paid requests

Backend, model, endpoint, and the timestamp granularity are output-affecting
and therefore part of the worker's processing identity. Switching between
the local and the hosted backend, or between hosted models, reprocesses
unchanged recordings and never mixes cache results from different
configurations. Unchanged work replays from the private cache without
another paid request; delete `DATA_ROOT/speech/cache` only when you accept
re-transcription cost.

### Failures

Authentication failures and rejected requests are never retried: the API key
is wrong, the account is out of scope, or the request itself cannot succeed,
so a second attempt would only cost more. Rate limits, timeouts, an
unreachable service, and server errors are retried with bounded exponential
backoff (honoring `Retry-After`) within `SPEECH_MISTRAL_MAX_ATTEMPTS`; each
retry may be billed as a new request, so the budget stays small. Malformed or
untimed answers and exhausted retries fail content-free: the worker writes an
error *type* only (for example `speech:processing:HostedRateLimitError`) into
`speech/failures`, the source is quarantined, and the last successful
published generation stays active.

## What is published

Sanitized Markdown only, below `sources/audio` with `wiki_root: audio`:

- a redacted recording title (derived from the file name) and the sanitized
  relative WebDAV origin,
- the source audio SHA-256, language, backend/model versions and options,
- `[00:01:23-00:01:29] Speaker 1: ...` entries (rendered as `Sprecher N`),
  with machine-generated labels and text explicitly marked for review.

Source identity comes from a persistent opaque mapping of WebDAV paths
stored in `DATA_ROOT/state/audio-identity` (private provider storage,
outside the content-free shared job store). Consequences:

- changing redaction rules never changes identity or merges sources;
- a renamed recording revokes the old source and publishes a new one;
- identical audio at two paths keeps two distinct sources;
- removing a recording upstream records a manifest revocation instead of
  silently dropping the previously ingested knowledge.

## Data layout and retention

```text
${DATA_ROOT}/
├── incoming/audio/             # private rclone snapshot (raw audio)
├── speech/
│   ├── recordings/             # immutable per-hash copies for the worker
│   ├── requests/               # content-free queue entries
│   ├── failures/               # content-free worker failure reports
│   ├── cache/                  # structured results (unredacted!)
│   ├── worker-identity.json    # active processing configuration fingerprint
│   └── worker-health.json      # worker heartbeat
├── models/audio/               # persistent model cache
├── sources/audio/              # published sanitized generations
├── quarantine/audio/           # content-free error reports
└── state/audio-identity/       # private path → source ID mapping
```

`incoming/audio`, `speech/`, and `models/audio` are private: they are
never mounted into OpenCode, never served as sources, and never appear in
logs. Raw audio and unredacted transcripts stay there — with the opt-in
hosted backend, staged recordings in `speech/recordings` are additionally
uploaded to the configured transcription service before redaction (see
above). Deleting
`speech/cache` only costs re-transcription; deleting
`state/audio-identity` would re-identify every recording (do not).

To recover manually, delete every entry of `sources/audio/generations`
except the directory `current` resolves to, then restart the service. The
names `manifest.json`, `current`, `generations`, and `.generation.json`
are reserved inside `sources/audio`.

Generation metadata stores only an inventory fingerprint, never the original
recording paths. Removal revocations remain in subsequent manifests until the
same source becomes live again, so delayed wiki ingestion still sees removals.

## Speech worker

The `audio-speech` service (`karpathy-wiki-ingest-speech` image) owns the
models; it sees only the private speech directory and its model cache —
never WebDAV credentials, redactions, or the wiki. In hosted mode it is also
the only container that sees the Mistral API secret. It polls
`speech/requests`, transcribes, and writes structured results into
`speech/cache`; providers replay identical work from that cache, keyed by
audio SHA-256 plus the full processing key (a changed worker image
supersedes older results automatically).

Before processing requests, the worker announces its identity in
`speech/worker-identity.json`. The identity covers the backend, model,
device, compute type, endpoint, limits, installed runtime, speech code, and
a build-time image stamp — never the API key. Providers accept only matching
cache entries and include this identity in durable publication jobs, so
configuration changes reprocess unchanged recordings too. A worker change
during publication aborts that generation for retry. Start the worker before
running a one-shot provider.

CI runs the fake backend by setting
`SPEECH_BACKEND=fake` for the speech service; it produces deterministic
segments without codecs. The CPU image carries faster-whisper and the pinned
hosted-API client (`httpx`) in `ingest/speech/requirements.txt`, hash-pinned;
no new runtime dependency is needed for the hosted backend beyond that
pinned HTTP client. CUDA runtime and Compose GPU
reservation support are tracked in
[#130](https://github.com/mstroppel/karpathy-wiki/issues/130).
