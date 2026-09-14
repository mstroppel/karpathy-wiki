from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import logging
import os
import re
import signal
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

LOG = logging.getLogger("karpathy-wiki-ingest")
PHONE_CANDIDATE_RE = re.compile(
    r"(?<!\w)(?:\+|00)?\d(?:[\s()./-]*\d){5,}(?!\w)"
)
PLACEHOLDER_RE = re.compile(r"^\[[A-Z][A-Z0-9_]*\]$")
SOURCE_FORMAT_VERSION = 2
SOURCE_FILE_RE = re.compile(r"^document-(\d+)\.md$")
DOCUMENTS_PER_DIRECTORY = 1000
SOURCE_REVISION_RE = re.compile(
    r'(?m)^source_revision:\s*["\']?([0-9a-f]{64})["\']?\s*$'
)
REVOKED_ID_RE = re.compile(r"(?m)^- (\d+)$")
REVOKED_TITLE = "# Widerrufene Paperless-Dokumente"


class Anonymizer(Protocol):
    fingerprint: str

    def anonymize(self, text: str) -> tuple[str, Counter[str]]: ...

    def contains_person_name(self, text: str) -> bool: ...


class PaperlessSource(Protocol):
    def selected_document_ids(self) -> list[int]: ...

    def document(self, document_id: int) -> dict[str, Any]: ...

    def document_type_name(self, value: Any) -> str | None: ...

    def tag_names(self, values: Any) -> list[str]: ...


class PrivacyValidationError(ValueError):
    """The candidate output is not safe to publish."""


@dataclass(frozen=True)
class Settings:
    api_url: str
    public_url: str
    source_tag_id: int
    token: str
    redactions_path: Path
    interval_seconds: int
    sanitized_root: Path
    quarantine_root: Path
    health_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        token = read_secret("PAPERLESS_TOKEN", "PAPERLESS_TOKEN_FILE")
        source_tag_id = int(required_env("PAPERLESS_SOURCE_TAG_ID"))
        if source_tag_id <= 0:
            raise ValueError("PAPERLESS_SOURCE_TAG_ID must be greater than zero")
        interval_seconds = int(os.getenv("SYNC_INTERVAL_SECONDS", "900"))
        if interval_seconds <= 0:
            raise ValueError("SYNC_INTERVAL_SECONDS must be greater than zero")
        public_url = os.getenv(
            "PAPERLESS_PUBLIC_URL", "https://paperless.rafatz.de"
        ).rstrip("/")
        if urllib.parse.urlsplit(public_url).scheme != "https":
            raise ValueError("PAPERLESS_PUBLIC_URL must use HTTPS")
        return cls(
            api_url=os.getenv("PAPERLESS_API_URL", "http://paperless:8000").rstrip("/"),
            public_url=public_url,
            source_tag_id=source_tag_id,
            token=token,
            redactions_path=Path(required_env("REDACTIONS_FILE")),
            interval_seconds=interval_seconds,
            sanitized_root=Path(
                os.getenv("SANITIZED_ROOT", "/data/sanitized/paperless")
            ),
            quarantine_root=Path(os.getenv("QUARANTINE_ROOT", "/data/quarantine")),
            health_path=Path(os.getenv("HEALTH_PATH", "/tmp/health.json")),
        )


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


def read_secret(value_name: str, file_name: str) -> str:
    direct = os.getenv(value_name, "").strip()
    if direct:
        return direct
    path = required_env(file_name)
    value = Path(path).read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"secret file for {value_name} is empty")
    return value


