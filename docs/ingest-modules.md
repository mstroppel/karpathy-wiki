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

## The provider manifest

Every ingest cycle writes a versioned `manifest.json` into its sanitized
source root. It is the end-to-end contract between ingest and the wiki: the
generic `wiki_ingest_status` tool consumes only this manifest and the wiki
pages, and contains no provider-specific code. A manifest lists each
published source with its stable `source_key`, the sanitized `source_path`,
the destination `wiki_path`, the `source_revision`, the exact `frontmatter`
the wiki page must carry, and the `claim` that identifies the source on its
page. Revocations and content-free error reports are part of the manifest.

The name `manifest.json` is reserved at the root of every source directory;
source files with that name are quarantined and never published.

The contract lives in [`contracts/provider-manifest/`](../contracts/provider-manifest/v1/contract.json)
together with shared conformance fixtures that both the Python packages and
the JavaScript scanner tests execute. Sources without a readable, valid
manifest of a supported version are reported as `invalid`; unknown versions
are never silently ignored.

A manifest declares its `wiki_root`: the wiki subtree the provider owns
relative to the wiki source root (`.` for providers that own the whole tree).
Pages are attributed to the manifest with the longest matching `wiki_root`,
so every provider declares its own subtree and `wiki_root` values must be
unique across sources; colliding manifests are reported as `invalid`.

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

Because the status scanner reads only the provider manifest, a third-party
module never requires rebuilding the core OpenCode image. A minimal provider
writes sanitized Markdown and one manifest per cycle:

```python
import hashlib
from pathlib import Path

from karpathy_wiki_ingest.manifest import ManifestItem, build_manifest, write_manifest
from karpathy_wiki_ingest.shared import TargetedAnonymizer, atomic_write


def main() -> None:
    incoming = Path("/data/incoming/notes")
    sanitized = Path("/data/sanitized/notes")
    anonymizer = TargetedAnonymizer.from_file(Path("/run/secrets/redactions"))
    items = []
    for source in sorted(incoming.rglob("*.txt")):
        output, _ = anonymizer.anonymize(source.read_text(encoding="utf-8"))
        revision = hashlib.sha256(output.encode("utf-8")).hexdigest()
        relative = source.relative_to(incoming).as_posix()
        atomic_write(sanitized / relative, output)
        items.append(
            ManifestItem(
                source_key=relative,
                source_path=relative,
                wiki_path=f"notes/{relative}/index.md",
                source_revision=revision,
                frontmatter={
                    "source_adapter": "notes",
                    "source_path": relative,
                    "source_revision": revision,
                },
                claim={"source_path": relative},
            )
        )
    write_manifest(sanitized / "manifest.json", build_manifest("notes", items, wiki_root="notes"))
```

## Contract versions and deprecation

Both contracts (`karpathy-wiki-ingest-status`, `karpathy-wiki-provider-manifest`)
are versioned documents in `contracts/`. Breaking changes bump the version. A
new manifest version is only introduced together with scanner support, and the
previous version stays readable for one release cycle before removal. Sources
that present an unknown contract or version are reported as `invalid` instead
of being guessed at. Between 1.0.0 releases no compatibility shims are
provided: installations upgrade by re-running ingest and re-importing pages.

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
  ingest skill read the contracts, not the plugin code. The versioned status
  contract with shared conformance fixtures lives in
  [`contracts/ingest-status/`](../contracts/ingest-status/v1/contract.json),
  and the provider manifest contract in
  [`contracts/provider-manifest/`](../contracts/provider-manifest/v1/contract.json);
  both the Python packages and the JavaScript status scanner are tested
  against the same fixtures. Every cycle must end by writing a valid
  `manifest.json` into the sanitized source root.

The core image provides the environment; a module must not require extra
services reachable from the wiki network. Attach any needed network
explicitly in Compose.
