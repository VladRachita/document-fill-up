"""Unlabelled address detection with regular expressions (street + locality lines)."""

from __future__ import annotations

import re

from docfill.extraction.text_utils import clean_value, is_address_continuation, parse_address
from docfill.models import ExtractedField

STREET_CONFIDENCE = 0.55
LOCALITY_CONFIDENCE = 0.5

_EN_SUFFIX = (
    r"(?i:street|st|avenue|ave|road|rd|boulevard|blvd|lane|ln|drive|dr|court|ct|place|pl|"
    r"square|sq|terrace|way|close|crescent|parkway|pkwy|highway|hwy)"
)
_STREET_PATTERNS = (
    # 12 Baker Street / 742 Evergreen Terrace, Apt 3
    re.compile(
        rf"\b\d{{1,5}}[A-Za-z]?\s+(?:[A-Z][\w'’.-]*\s+){{0,4}}{_EN_SUFFIX}\b\.?"
        r"(?:,?\s*(?i:apt|apartment|suite|unit|flat)\.?\s*\w+)?"
    ),
    # Str. Florilor nr. 5, bl. A2, ap. 10 (Romanian)
    re.compile(
        r"\b(?i:strada|str|bd|b-dul|bulevardul|calea|aleea|șoseaua|soseaua|șos|sos|piața|piata|"
        r"splaiul|intrarea)\.?\s+[^\W\d_][\w .'’-]{1,60}?,?\s*(?i:nr|număr|numar|no)\.?\s*\d+"
        r"[A-Za-z]?(?:,?\s*(?i:bl|sc|et|ap)\.?\s*\w+)*"
    ),
    # Hauptstraße 5 (German)
    re.compile(
        r"\b[A-ZÄÖÜ][\wäöüß-]*(?:straße|strasse|str\.|weg|gasse|platz|allee|ring|damm)\s+\d+[a-z]?\b"
    ),
)


def extract_patterns(text: str, document: str | None = None) -> list[ExtractedField]:
    lines = text.splitlines()
    for index, line in enumerate(lines):
        for pattern in _STREET_PATTERNS:
            match = pattern.search(line)
            if not match:
                continue
            address = clean_value(match.group())
            rest = line[match.end() :]
            if rest.lstrip().startswith(","):
                address += ", " + clean_value(rest)
            for extra in lines[index + 1 : index + 3]:
                if not is_address_continuation(extra):
                    break
                address += ", " + clean_value(extra)
            return _address_fields(address, document)
    return []


def _address_fields(address: str, document: str | None) -> list[ExtractedField]:
    fields = [
        ExtractedField(
            name="full_address",
            value=address,
            confidence=STREET_CONFIDENCE,
            source="pattern",
            evidence=address,
            document=document,
        )
    ]
    for name, value in parse_address(address).items():
        fields.append(
            ExtractedField(
                name=name,
                value=value,
                confidence=STREET_CONFIDENCE if name == "street_address" else LOCALITY_CONFIDENCE,
                source="pattern",
                evidence=address,
                document=document,
            )
        )
    return fields
