"""Label-based extraction: ``First name: John``, ``Nume: POPESCU``, tables flattened to
``label: value`` by the readers, and labels written on their own line above the value."""

from __future__ import annotations

import re
from functools import lru_cache

from docfill.extraction.fields import FIELDS, FieldSpec
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

INLINE_CONFIDENCE = 0.95
NEXT_LINE_CONFIDENCE = 0.8
_MAX_ADDRESS_LINES = 4

# Optional "(as in passport)" note, then a ':', '|', '=' or ' - ' separator.
_NOTE = r"(?:\s*\([^)]{0,40}\))?"
_SEPARATOR = rf"{_NOTE}\s*(?:[:|=]|[-–](?=\s))"


@lru_cache
def _synonym_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for spec in FIELDS.values():
        for synonym in spec.synonyms:
            key = collapse(fold(synonym))
            if index.get(key, spec.name) != spec.name:
                raise ValueError(f"Synonym '{synonym}' is used by two fields")
            index[key] = spec.name
    return index


@lru_cache
def _patterns() -> tuple[re.Pattern[str], re.Pattern[str]]:
    synonyms = sorted(_synonym_index(), key=len, reverse=True)
    alternatives = "|".join(re.escape(s).replace(r"\ ", r"\s+") for s in synonyms)
    inline = re.compile(rf"(?<![\w])(?P<label>{alternatives})(?![\w]){_SEPARATOR}\s*")
    alone = re.compile(rf"^[\W\d]{{0,4}}(?P<label>{alternatives}){_NOTE}\s*[:|=]?\s*$")
    return inline, alone


_BULLET_PREFIX = re.compile(r"^[\W\d]{0,6}$")


def _starts_field(folded_line: str, start: int) -> bool:
    """A label must open the line (after numbering/bullets) or follow an earlier
    ``key: value`` pair, so "Company name: ACME" is not read as a person's name."""
    prefix = folded_line[:start]
    return bool(_BULLET_PREFIX.match(prefix)) or any(sep in prefix for sep in ":|=;")


def _field_for(label: str) -> FieldSpec:
    return FIELDS[_synonym_index()[collapse(label)]]


def is_label_line(line: str) -> bool:
    inline, alone = _patterns()
    folded = fold(line)
    return bool(alone.match(folded) or inline.search(folded))


def validate(spec: FieldSpec, value: str, strict: bool = False) -> str | None:
    """Return the normalised value, or ``None`` if it does not fit the field."""
    value = clean_value(value)
    if not value or len(value) > spec.max_length or not any(c.isalnum() for c in value):
        return None
    if spec.kind == "name":
        return normalize_name(value) if looks_like_name(value, strict=strict) else None
    if spec.kind == "postal_code":
        if looks_like_postal_code(value):
            return value.upper()
        match = re.search(POSTAL_CODE, value.upper())
        return match.group() if match and any(c.isdigit() for c in match.group()) else None
    if spec.kind in ("place", "country"):
        if not any(c.isalpha() for c in value) or (
            spec.kind == "country" and any(c.isdigit() for c in value)
        ):
            return None
        if strict and not value[0].isupper():
            return None
        return normalize_name(value)
    return value


def _following_lines(lines: list[str], start: int) -> list[str]:
    """The lines after ``start`` up to the next blank line."""
    block: list[str] = []
    for line in lines[start:]:
        if not line.strip():
            break
        block.append(line)
    return block


def _value_below(spec: FieldSpec, lines: list[str], index: int) -> str | None:
    """Value written under a label (``First name`` / ``Anna``); addresses may span lines."""
    following = _following_lines(lines, index + 1)
    if not following or is_label_line(following[0]):
        return None
    block = [following[0]]
    if spec.kind == "address":
        for extra in following[1:_MAX_ADDRESS_LINES]:
            if is_label_line(extra):
                break
            block.append(extra)
    return ", ".join(clean_value(part) for part in block)


def extract_labeled(text: str, document: str | None = None) -> list[ExtractedField]:
    inline, alone = _patterns()
    lines = text.splitlines()
    found: list[ExtractedField] = []

    def emit(spec: FieldSpec, raw: str | None, confidence: float, evidence: str) -> None:
        value = validate(spec, raw, strict=confidence < INLINE_CONFIDENCE) if raw else None
        if value:
            found.append(
                ExtractedField(
                    name=spec.name,
                    value=value,
                    confidence=confidence,
                    source="label",
                    evidence=evidence.strip(),
                    document=document,
                )
            )

    for index, line in enumerate(lines):
        folded = fold(line)
        matches = [m for m in inline.finditer(folded) if _starts_field(folded, m.start())]

        if not matches:
            if match := alone.match(folded):
                spec = _field_for(match["label"])
                emit(spec, _value_below(spec, lines, index), NEXT_LINE_CONFIDENCE, line)
            continue

        for position, match in enumerate(matches):
            is_last = position + 1 == len(matches)
            end = len(line) if is_last else matches[position + 1].start()
            spec = _field_for(match["label"])
            raw = clean_value(line[match.end() : end])
            if is_last and not raw:
                # "First name:" with the value on the next line.
                emit(spec, _value_below(spec, lines, index), NEXT_LINE_CONFIDENCE, line)
                continue
            if is_last and spec.kind == "address":
                # "Address: 12 Baker Street" followed by "London NW1 6XE" on the next line.
                for extra in _following_lines(lines, index + 1)[: _MAX_ADDRESS_LINES - 1]:
                    if is_label_line(extra) or not is_address_continuation(extra):
                        break
                    raw = f"{raw}, {clean_value(extra)}"
            emit(spec, raw, INLINE_CONFIDENCE, line)
    return found
