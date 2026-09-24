"""Unlabelled data found with regular expressions: street + locality lines, and on identity
documents the CNP, ``SERIA AX NR 123456``, the validity range and the machine readable zone."""

from __future__ import annotations

import re

from docfill.extraction.text_utils import clean_value, is_address_continuation, parse_address
from docfill.models import ExtractedField
from docfill.mrz import read_mrz, split_ro_document_number
from docfill.ro import CITIZENSHIP_BY_CODE, check_cnp, format_date, normalize_date

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
        r"splaiul|intrarea)(?:\.\s*|\s+)[^\W\d_][\w .'’-]{1,60}?,?\s*"
        r"(?i:nr|număr|numar|no)\.?\s*\d+[A-Za-z]?(?:,?\s*(?i:bl|sc|et|ap)\.?\s*\w+)*"
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


# --------------------------------------------------------------------------- identity documents

_CNP = re.compile(r"(?<!\d)[1-9](?:\s?\d){12}(?!\d)")
_SERIES_NUMBER = re.compile(
    r"\bSERIA\s*[:.]?\s*(?P<series>[A-Z]{2})\s*(?:NR|N[RO])\s*[:.]?\s*(?P<number>\d{6,7})\b",
    re.IGNORECASE,
)
_VALIDITY = re.compile(
    r"(?P<issued>\d{2}\.\d{2}\.\d{2,4})\s*[-–—]\s*(?P<expires>\d{2}\.\d{2}\.\d{4})"
)


def _field(
    name: str, value: str, confidence: float, source: str, evidence: str, document: str | None
) -> ExtractedField:
    return ExtractedField(
        name=name,
        value=value,
        confidence=confidence,
        source=source,
        evidence=evidence,
        document=document,
    )


def extract_identity(
    text: str, document: str | None = None, doc_type: str | None = None
) -> list[ExtractedField]:
    """CNP (checksum-valid only), series/number, validity range and the MRZ of ID cards."""
    on_id_card = doc_type == "id_card"
    found: list[ExtractedField] = []
    for match in _CNP.finditer(text):
        cnp = match.group().replace(" ", "")
        if check_cnp(cnp).valid:
            found.append(_field("cnp", cnp, 0.9, "pattern", match.group(), document))
            break
    if match := _SERIES_NUMBER.search(text):
        confidence = 0.9 if on_id_card else 0.7
        found.append(
            _field(
                "id_series", match["series"].upper(), confidence, "pattern", match.group(), document
            )
        )
        found.append(
            _field("id_number", match["number"], confidence, "pattern", match.group(), document)
        )
    if (match := _VALIDITY.search(text)) and (on_id_card or "valab" in text.lower()):
        issued, expires = normalize_date(match["issued"]), normalize_date(match["expires"])
        if issued and expires:
            found.append(_field("id_issue_date", issued, 0.85, "pattern", match.group(), document))
            found.append(
                _field("id_expiry_date", expires, 0.85, "pattern", match.group(), document)
            )
    found.extend(_from_mrz(text, document))
    return found


def _from_mrz(text: str, document: str | None) -> list[ExtractedField]:
    mrz = read_mrz(text)
    if mrz is None:
        return []
    evidence = f"MRZ ({mrz.layout})"
    found: list[ExtractedField] = []
    # Names have no check digit and no diacritics: useful to confirm, weaker on their own.
    if mrz.surname:
        found.append(_field("last_name", mrz.surname.title(), 0.7, "mrz", evidence, document))
    if mrz.given_names:
        found.append(_field("first_name", mrz.given_names.title(), 0.7, "mrz", evidence, document))
    if mrz.valid.get("document_number"):
        split = split_ro_document_number(mrz.document_number)
        if split and mrz.issuing_state == "ROU":
            found.append(_field("id_series", split[0], 0.97, "mrz", evidence, document))
            found.append(_field("id_number", split[1], 0.97, "mrz", evidence, document))
    if mrz.valid.get("birth_date") and mrz.birth_date:
        found.append(
            _field("date_of_birth", format_date(mrz.birth_date), 0.95, "mrz", evidence, document)
        )
    if mrz.valid.get("expiry_date") and mrz.expiry_date:
        found.append(
            _field("id_expiry_date", format_date(mrz.expiry_date), 0.95, "mrz", evidence, document)
        )
    if mrz.sex in ("M", "F"):
        found.append(_field("sex", mrz.sex, 0.85, "mrz", evidence, document))
    if mrz.nationality in CITIZENSHIP_BY_CODE:
        found.append(
            _field(
                "citizenship", CITIZENSHIP_BY_CODE[mrz.nationality], 0.8, "mrz", evidence, document
            )
        )
    if mrz.document_code.startswith("I") and mrz.issuing_state == "ROU":
        found.append(_field("id_type", "CI", 0.9, "mrz", evidence, document))
    return found
