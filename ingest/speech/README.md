# karpathy-wiki-speech

Local speech processing for Karpathy Wiki ingest: timed transcription with
faster-whisper and optional speaker-diarization with pyannote.audio. Receives
only an audio path and processing options, returns structured timed segments
(`start_ms`, `end_ms`, text, optional `speaker_id`, language, and backend
metadata). Decoding uses the FFmpeg libraries bundled with PyAV. No audio or
transcript ever leaves the host.

The interface is provider-independent: the WebDAV audio ingest plugin uses it
today, and a later email-attachment provider can submit attachments to the
same worker. CI and machines without a GPU run the fake backend; real
transcription runs in the opt-in CUDA image with pinned model versions.