class PaperlessClient:
    def __init__(self, api_url: str, token: str, source_tag_id: int) -> None:
        self.api_url = api_url
        self.source_tag_id = source_tag_id
        self.headers = {
            "Accept": "application/json; version=10",
            "Authorization": f"Token {token}",
            "User-Agent": "karpathy-wiki-ingest/1",
        }
        self.resource_names: dict[tuple[str, int], str] = {}

    def selected_document_ids(self) -> list[int]:
        self.resource_names.clear()
        document_ids: set[int] = set()
        page = 1
        while True:
            query = urllib.parse.urlencode(
                {
                    "tags__id__all": self.source_tag_id,
                    "ordering": "id",
                    "page": page,
                    "page_size": 100,
                }
            )
            payload = self._get_json(f"{self.api_url}/api/documents/?{query}")
            results = payload.get("results")
            if not isinstance(results, list):
                raise ValueError("Paperless document list has no results array")
            document_ids.update(int(item["id"]) for item in results)
            if not payload.get("next"):
                break
            page += 1
        return sorted(document_ids)

    def document(self, document_id: int) -> dict[str, Any]:
        payload = self._get_json(f"{self.api_url}/api/documents/{document_id}/")
        if int(payload.get("id", -1)) != document_id:
            raise ValueError(f"Paperless returned the wrong document for {document_id}")
        return payload

    def document_type_name(self, value: Any) -> str | None:
        if value is None:
            return None
        return self._resource_name("document_types", value)

    def tag_names(self, values: Any) -> list[str]:
        if not isinstance(values, list):
            raise ValueError("Paperless document tags are not an array")
        return [self._resource_name("tags", value) for value in values]

    def _resource_name(self, resource: str, value: Any) -> str:
        if isinstance(value, dict):
            name = value.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Paperless {resource} entry has no name")
            return " ".join(name.split())
        try:
            resource_id = int(value)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Paperless {resource} entry has no numeric ID") from error
        key = (resource, resource_id)
        if key not in self.resource_names:
            payload = self._get_json(f"{self.api_url}/api/{resource}/{resource_id}/")
            name = payload.get("name")
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"Paperless {resource} entry has no name")
            self.resource_names[key] = " ".join(name.split())
        return self.resource_names[key]

    def _get_json(self, url: str) -> dict[str, Any]:
        return self._request_json(url)

    def _request_json(self, url: str) -> dict[str, Any]:
        request = urllib.request.Request(url, headers=self.headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
        if not isinstance(payload, dict):
            raise ValueError("Paperless returned a non-object JSON response")
        return payload


def resource_id(value: Any, resource: str) -> int:
    if isinstance(value, dict):
        value = value.get("id")
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"Paperless {resource} entry has no numeric ID") from error


@dataclass(frozen=True)
class LiteralRule:
    category: str
    replacement: str
    pattern: re.Pattern[str]


