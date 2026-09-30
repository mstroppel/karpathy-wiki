# Audio ingest

Issue #15: selected WebDAV audio recordings become locally transcribed,
optionally speaker-diarized Markdown sources. Transcription runs on the
local speech worker (faster-whisper with CTranslate2); no audio or
transcript is ever sent to a cloud API. The existing wiki ingest flow
imports the published sources; transcription alone never writes wiki pages.

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
| `AUDIO_SYNC_INTERVAL` | `1h` | Daemon cycle interval (transcription is expensive) |
| `AUDIO_LANGUAGE` | auto-detect | ISO code passed to the speech worker |
| `AUDIO_DIARIZE` | `0` | Request anonymous speaker-turn diarization |
| `AUDIO_MAX_BYTES` | `536870912` | Reject larger recordings content-free |
| `AUDIO_MAX_DURATION_SECONDS` | `14400` | Reject longer recordings content-free |
| `SPEECH_MODEL` | `small` | faster-whisper model preset (see below) |
| `SPEECH_DEVICE` | `cpu` | `cpu` or `cuda` |
| `SPEECH_COMPUTE_TYPE` | `int8` | CTranslate2 compute type; use `int8_float16` on CUDA |
| `HUGGINGFACE_TOKEN` | – | Only needed for diarization model downloads |

Model presets: start with `small` (multilingual, `int8` on CPU,
`int8_float16` on a small CUDA GPU). Larger models and Whisper V3 Turbo
quality presets should be benchmarked on the actual host before adoption
(the concept doc records the host-trial caveat). Model files are downloaded
once into the persistent model cache (`DATA_ROOT/models/audio`); their
licenses require acceptance for diarization models.

Speaker diarization with pyannote.audio identifies speaker *turns*, not
people; ambiguous or overlapping speech is marked as unknown rather than
inventing a speaker, and clusters are never mapped to identities. It
additionally requires a pinned pyannote installation on the speech worker
image and an HF token accepted for the model license; until that host trial
happens, diarization-enabled requests fail content-free.

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
logs. Raw audio and unredacted transcripts stay there. Deleting
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
never WebDAV credentials, redactions, or the wiki. It polls
`speech/requests`, transcribes, and writes structured results into
`speech/cache`; providers replay identical work from that cache, keyed by
audio SHA-256 plus the full processing key (a changed worker image
supersedes older results automatically).

Before processing requests, the worker announces its identity in
`speech/worker-identity.json`. The identity covers the backend, model,
device, compute type, limits, installed runtime, speech code, and a build-time
image stamp. Providers accept only matching cache entries and include this
identity in durable publication jobs, so configuration changes reprocess
unchanged recordings too. A worker change during publication aborts that
generation for retry. Start the worker before running a one-shot provider.

CI and machines without a GPU run the fake backend by setting
`SPEECH_BACKEND=fake` for the speech service; it produces deterministic
segments without codecs. The CPU image carries faster-whisper pinned by
hash (`ingest/speech/requirements.txt`); a CUDA override with an NVIDIA GPU
reservation is an opt-in host configuration, not shipped by default.
