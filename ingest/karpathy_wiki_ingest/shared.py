from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import tempfile
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PHONE_CANDIDATE_RE = re.compile(
    r"(?<!\w)(?:\+|00)?\d(?:[\s()./-]*\d){5,}(?!\w)"
)
PLACEHOLDER_RE = re.compile(r"^\[[A-Z][A-Z0-9_]*\]$")


class PrivacyValidationError(ValueError):
    """The candidate output is not safe to publish."""


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"{name} is required")
    return value


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
