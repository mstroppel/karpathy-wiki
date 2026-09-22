"""Provider manifest: the end-to-end contract between ingest and the wiki.

Every ingest cycle writes a versioned ``manifest.json`` into its sanitized
source root (see ``contracts/provider-manifest/`` in the repository). The
generic JavaScript status scanner consumes only this manifest and the wiki
pages; it contains no provider-specific code.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .contract import REVISION_RE
from .shared import atomic_write

MANIFEST_CONTRACT = "karpathy-wiki-provider-manifest"
MANIFEST_VERSION = 1
MANIFEST_FILENAME = "manifest.json"

_SOURCE_RE = re.compile(r"[a-z0-9_-]+")
_FORBIDDEN_SEGMENTS = {"", ".", ".."}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class ManifestError(ValueError):
    """The manifest does not conform to the provider manifest contract."""


@dataclass(frozen=True)
class ManifestItem:
    source_key: str
    source_path: str
    wiki_path: str
    source_revision: str
    frontmatter: dict[str, Any]
    claim: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def check_relative_path(value: Any, label: str = "path") -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.startswith("/")
        or "\\" in value
        or _CONTROL_RE.search(value)
        or any(segment in _FORBIDDEN_SEGMENTS for segment in value.split("/"))
    ):
        raise ManifestError(f"{label} must be a normalized relative POSIX path")
    return value


def check_claim(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not value:
        raise ManifestError(f"{label} must be a non-empty object")
    for name, field_value in value.items():
        if not isinstance(name, str) or not name or not isinstance(field_value, str):
            raise ManifestError(f"{label} must map non-empty names to strings")
    return value


def _claim_key(claim: dict[str, str]) -> str:
    return json.dumps(sorted(claim.items()), ensure_ascii=False)


def validate_manifest(value: Any, expected_source: str | None = None) -> dict[str, Any]:
    """Validate a parsed manifest against the contract, or raise ManifestError."""
    if not isinstance(value, dict):
        raise ManifestError("manifest must be a JSON object")
    if value.get("contract") != MANIFEST_CONTRACT:
        raise ManifestError("manifest does not follow the provider manifest contract")
    version = value.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ManifestError("manifest version is invalid")
    if version != MANIFEST_VERSION:
        raise ManifestError(f"unsupported manifest version: {version}")
    source = value.get("source")
    if not isinstance(source, str) or not _SOURCE_RE.fullmatch(source):
        raise ManifestError("manifest source is invalid")
    if expected_source is not None and source != expected_source:
        raise ManifestError(f"manifest source {source!r} does not match {expected_source!r}")
    generated_at = value.get("generated_at")
    if not isinstance(generated_at, int) or isinstance(generated_at, bool) or generated_at <= 0:
        raise ManifestError("manifest generated_at is invalid")
    wiki_root = value.get("wiki_root", ".")
    if wiki_root != ".":
        check_relative_path(wiki_root, "manifest wiki_root")

    items = value.get("items")
    if not isinstance(items, list):
        raise ManifestError("manifest items must be an array")
    seen_keys: set[str] = set()
    seen_claims: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            raise ManifestError("manifest item must be an object")
        source_key = item.get("source_key")
        if not isinstance(source_key, str) or not source_key:
            raise ManifestError("manifest item source_key must be a non-empty string")
        if source_key in seen_keys:
            raise ManifestError(f"source_key is duplicated: {source_key}")
        seen_keys.add(source_key)
        check_relative_path(item.get("source_path"), f"{source_key}: source_path")
        check_relative_path(item.get("wiki_path"), f"{source_key}: wiki_path")
        revision = item.get("source_revision")
        if not isinstance(revision, str) or not REVISION_RE.fullmatch(revision):
            raise ManifestError(f"{source_key}: source_revision is invalid")
        frontmatter = item.get("frontmatter")
        if not isinstance(frontmatter, dict):
            raise ManifestError(f"{source_key}: frontmatter must be an object")
        if frontmatter.get("source_revision") != revision:
            raise ManifestError(f"{source_key}: frontmatter revision differs")
        claim = check_claim(item.get("claim"), f"{source_key}: claim")
        claim_identifier = _claim_key(claim)
        if claim_identifier in seen_claims:
            raise ManifestError(f"{source_key}: claim is already used by another item")
        seen_claims.add(claim_identifier)

    revoked = value.get("revoked", [])
    if not isinstance(revoked, list):
        raise ManifestError("manifest revoked must be an array")
    for entry in revoked:
        if not isinstance(entry, dict):
            raise ManifestError("revoked entry must be an object")
        source_key = entry.get("source_key")
        if not isinstance(source_key, str) or not source_key:
            raise ManifestError("revoked entry source_key must be a non-empty string")
        if source_key in seen_keys:
            raise ManifestError(f"revoked source_key is also listed as an item: {source_key}")
        check_claim(entry.get("claim"), f"{source_key}: claim")
        claim_identifier = _claim_key(entry["claim"])
        if claim_identifier in seen_claims:
            raise ManifestError(f"{source_key}: claim is already used by another item")
        seen_claims.add(claim_identifier)

    errors = value.get("errors", [])
    if not isinstance(errors, list):
        raise ManifestError("manifest errors must be an array")
    for error in errors:
        if not isinstance(error, dict):
            raise ManifestError("error entry must be an object")
        if not isinstance(error.get("error"), str) or not error["error"]:
            raise ManifestError("error entry must carry an error string")
        if "path" in error:
            check_relative_path(error["path"], "error path")

    return value


def build_manifest(
    source: str,
    items: list[ManifestItem],
    revoked: list[dict[str, Any]] | None = None,
    errors: list[dict[str, Any]] | None = None,
    generated_at: int | None = None,
    wiki_root: str = ".",
) -> dict[str, Any]:
    manifest = {
        "contract": MANIFEST_CONTRACT,
        "version": MANIFEST_VERSION,
        "source": source,
        "generated_at": int(time.time() if generated_at is None else generated_at),
        "wiki_root": wiki_root,
        "items": [item.to_dict() for item in items],
        "revoked": list(revoked or []),
        "errors": list(errors or []),
    }
    validate_manifest(manifest, expected_source=source)
    return manifest


def write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    """Persist the manifest atomically; the scanner tolerates concurrent reads."""
    atomic_write(
        path,
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
    )
