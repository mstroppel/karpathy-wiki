"""Paperless ingest plugin for Karpathy Wiki.

The plugin is split along responsibility boundaries:

- ``config``: environment settings and secret handling
- ``client``: the Paperless-ngx HTTP API client
- ``documents``: document modelling, hashing, and source rendering
- ``storage``: sanitized-file layout and revocation lists
- ``ingestor``: the synchronization service
- ``cli``: argument parsing and daemon lifecycle

Breaking change (pre-1.0, no backwards compatibility): the former
single-module package exported ``MANIFEST_FILENAME``, ``ManifestItem``,
``build_manifest``, ``write_manifest``, and ``required_env`` from here.
Those symbols were never plugin state and are no longer re-exported;
import ``required_env`` from ``karpathy_wiki_ingest.shared`` and the
manifest names from ``karpathy_wiki_ingest.manifest`` instead.

The package entry point stays ``karpathy_wiki_ingest_paperless:main``.
"""

from __future__ import annotations

from karpathy_wiki_ingest.shared import (
    PrivacyValidationError,
    TargetedAnonymizer,
    atomic_write,
    canonical_phone,
    write_health,
)

from .cli import main
from .client import PaperlessClient, PaperlessSource, resource_id
from .config import Settings, read_secret
from .documents import SOURCE_FORMAT_VERSION, normalize_issued_date, render_document, source_hash
from .ingestor import Anonymizer, Ingestor, run_continuously
from .storage import (
    DOCUMENTS_PER_DIRECTORY,
    REVOKED_TITLE,
    document_directory,
    parse_revoked_ids,
    read_revoked_ids,
    source_document_ids,
    source_document_path,
    source_revision,
    write_revoked_ids,
)

__all__ = [
    "DOCUMENTS_PER_DIRECTORY",
    "REVOKED_TITLE",
    "Anonymizer",
    "Ingestor",
    "PaperlessClient",
    "PaperlessSource",
    "PrivacyValidationError",
    "Settings",
    "SOURCE_FORMAT_VERSION",
    "TargetedAnonymizer",
    "atomic_write",
    "canonical_phone",
    "document_directory",
    "main",
    "normalize_issued_date",
    "parse_revoked_ids",
    "read_revoked_ids",
    "read_secret",
    "render_document",
    "resource_id",
    "run_continuously",
    "source_document_ids",
    "source_document_path",
    "source_hash",
    "source_revision",
    "write_health",
    "write_revoked_ids",
]