class TargetedAnonymizer:
    CATEGORIES = {
        "people": "PERSON",
        "addresses": "ADDRESS",
        "phones": "PHONE",
        "emails": "EMAIL",
        "birth_dates": "BIRTH_DATE",
    }

    def __init__(
        self,
        literal_rules: list[LiteralRule],
        phone_replacements: dict[str, str],
        person_name_patterns: list[re.Pattern[str]],
        fingerprint: str,
    ) -> None:
        self.literal_rules = literal_rules
        self.phone_replacements = phone_replacements
        self.person_name_patterns = person_name_patterns
        self.fingerprint = fingerprint

    @classmethod
    def from_file(cls, path: Path) -> "TargetedAnonymizer":
        try:
            configuration = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError("REDACTIONS_FILE is not readable JSON") from error
        return cls.from_config(configuration)

    @classmethod
    def from_config(cls, configuration: Any) -> "TargetedAnonymizer":
        if not isinstance(configuration, dict):
            raise ValueError("redaction configuration must be a JSON object")
        unknown = set(configuration) - set(cls.CATEGORIES)
        if unknown:
            raise ValueError(f"unknown redaction categories: {', '.join(sorted(unknown))}")

        literals: list[tuple[str, str, str]] = []
        phone_replacements: dict[str, str] = {}
        seen_literals: dict[str, str] = {}
        person_names: set[str] = set()

        def add_literal(category: str, replacement: str, value: str) -> None:
            normalized = " ".join(value.lower().split())
            previous = seen_literals.get(normalized)
            if previous and previous != replacement:
                raise ValueError("one literal value has multiple replacements")
            if previous is None:
                seen_literals[normalized] = replacement
                literals.append((category, replacement, value.strip()))

        for section, category in cls.CATEGORIES.items():
            entries = configuration.get(section, [])
            if not isinstance(entries, list):
                raise ValueError(f"redaction category {section} must be an array")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise ValueError(f"entries in {section} must be JSON objects")
                replacement = entry.get("replacement")
                if not isinstance(replacement, str) or not PLACEHOLDER_RE.fullmatch(
                    replacement
                ):
                    raise ValueError(
                        f"replacement in {section} must look like [PERSON_1]"
                    )
                values = entry.get("values", [])
                aliases = entry.get("aliases", [])
                if not isinstance(values, list) or not isinstance(aliases, list):
                    raise ValueError(f"values and aliases in {section} must be arrays")
                if section == "phones" and (aliases or not values):
                    raise ValueError("phones require a non-empty values array")
                for value in values:
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError(f"values in {section} must be non-empty strings")
                    if section == "phones":
                        canonical = canonical_phone(value)
                        if len(canonical) < 7:
                            raise ValueError("configured phone numbers are too short")
                        previous = phone_replacements.get(canonical)
                        if previous and previous != replacement:
                            raise ValueError("one phone number has multiple replacements")
                        phone_replacements[canonical] = replacement
                    else:
                        add_literal(category, replacement, value)
                        if section == "people":
                            person_names.add(value.strip())
                for alias in aliases:
                    if not isinstance(alias, str) or not alias.strip():
                        raise ValueError(f"aliases in {section} must be non-empty strings")
                    add_literal(category, replacement, alias)
                    if section == "people":
                        person_names.add(alias.strip())

                has_structured_fields = False
                if section == "people" and any(
                    key in entry
                    for key in (
                        "first_name",
                        "middle_names",
                        "last_name",
                        "previous_last_name",
                    )
                ):
                    first_name = required_entry_text(entry, "first_name", section)
                    middle_names = optional_entry_texts(entry, "middle_names", section)
                    last_name = required_entry_text(entry, "last_name", section)
                    last_names = [last_name]
                    if "previous_last_name" in entry:
                        last_names.append(
                            required_entry_text(entry, "previous_last_name", section)
                        )
                    person_names.update((first_name, *last_names, *middle_names))
                    for configured_last_name in last_names:
                        for variant in name_variants(
                            first_name, middle_names, configured_last_name
                        ):
                            add_literal(category, replacement, variant)
                    has_structured_fields = True
                elif section == "addresses" and any(
                    key in entry
                    for key in ("street", "house_number", "postal_code", "city")
                ):
                    street = required_entry_text(entry, "street", section)
                    house_number = required_entry_text(entry, "house_number", section)
                    postal_code = required_entry_text(entry, "postal_code", section)
                    cities = required_entry_texts(entry, "city", section)
                    if not re.fullmatch(r"\d{5}", postal_code):
                        raise ValueError("postal_code in addresses must contain five digits")
                    for variant in address_variants(
                        street, house_number, postal_code, cities
                    ):
                        add_literal(category, replacement, variant)
                    has_structured_fields = True
                elif section == "birth_dates" and "date" in entry:
                    configured_date = required_entry_text(entry, "date", section)
                    for variant in birth_date_variants(configured_date):
                        add_literal(category, replacement, variant)
                    has_structured_fields = True
                if section != "phones" and not values and not has_structured_fields:
                    raise ValueError(
                        f"entries in {section} require structured fields or values"
                    )

        if not literals and not phone_replacements:
            raise ValueError("at least one redaction value must be configured")
        literal_rules = [
            LiteralRule(category, replacement, literal_pattern(value))
            for category, replacement, value in sorted(
                literals, key=lambda item: len(item[2]), reverse=True
            )
        ]
        encoded = json.dumps(
            configuration, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        person_name_patterns = [
            literal_pattern(value)
            for value in sorted(person_names, key=len, reverse=True)
        ]
        return cls(
            literal_rules,
            phone_replacements,
            person_name_patterns,
            hashlib.sha256(encoded).hexdigest(),
        )

    def anonymize(self, text: str) -> tuple[str, Counter[str]]:
        counts: Counter[str] = Counter()
        output = text
        for rule in self.literal_rules:
            output, replacements = rule.pattern.subn(rule.replacement, output)
            if replacements:
                counts[rule.category] += replacements

        def replace_phone(match: re.Match[str]) -> str:
            replacement = self.phone_replacements.get(canonical_phone(match.group()))
            if replacement is None:
                return match.group()
            counts["PHONE"] += 1
            return replacement

        output = PHONE_CANDIDATE_RE.sub(replace_phone, output)
        if any(rule.pattern.search(output) for rule in self.literal_rules):
            raise PrivacyValidationError("configured literal remains after redaction")
        if any(
            canonical_phone(match.group()) in self.phone_replacements
            for match in PHONE_CANDIDATE_RE.finditer(output)
        ):
            raise PrivacyValidationError("configured phone number remains after redaction")
        return output, counts

    def contains_person_name(self, text: str) -> bool:
        return any(pattern.search(text) for pattern in self.person_name_patterns)


def literal_pattern(value: str) -> re.Pattern[str]:
    parts = value.split()
    pattern = re.escape(parts[0])
    for previous, part in zip(parts, parts[1:]):
        separator = r"\s*" if previous.endswith(",") else r"\s+"
        pattern += separator + re.escape(part)
    return re.compile(r"(?<!\w)" + pattern + r"(?!\w)", re.IGNORECASE)


def required_entry_text(entry: dict[str, Any], key: str, section: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} in {section} must be a non-empty string")
    return " ".join(value.split())


def optional_entry_texts(
    entry: dict[str, Any], key: str, section: str
) -> list[str]:
    values = entry.get(key, [])
    if not isinstance(values, list) or any(
        not isinstance(value, str) or not value.strip() for value in values
    ):
        raise ValueError(f"{key} in {section} must be an array of non-empty strings")
    return [" ".join(value.split()) for value in values]


def required_entry_texts(entry: dict[str, Any], key: str, section: str) -> list[str]:
    values = optional_entry_texts(entry, key, section)
    if not values:
        raise ValueError(f"{key} in {section} must be a non-empty array")
    return values


def name_variants(
    first_name: str, middle_names: list[str], last_name: str
) -> set[str]:
    given_name_variants = {first_name, *middle_names}
    if middle_names:
        given_name_variants.add(" ".join((first_name, *middle_names)))
    return {
        variant
        for given_names in given_name_variants
        for variant in (
            f"{given_names} {last_name}",
            f"{last_name} {given_names}",
            f"{last_name}, {given_names}",
        )
    }


def birth_date_variants(value: str) -> set[str]:
    try:
        parsed = datetime.date.fromisoformat(value)
    except ValueError as error:
        raise ValueError("date in birth_dates must use YYYY-MM-DD") from error
    day = str(parsed.day)
    month = str(parsed.month)
    day_padded = f"{parsed.day:02d}"
    month_padded = f"{parsed.month:02d}"
    year = str(parsed.year)
    short_year = f"{parsed.year % 100:02d}"
    variants = {value}
    for separator in (".", "/", "-"):
        variants.update(
            {
                separator.join((day, month, year)),
                separator.join((day_padded, month_padded, year)),
                separator.join((day, month, short_year)),
                separator.join((day_padded, month_padded, short_year)),
            }
        )
    return variants


def street_variants(street: str) -> set[str]:
    suffixes = ("straße", "strasse", "str.", "str")
    lowered = street.lower()
    for suffix in suffixes:
        if lowered.endswith(suffix):
            base = street[: -len(suffix)].rstrip()
            if base:
                separator = " " if street[: -len(suffix)].endswith(" ") else ""
                return {
                    f"{base}{separator}{variant}"
                    for variant in ("Straße", "Strasse", "Str.", "Str")
                }
    return {street}


def address_variants(
    street: str, house_number: str, postal_code: str, cities: list[str]
) -> set[str]:
    locations = {
        location
        for city in cities
        for location in (f"{postal_code} {city}", f"{city} {postal_code}")
    }
    variants: set[str] = set()
    for street_name in street_variants(street):
        street_address = f"{street_name} {house_number}"
        for location in locations:
            variants.update(
                {
                    f"{street_address}, {location}",
                    f"{street_address} {location}",
                    f"{location}, {street_address}",
                    f"{location} {street_address}",
                }
            )
    return variants


def canonical_phone(value: str) -> str:
    digits = "".join(character for character in value if character.isdigit())
    if digits.startswith("0049"):
        digits = "49" + digits[4:]
    if digits.startswith("490"):
        return "49" + digits[3:]
    if digits.startswith("0"):
        return "49" + digits[1:]
    return digits


def normalize_issued_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Paperless document creation date is not text")
    try:
        return datetime.date.fromisoformat(value).isoformat()
    except ValueError as error:
        raise ValueError("Paperless document creation date is not YYYY-MM-DD") from error


def source_hash(
    document: dict[str, Any],
    redaction_fingerprint: str,
    document_type: str | None,
    tags: list[str],
) -> str:
    selected = {
        "format_version": SOURCE_FORMAT_VERSION,
        "id": document.get("id"),
        "title": document.get("title") or "",
        "content": document.get("content") or "",
        "issued_date": document.get("created") or "",
        "document_type": document_type,
        "tags": tags,
        "redactions": redaction_fingerprint,
    }
    encoded = json.dumps(selected, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def render_document(
    document_id: int,
    title: str,
    content: str,
    public_url: str,
    counts: Counter[str],
    issued_date: str | None,
    document_type: str | None,
    tags: list[str],
    removed_person_tags: int,
    source_revision: str,
) -> str:
    source_url = f"{public_url}/documents/{document_id}"
    safe_title = " ".join(title.split())[:300] or f"Paperless-Dokument {document_id}"
    count_text = ", ".join(f"{kind}: {counts[kind]}" for kind in sorted(counts)) or "keine"
    return (
        "---\n"
        f"title: {json.dumps(safe_title, ensure_ascii=False)}\n"
        f"paperless_id: {document_id}\n"
        f"paperless_url: {json.dumps(source_url)}\n"
        f"source_revision: {json.dumps(source_revision)}\n"
        f"issued_date: {json.dumps(issued_date) if issued_date else 'null'}\n"
        f"document_type: {json.dumps(document_type, ensure_ascii=False) if document_type else 'null'}\n"
        f"tags: {json.dumps(tags, ensure_ascii=False)}\n"
        "anonymized: true\n"
        "---\n\n"
        f"# {safe_title}\n\n"
        f"[Dokument in Paperless öffnen]({source_url})\n\n"
        "## Gezielte Anonymisierung\n\n"
        f"Ersetzte konfigurierte Werte: {count_text}.\n\n"
        f"Entfernte Tags mit konfigurierten Personennamen: {removed_person_tags}.\n\n"
        "## Bereinigter OCR-Text\n\n"
        f"{content.strip()}\n"
    )


class Ingestor:
    def __init__(self, settings: Settings, anonymizer: Anonymizer) -> None:
        self.settings = settings
        self.anonymizer = anonymizer
        self.client: PaperlessSource = PaperlessClient(
            settings.api_url, settings.token, settings.source_tag_id
        )
        settings.sanitized_root.mkdir(parents=True, exist_ok=True)
        settings.quarantine_root.mkdir(parents=True, exist_ok=True)

    def run_once(self) -> tuple[int, int]:
        changed = 0
        failed = 0
        selected_ids = self.client.selected_document_ids()
        known_ids = source_document_ids(self.settings.sanitized_root)
        for document_id in selected_ids:
            try:
                if self.process(document_id):
                    changed += 1
            except PrivacyValidationError as error:
                failed += 1
                self.quarantine(document_id, error)
                LOG.error("Document %s failed privacy validation", document_id)
            except Exception as error:
                failed += 1
                self.record_error(document_id, error)
                LOG.error("Document %s failed with %s", document_id, type(error).__name__)
        self.reconcile(set(selected_ids), known_ids)
        write_health(self.settings.health_path, failed)
        LOG.info("Sync complete: %s changed, %s failed", changed, failed)
        return changed, failed

    def process(self, document_id: int) -> bool:
        document = self.client.document(document_id)
        issued_date = normalize_issued_date(document.get("created"))
        document_type = self.client.document_type_name(document.get("document_type"))
        content_tag_values = [
            value
            for value in document.get("tags", [])
            if resource_id(value, "tag") != self.settings.source_tag_id
        ]
        tag_names = sorted(self.client.tag_names(content_tag_values))
        digest = source_hash(
            document, self.anonymizer.fingerprint, document_type, tag_names
        )
        target = source_document_path(self.settings.sanitized_root, document_id)
        if source_revision(target) == digest:
            self.set_revoked(document_id, False)
            return False
        content = document.get("content") or ""
        if not isinstance(content, str) or not content.strip():
            raise PrivacyValidationError("Paperless document has no OCR text")
        title = document.get("title") or ""
        if not isinstance(title, str):
            raise ValueError("Paperless document title is not text")
        safe_title, title_counts = self.anonymizer.anonymize(title)
        safe_content, content_counts = self.anonymizer.anonymize(content)
        safe_document_type = None
        metadata_counts: Counter[str] = Counter()
        if document_type:
            safe_document_type, metadata_counts = self.anonymizer.anonymize(
                document_type
            )
        safe_tags: list[str] = []
        removed_person_tags = 0
        for tag_name in tag_names:
            if self.anonymizer.contains_person_name(tag_name):
                removed_person_tags += 1
                continue
            safe_tag, tag_counts = self.anonymizer.anonymize(tag_name)
            safe_tags.append(safe_tag)
            metadata_counts.update(tag_counts)
        output = render_document(
            document_id,
            safe_title,
            safe_content,
            self.settings.public_url,
            title_counts + content_counts + metadata_counts,
            issued_date,
            safe_document_type,
            safe_tags,
            removed_person_tags,
            digest,
        )
        atomic_write(target, output)
        self.set_revoked(document_id, False)
        (self.settings.quarantine_root / f"document-{document_id}.txt").unlink(
            missing_ok=True
        )
        LOG.info("Document %s was anonymized", document_id)
        return True

    def quarantine(self, document_id: int, error: Exception) -> None:
        target = source_document_path(self.settings.sanitized_root, document_id)
        if target.is_file():
            self.set_revoked(document_id, True)
        target.unlink(missing_ok=True)
        message = f"document_id={document_id}\ncategory=privacy-validation\nerror_type={type(error).__name__}\n"
        atomic_write(self.settings.quarantine_root / f"document-{document_id}.txt", message)

    def record_error(self, document_id: int, error: Exception) -> None:
        message = f"document_id={document_id}\ncategory=operational\nerror_type={type(error).__name__}\n"
        atomic_write(self.settings.quarantine_root / f"document-{document_id}.txt", message)

    def reconcile(self, selected_ids: set[int], known_ids: set[int]) -> None:
        revoked_path = self.settings.sanitized_root / "revoked.md"
        revoked_ids = read_revoked_ids(revoked_path)
        removed_ids = known_ids - selected_ids
        revoked_ids.update(removed_ids)
        if removed_ids or not revoked_path.exists():
            write_revoked_ids(revoked_path, revoked_ids)
        for document_id in removed_ids:
            source_document_path(self.settings.sanitized_root, document_id).unlink(
                missing_ok=True
            )
            LOG.warning("Document %s was revoked because its tag was removed", document_id)

    def set_revoked(self, document_id: int, revoked: bool) -> None:
        path = self.settings.sanitized_root / "revoked.md"
        ids = read_revoked_ids(path)
        if (document_id in ids) == revoked:
            return
        if revoked:
            ids.add(document_id)
        else:
            ids.discard(document_id)
        write_revoked_ids(path, ids)


def document_directory(document_id: int) -> str:
    start = document_id // DOCUMENTS_PER_DIRECTORY * DOCUMENTS_PER_DIRECTORY
    end = start + DOCUMENTS_PER_DIRECTORY - 1
    return f"{start:04d}-{end:04d}"


def source_document_path(root: Path, document_id: int) -> Path:
    return root / document_directory(document_id) / f"document-{document_id}.md"


def source_document_ids(root: Path) -> set[int]:
    ids = set()
    for path in root.glob("*/document-*.md"):
        match = SOURCE_FILE_RE.fullmatch(path.name)
        if match:
            ids.add(int(match.group(1)))
    return ids


def source_revision(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, UnicodeError):
        return None
    match = SOURCE_REVISION_RE.search(text)
    return match.group(1) if match else None


def read_revoked_ids(path: Path) -> set[int]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    lines = text.splitlines()
    if not lines or lines[0] != REVOKED_TITLE:
        raise ValueError(f"Invalid revoked document list: {path}")
    ids = set()
    for line in lines[1:]:
        if not line:
            continue
        match = REVOKED_ID_RE.fullmatch(line)
        if not match:
            raise ValueError(f"Invalid revoked document list: {path}")
        ids.add(int(match.group(1)))
    return ids


def write_revoked_ids(path: Path, ids: set[int]) -> None:
    lines = [REVOKED_TITLE, ""]
    lines.extend(f"- {document_id}" for document_id in sorted(ids))
    lines.append("")
    atomic_write(path, "\n".join(lines))


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def write_health(path: Path, failed: int) -> None:
    atomic_write(
        path, json.dumps({"checked_at": int(time.time()), "failed": failed}) + "\n"
    )


def run_continuously(
    ingestor: Ingestor, settings: Settings, stop_event: threading.Event
) -> None:
    while not stop_event.is_set():
        try:
            ingestor.anonymizer = TargetedAnonymizer.from_file(
                settings.redactions_path
            )
            ingestor.run_once()
        except (urllib.error.URLError, OSError, ValueError):
            LOG.exception("Paperless synchronization failed")
            write_health(settings.health_path, 1)
        if stop_event.wait(settings.interval_seconds):
            return


def main() -> None:
    parser = argparse.ArgumentParser(description="Redact tagged Paperless documents")
    parser.add_argument("--once", action="store_true", help="run one synchronization")
    arguments = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = Settings.from_env()
    anonymizer = TargetedAnonymizer.from_file(settings.redactions_path)
    ingestor = Ingestor(settings, anonymizer)
    if arguments.once:
        _, failed = ingestor.run_once()
        if failed:
            raise SystemExit(1)
        return
    stop_event = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stop_event.set())
    run_continuously(ingestor, settings, stop_event)
