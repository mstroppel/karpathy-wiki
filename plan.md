# Planned work

Only remaining work is listed here; scope and acceptance criteria live in the
linked issues. Deliver small, independently deployable PRs.

## Current priorities

1. [#129](https://github.com/mstroppel/karpathy-wiki/issues/129): Add code-review agent skill
2. [#26](https://github.com/mstroppel/karpathy-wiki/issues/26): Expand integration coverage and harden daemon failure handling
3. [#135](https://github.com/mstroppel/karpathy-wiki/issues/135): Audio Improvements

## Near-term speech extensions

- [#130](https://github.com/mstroppel/karpathy-wiki/issues/130): Optional CUDA deployment for local speech
- [#131](https://github.com/mstroppel/karpathy-wiki/issues/131): Deliver optional local speaker diarization
- [#134](https://github.com/mstroppel/karpathy-wiki/issues/134): Optional Mistral hosted speech-to-text backend

These features are independent; CUDA is not a prerequisite for diarization or
hosted STT.

## Further user-facing extensions

- [#18](https://github.com/mstroppel/karpathy-wiki/issues/18): Multi Language Support
- [#78](https://github.com/mstroppel/karpathy-wiki/issues/78): E-Mail Ingest
- [#137](https://github.com/mstroppel/karpathy-wiki/issues/137): Use OpenChamber

## Discovery

- [#133](https://github.com/mstroppel/karpathy-wiki/issues/133): Speaker Identification

## Deferred until a concrete need arises

- [#113](https://github.com/mstroppel/karpathy-wiki/issues/113): Optional: isolate and serialize wiki publication when needed

## After the first stable 1.0 release

- [#108](https://github.com/mstroppel/karpathy-wiki/issues/108): Isolate wiki-agent shell execution from OpenCode credentials
- [#46](https://github.com/mstroppel/karpathy-wiki/issues/46): Version and migrate generated wiki security policy

Before 1.0, reorganize data manually per release notes; no automatic migrations,
legacy aliases, or data-layout moves.
