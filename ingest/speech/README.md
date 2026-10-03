# karpathy-wiki-speech

Speech processing for Karpathy Wiki ingest: timed transcription with
faster-whisper and optional speaker-diarization with pyannote.audio, or the
opt-in hosted Mistral backend. Receives only an audio path and processing
options, returns structured timed segments (`start_ms`, `end_ms`, text,
optional `speaker_id`, language, and backend metadata). Decoding uses the
FFmpeg libraries bundled with PyAV.

The backend is explicitly selected (`SPEECH_BACKEND`):

- `faster-whisper` (default) and `fake` keep every recording and transcript
  on the host;
- `mistral` uploads the private staged recording to the Mistral
  speech-to-text API before any redaction and needs a worker-only API
  secret. Unredacted results, cache replay, content-free failures, and the
  shared timed-segment contract are identical in all modes; see
  `docs/audio.md` for what leaves the host, service limits, costs, and
  data-handling terms.

The interface is provider-independent: the WebDAV audio ingest plugin uses it
today, and a later email-attachment provider can submit attachments to the
same worker. CI and machines without a GPU run the fake backend; real
transcription runs in the opt-in CUDA image with pinned model versions.
