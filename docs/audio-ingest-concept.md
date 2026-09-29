# Audio ingest concept (#15)

## Goal and boundaries

Turn selected WebDAV audio recordings into locally transcribed, optionally
speaker-diarized Markdown sources. The existing wiki ingest/status flow then
imports those sources; transcription alone does not write wiki pages. Keep the
speech-processing interface reusable for a later email-attachment provider.
Start with one recording at a time and no external transcription service.

## Proposed flow

```text
selected WebDAV folder (audio extensions only)
  -> audio connector: rclone to private incoming/audio snapshots
  -> audio worker: decode, transcribe, optionally diarize, redact
  -> sources/audio: immutable sanitized generation + v1 manifest
  -> wiki_ingest_status / wiki-ingest -> wiki/sources/audio/...
```

1. Add an opt-in `audio` profile. Its WebDAV connector uses the existing
   WebDAV credentials but keeps its own private raw-audio directory; it does
   not change the Markdown-only WebDAV provider or publish raw files under
   `sources/`. Sync a complete inventory to a private snapshot before making
   it available to the worker. The worker reads immutable snapshots rather
   than files while rclone is updating them. Set format, size and duration
   limits and reject unsupported/corrupt files with content-free errors.
2. A separate local speech worker receives only an audio path and options,
   returning structured timed segments (`start_ms`, `end_ms`, `text`, optional
   `speaker_id`, language and backend/model version). Decode with ffmpeg;
   use faster-whisper/CTranslate2 for transcription and, when enabled,
   pyannote.audio for diarization. Align transcript segments with speaker
   turns by time overlap and mark ambiguous/overlapping speech as unknown
   rather than inventing a speaker. Do not infer personal identities from
   anonymous speaker clusters. Keep this interface independent of WebDAV so
   an email provider can submit attachments to the same worker later.
3. Redact the transcript before inserting timestamps or speaker labels. Build
   a continuous text view across segment boundaries with a mapping from text
   spans back to timed segments. Apply the configured deny-list to that view
   and project replacements onto the segments: a match spanning segments
   must remove all matched text, emitting its replacement once. Preserve
   timing information without duplicating the original matched words. Extend
   the shared anonymizer with span-aware output as needed so this uses the
   same matching rules as `TargetedAnonymizer`.
   Render Markdown with a sanitized recording title, language, sanitized
   relative WebDAV origin, source audio SHA-256, processing model/version
   and parameters, followed by
   `[00:01:23–00:01:29] Speaker 1: ...` entries. Make explicit that labels
   and transcript text are machine-generated and should be reviewed. Keep
   original audio, decoded audio and raw transcript out of published sources,
   model prompts, wiki assets and content-bearing logs. Apply
   `TargetedAnonymizer` locally to the entire rendered document as a final
   pass. Redact and validate all public metadata too, including provenance,
   titles, frontmatter, manifest fields and error reports; document-only
   validation is insufficient. Publish only sanitized output after residual
   deny-list validation succeeds.
   Redaction is a deny-list, not a guarantee of complete anonymization.
4. Publish under a distinct `sources/audio` provider (`wiki_root: audio`)
   using the existing v1 manifest and source-generation/state conventions.
   Assign a persistent opaque source ID to each WebDAV path and keep the
   original path-to-ID mapping in private provider storage, outside the
   content-free shared job store. Use that ID in public source keys, claims
   and output/wiki paths. Do not derive IDs from redacted paths: changing
   redaction rules must not change identity or merge distinct sources.
   Treat a renamed path as a new source and revoke the old one; identical
   audio at different paths still has distinct source identities.

   Separate two kinds of reuse. Cache structured speech results privately by
   audio SHA-256 plus backend/model versions and all speech-processing options.
   This cache contains unredacted text and must never be mounted into OpenCode
   or served as a source. For each source ID, separately render and sanitize
   those results using that source's title and origin. Key published artifacts
   by source ID, speech-result revision, provenance/rendering inputs, renderer
   version and redaction fingerprint. Changed redactions or provenance must
   regenerate publication even when speech results are reused. A manifest
   item points to sanitized Markdown and its SHA-256
   `source_revision`, with a stable `wiki_path` and claim. Record upstream
   removal as a revocation; do not silently delete previously ingested wiki
   knowledge. A failed transcription or redaction keeps the previous
   published generation and reports a content-free failure.

## Local GPU and speaker support

noScribe uses [faster-whisper and pyannote](https://github.com/kaixxx/noScribe)
for transcription and speaker diarization. Follow that combination through
their libraries, rather than embedding the noScribe GUI/CLI or copying its
GPL-licensed code. noScribe's own project documents local processing and
supports NVIDIA CUDA; diarization identifies **speaker turns**, not named
people. Its advanced options describe Whisper V3 Turbo models, which are a
candidate for a quality preset.

For the proposed 8 GB RTX 2060, start with a configurable small/medium
multilingual model using `int8_float16` (or `int8`) and batch size 1; benchmark
V3 Turbo as an optional quality preset on the actual card. Run transcription
and diarization sequentially, releasing GPU memory between stages (or run
diarization on CPU if GPU memory is insufficient). No guaranteed fit or speed
is assumed without a host trial, especially when other processes use VRAM.
Use an opt-in CUDA image/Compose override with an NVIDIA GPU reservation;
leave CPU mode usable for CI and machines without a GPU. Pin compatible
CUDA/CTranslate2/PyTorch/pyannote and model versions in the worker image, and
keep model files in a persistent private cache with documented download and
license/consent requirements. No audio or transcript is sent to a cloud API.
Keep WebDAV credentials in the connector; the GPU worker gets only its
private audio snapshot, model cache, sanitized output, state and redactions.

## Delivery and verification

1. Define the timed-segment/backend interface and build a bounded local
   transcription spike. Measure VRAM, processing time and diarization quality
   on the target host with short, consented one- and two-speaker recordings;
   confirm the CUDA runtime before choosing the default model preset.
2. Add the WebDAV audio connector and opt-in worker/profile with immutable
   input snapshots, idempotent processing, privacy checks, provider manifest,
   health reporting and docs for retention of raw input, private speech-result
   caches, source identity mappings and model caches.
3. Test identical and changed recordings, duplicate hashes, missing/deleted
   files, changing redactions/models, malformed/oversized audio, interrupted
   sync/worker/restart, diarization disabled or uncertain, and validation
   failures. Include sensitive filenames in all published-metadata checks,
   distinct paths that sanitize to the same title, identical audio at different
   paths, renamed recordings, and a configured multiword literal split across
   transcript segments. Verify separate provenance on cache hits, stable IDs
   after redaction changes, and removal of every part of a cross-segment match.
   CI can use a fake backend and tiny generated audio; run a separate
   opt-in host GPU smoke test for real models. Verify status transitions and
   import with the existing wiki flow.

External transcription, named speaker identification and a correction UI are
outside the initial scope. The speech backend remains replaceable if these
capabilities become needed.
