# Ingest modules

Ingest is plugin-based and split into installable Python distributions. The
core repository ships the shared safety core plus two built-in plugins:

```text
ingest/
  core/       karpathy-wiki-ingest            shared core: anonymizer, atomic
              (karpathy_wiki_ingest)          writes, validation, health, plugin
                                              dispatch; ships no plugins
  webdav/     karpathy-wiki-ingest-webdav     rclone sync + sanitizer loop
              (karpathy_wiki_ingest_webdav)
  paperless/  karpathy-wiki-ingest-paperless  Paperless API + sanitizer loop
              (karpathy_wiki_ingest_paperless)
```

Each distribution has its own `pyproject.toml` and is built and installed
independently; the plugin images install the core wheel and exactly one plugin
wheel, so no image ships another plugin's code. The unqualified
`karpathy-wiki-ingest` image is the plugin-free core and serves as the base
image for third-party plugins.

`python -m karpathy_wiki_ingest <plugin>` dispatches to a plugin's `main()`.
Without a plugin name it runs the sole installed plugin (which is what the
per-plugin images rely on); with zero or multiple plugins installed, a name is
required.

## Adding a module without touching this repository

A module is any Python package that exposes a callable `main()`. It can live
in a separate repo and depends only on the shared core library:

```python
# karpathy_wiki_ingest_mastodon/main.py
from karpathy_wiki_ingest.shared import (
    PrivacyValidationError,
    TargetedAnonymizer,
    atomic_write,
    required_env,
)


def main() -> None: ...
```

Dispatch accepts either of the following; both are discovered automatically,
so a sole installed plugin runs even when it is not named explicitly:

1. **Package convention** - a package named `karpathy_wiki_ingest_<plugin>`
   with a `main()`. This is the recommended path; it needs no extra metadata.
2. **Entry points** - register an entry point named after the plugin in the
   group `karpathy_wiki_ingest.plugins` that points at your `main()`. This is
   what the built-in plugins do.

Build your image on top of the shared core image (it already contains Python,
rclone, and redaction tooling):

```dockerfile
FROM ghcr.io/mstroppel/karpathy-wiki-ingest:${KARPATHY_WIKI_VERSION:-latest}
COPY karpathy_wiki_ingest_mastodon /app/karpathy_wiki_ingest_mastodon
```

Then reference it in `compose.yaml` under an optional profile, mirroring the
built-in services:

```yaml
  mastodon-ingest:
    image: yourrepo/karpathy-wiki-mastodon-ingest:latest
    profiles: ["mastodon"]
```

## Runtime contract

A module must guarantee the same properties the built-ins provide:

- Read the shared `REDACTIONS_FILE` secret (JSON) and build a
  `TargetedAnonymizer` from it; never write content that has not passed
  `anonymize()` without raising `PrivacyValidationError` on residual matches.
- Write sanitized Markdown only to `SANITIZED_ROOT`, non-destructively via
  `atomic_write()`; files that fail text decoding or privacy validation go to
  `QUARANTINE_ROOT` as content-free error reports and are removed from the
  sanitized tree.
- Operate under `PUID:PGID`, honor a `--once` flag for one-shot runs, and
  tolerate concurrent wiki reads during writes.
- Keep the source layout and front matter format versioned; OpenCode and the
  ingest skill read this contract, not the plugin code. The versioned status
  contract with shared conformance fixtures lives in
  [`contracts/ingest-status/`](../contracts/ingest-status/v1/contract.json);
  both the Python packages and the JavaScript status scanner are tested
  against the same fixtures.

The core image provides the environment; a module must not require extra
services reachable from the wiki network. Attach any needed network
explicitly in Compose.
