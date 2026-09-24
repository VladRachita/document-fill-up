"""Label-based extraction: ``First name: John``, ``Nume: POPESCU``, multilingual identity card
labels (``Nume/Nom/Last name`` with the value on the next line), tables flattened to
``label: value`` by the readers, labels specific to a document type and labels learned from the
user's corrections."""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import lru_cache

from docfill.extraction.fields import FIELDS, TYPE_SYNONYMS, FieldSpec
from docfill.extraction.text_utils import (
    POSTAL_CODE,
    clean_value,
    collapse,
    fold,
    is_address_continuation,
    looks_like_name,
    looks_like_postal_code,
    normalize_name,
)
from docfill.models import ExtractedField
from docfill.ro import (
    CITIZENSHIP_NORMAL,
    looks_romanian_address,
    normalize_date,
    normalize_sex,
    restore_diacritics,
)
from docfill.sanitize import _iban_valid

INLINE_CONFIDENCE = 0.95
NEXT_LINE_CONFIDENCE = 0.8
# Identity cards always print the label above the value (and the MRZ/CNP cross-check them).
NEXT_LINE_CONFIDENCE_BY_TYPE = {"id_card": 0.92}
LEARNED_CONFIDENCE = 0.85
_MAX_ADDRESS_LINES = 4

# Optional "(as in passport)" note, then a ':', '|', '=' or ' - ' separator.
_NOTE = r"(?:\s*\([^)]{0,40}\))?"
# Other language versions of the same label: "Nume/Nom/Last name".
_TAIL = r"(?:\s*/\s*[^\W\d_][\w'’ ]{0,28}?)*"
_SEPARATOR = rf"{_TAIL}{_NOTE}\s*(?:[:|=]|[-–](?=\s))"
_BULLET_PREFIX = re.compile(r"^[\W\d]{0,6}$")
_DATE_IN_TEXT = re.compile(
    r"\d{1,2}\s*[./-]\s*\d{1,2}\s*[./-]\s*\d{2,4}|\d{4}-\d{2}-\d{2}|\d{1,2}\s+[A-Za-zăâîșț]+\s+\d{4}"
)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_TRUTHY = {"x", "da", "yes", "1", "true", "on", "bifat", "✓"}


def _key(label: str) -> str:
    return collapse(fold(label))


@lru_cache
def general_labels() -> dict[str, str]:
    index: dict[str, str] = {}
    for spec in FIELDS.values():
        for synonym in spec.synonyms:
            key = _key(synonym)
            if index.get(key, spec.name) != spec.name:
                raise ValueError(f"Synonym '{synonym}' is used by two fields")
            index[key] = spec.name
    return index


def label_index(
    doc_type: str | None = None, learned: Mapping[str, str] | None = None
) -> dict[str, str]:
    """All labels for a document: general ones, then type-specific and learned ones on top."""
    index = dict(general_labels())
    index.update({_key(k): v for k, v in TYPE_SYNONYMS.get(doc_type or "", {}).items()})
    index.update({_key(k): v for k, v in (learned or {}).items() if v in FIELDS})
    return index


@lru_cache(maxsize=64)
def _patterns(labels: tuple[str, ...]) -> tuple[re.Pattern[str], re.Pattern[str]]:
    ordered = sorted(labels, key=len, reverse=True)
    alternatives = "|".join(re.escape(s).replace(r"\ ", r"\s+") for s in ordered)
    inline = re.compile(rf"(?<![\w])(?P<label>{alternatives})(?![\w]){_SEPARATOR}\s*")
    alone = re.compile(
        rf"^[\W\d]{{0,4}}(?P<label>{alternatives})(?![\w]){_TAIL}{_NOTE}\s*[:|=]?\s*$"
    )
    return inline, alone


def _starts_field(folded_line: str, start: int) -> bool:
    """A label must open the line (after numbering/bullets) or follow an earlier
    ``key: value`` pair, so "Company name: ACME" is not read as a person's name."""
    prefix = folded_line[:start]
    return bool(_BULLET_PREFIX.match(prefix)) or any(sep in prefix for sep in ":|=;")


