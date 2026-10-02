# Planned work

Only remaining work is listed here; scope and acceptance criteria live in the
linked issues. Deliver small, independently deployable PRs.

## Current priorities

1. [#137](https://github.com/mstroppel/karpathy-wiki/issues/137): OpenChamber as the main chat interface — release image and default service implemented; complete browser/model acceptance (see [chat deployment](docs/chat.md))
2. [#134](https://github.com/mstroppel/karpathy-wiki/issues/134): Optional Mistral hosted speech-to-text backend
3. [#129](https://github.com/mstroppel/karpathy-wiki/issues/129): Add code-review agent skill
4. [#26](https://github.com/mstroppel/karpathy-wiki/issues/26): Expand integration coverage and harden daemon failure handling

The user's Mistral STT trial produced substantially better transcription than
local models. Prioritize the opt-in hosted backend over further local speech
features; raw audio leaves the host before transcript redaction. CUDA and local
diarization are not prerequisites.

## Further user-facing extensions

- [#18](https://github.com/mstroppel/karpathy-wiki/issues/18): Multi Language Support

## Deferred until a concrete need arises

- [#113](https://github.com/mstroppel/karpathy-wiki/issues/113): Optional: isolate and serialize wiki publication when needed
- [#135](https://github.com/mstroppel/karpathy-wiki/issues/135): Audio Improvements — revisit normalization only if the selected STT backend has a demonstrated quality gap

## After the first stable 1.0 release

- [#130](https://github.com/mstroppel/karpathy-wiki/issues/130): Optional CUDA deployment for local speech
- [#131](https://github.com/mstroppel/karpathy-wiki/issues/131): Deliver optional local speaker diarization
- [#133](https://github.com/mstroppel/karpathy-wiki/issues/133): Speaker Identification — discovery before implementation
- [#78](https://github.com/mstroppel/karpathy-wiki/issues/78): E-Mail Ingest — dedicated provider only if still needed; the current email workflow is covered via Paperless
- [#108](https://github.com/mstroppel/karpathy-wiki/issues/108): Isolate wiki-agent shell execution from OpenCode credentials
- [#46](https://github.com/mstroppel/karpathy-wiki/issues/46): Version and migrate generated wiki security policy

Before 1.0, reorganize data manually per release notes; no automatic migrations,
legacy aliases, or data-layout moves.