def validate(spec: FieldSpec, value: str, strict: bool = False) -> str | None:
    """Return the normalised value, or ``None`` if it does not fit the field."""
    value = clean_value(value)
    if not value or len(value) > spec.max_length or not any(c.isalnum() for c in value):
        return None
    if _key(value) in general_labels():  # "Nume: Prenume ...." on an empty form
        return None
    kind = spec.kind
    if kind == "name":
        return normalize_name(value) if looks_like_name(value, strict=strict) else None
    if kind == "cnp":
        digits = re.sub(r"[\s.-]", "", value)
        return digits if re.fullmatch(r"\d{13}", digits) else None
    if kind == "date":
        found = _DATE_IN_TEXT.search(value)
        return normalize_date(found.group()) if found else None
    if kind == "sex":
        return normalize_sex(value.split()[0])
    if kind == "email":
        found = _EMAIL.search(value)
        return found.group() if found else None
    if kind == "phone":
        phone = re.sub(r"[^\d+]", "", value)
        return phone if 6 <= len(phone.lstrip("+")) <= 15 else None
    if kind == "iban":
        iban = value.replace(" ", "").upper()
        return iban if 15 <= len(iban) <= 34 and _iban_valid(iban) else None
    if kind == "id_series":
        series = value.upper().replace(" ", "")
        return series if re.fullmatch(r"[A-Z]{1,2}", series) else None
    if kind == "id_number":
        number = value.replace(" ", "")
        return number if re.fullmatch(r"[A-Z0-9]{5,10}", number.upper()) else None
    if kind == "code":
        return value if re.fullmatch(r"\d{2,6}", value) else None
    if kind == "checkbox":
        return "x" if value.lower() in _TRUTHY else None
    if kind == "postal_code":
        if looks_like_postal_code(value):
            return value.upper()
        match = re.search(POSTAL_CODE, value.upper())
        return match.group() if match and any(c.isdigit() for c in match.group()) else None
    if kind in ("place", "country"):
        if not any(c.isalpha() for c in value) or (
            kind == "country" and any(c.isdigit() for c in value)
        ):
            return None
        if strict and not value[0].isupper():
            return None
        return restore_diacritics(normalize_name(value))
    if spec.name == "citizenship":  # "Română / ROU" on identity cards
        value = re.split(r"\s*/\s*", value)[0]
        return CITIZENSHIP_NORMAL.get(_key(value), value)
    if spec.name == "id_issued_by":  # "SPCLEP Sibiu 22.06.22-14.11.2032" when columns merge
        value = re.sub(r"\s*\d{2}\.\d{2}\.\d{2,4}.*$", "", value)
        return restore_diacritics(value) or None
    return value


def _following_lines(lines: list[str], start: int, skip_blank: bool = False) -> list[str]:
    """The lines after ``start`` up to the next blank line. With ``skip_blank`` single blank
    lines are skipped (OCR often puts one between a label and its value) and two end it."""
    block: list[str] = []
    blanks = 0
    for line in lines[start:]:
        if not line.strip():
            blanks += 1
            if not skip_blank or blanks > 1 or not block and blanks > 1:
                break
            continue
        blanks = 0
        block.append(line)
    return block


def extract_labeled(
    text: str,
    document: str | None = None,
    doc_type: str | None = None,
    learned: Mapping[str, str] | None = None,
) -> list[ExtractedField]:
    index = label_index(doc_type, learned)
    learned_keys = {_key(k) for k in (learned or {})}
    below = NEXT_LINE_CONFIDENCE_BY_TYPE.get(doc_type or "", NEXT_LINE_CONFIDENCE)
    inline, alone = _patterns(tuple(sorted(index)))
    lines = text.splitlines()
    found: list[ExtractedField] = []

    def is_label_line(line: str) -> bool:
        folded = fold(line)
        return bool(alone.match(folded) or inline.search(folded))

    def value_below(spec: FieldSpec, index_: int) -> str | None:
        """The value under a label; OCR may put one blank line in between. Addresses may span
        several lines, continuing past a blank line only with more address-looking text."""
        block: list[str] = []
        blank_before = False
        for line in lines[index_ + 1 :]:
            if not line.strip():
                if blank_before or (block and spec.kind != "address"):
                    break
                blank_before = True
                continue
            if is_label_line(line):
                break
            if block and (
                spec.kind != "address"
                or len(block) >= _MAX_ADDRESS_LINES
                or blank_before
                and not (looks_romanian_address(line) or is_address_continuation(line))
            ):
                break
            block.append(line)
            blank_before = False
        return ", ".join(clean_value(part) for part in block) or None

    def emit(label: str, raw: str | None, confidence: float, evidence: str) -> None:
        spec = FIELDS[index[collapse(label)]]
        is_learned = collapse(label) in learned_keys
        if is_learned:
            confidence = min(confidence, LEARNED_CONFIDENCE)
        value = validate(spec, raw, strict=confidence < INLINE_CONFIDENCE) if raw else None
        if value:
            found.append(
                ExtractedField(
                    name=spec.name,
                    value=value,
                    confidence=confidence,
                    source="learned" if is_learned else "label",
                    evidence=evidence.strip(),
                    document=document,
                )
            )

    for number, line in enumerate(lines):
        folded = fold(line)
        matches = [m for m in inline.finditer(folded) if _starts_field(folded, m.start())]

        if not matches:
            if match := alone.match(folded):
                spec = FIELDS[index[collapse(match["label"])]]
                emit(match["label"], value_below(spec, number), below, line)
            continue

        for position, match in enumerate(matches):
            is_last = position + 1 == len(matches)
            end = len(line) if is_last else matches[position + 1].start()
            spec = FIELDS[index[collapse(match["label"])]]
            raw = clean_value(line[match.end() : end])
            if is_last and not raw:
                # "First name:" with the value on the next line.
                emit(match["label"], value_below(spec, number), below, line)
                continue
            if is_last and spec.kind == "address":
                # "Address: 12 Baker Street" followed by "London NW1 6XE" on the next line.
                for extra in _following_lines(lines, number + 1)[: _MAX_ADDRESS_LINES - 1]:
                    if is_label_line(extra) or not is_address_continuation(extra):
                        break
                    raw = f"{raw}, {clean_value(extra)}"
            emit(match["label"], raw, INLINE_CONFIDENCE, line)
    return found


def is_label_line(line: str, doc_type: str | None = None) -> bool:
    inline, alone = _patterns(tuple(sorted(label_index(doc_type))))
    folded = fold(line)
    return bool(alone.match(folded) or inline.search(folded))
