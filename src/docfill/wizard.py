"""Step-by-step review helpers shared by the web wizard and the ``docfill wizard`` command.

Everything the pipeline decided is exposed so a person can check and correct it: the value
chosen for each field of the reference document, its confidence, how it was found (label rule,
pattern, ML model, derived), the text it was found in, and the other candidates.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from docfill.extraction.fields import FIELDS
from docfill.models import ExtractedField, ExtractionResult
from docfill.templates.models import StandardDocument
from docfill.templates.placeholders import apply, preview_lines

MAX_ALTERNATIVES = 4
SPECIAL_LABELS = {"today": "Date (today)"}


def field_label(name: str) -> str:
    if name in FIELDS:
        return FIELDS[name].label
    return SPECIAL_LABELS.get(name, name.replace("_", " ").capitalize())


@dataclass
class Candidate:
    value: str
    confidence: float
    source: str
    evidence: str | None = None
    document: str | None = None

    @classmethod
    def of(cls, extracted: ExtractedField) -> Candidate:
        return cls(
            value=extracted.value,
            confidence=round(extracted.confidence, 2),
            source=extracted.source,
            evidence=extracted.evidence,
            document=extracted.document,
        )


@dataclass
class FieldRow:
    """One field of the reference document, ready for review."""

    name: str
    label: str
    required: bool
    value: str = ""
    found: Candidate | None = None
    alternatives: list[Candidate] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def field_rows(
    template: StandardDocument, extraction: ExtractionResult | None, min_confidence: float
) -> list[FieldRow]:
    """The template's fields in document order, prefilled with confident extracted values.

    A best candidate under ``min_confidence`` is not prefilled; it is offered as an
    alternative the user can pick.
    """
    extraction = extraction or ExtractionResult()
    required = set(template.required_fields())
    rows: list[FieldRow] = []
    for name in template.field_names():
        row = FieldRow(name=name, label=field_label(name), required=name in required)
        best = extraction.fields.get(name)
        if best and best.confidence >= min_confidence:
            row.value = best.value
            row.found = Candidate.of(best)
        elif name == "today":
            row.value = date.today().isoformat()
            row.found = Candidate(value=row.value, confidence=1.0, source="system")
        row.alternatives = [
            Candidate.of(candidate)
            for candidate in extraction.candidates.get(name, [])
            if candidate.value != row.value
        ][:MAX_ALTERNATIVES]
        rows.append(row)
    return rows


def other_fields(template: StandardDocument, extraction: ExtractionResult) -> list[dict[str, Any]]:
    """Extracted data the reference document does not use (shown for transparency)."""
    used = set(template.field_names())
    return [
        {"name": name, "label": field_label(name), **asdict(Candidate.of(extracted))}
        for name, extracted in extraction.fields.items()
        if name not in used
    ]


def build_preview(template: StandardDocument, values: Mapping[str, str]) -> dict[str, Any]:
    """Live preview of the filled reference document (``values`` already cleaned)."""
    missing = [name for name in template.required_fields() if name not in values]
    names = template.field_names()
    filled = [name for name in names if name in values]
    preview: dict[str, Any] = {
        "kind": template.kind,
        "title": template.title,
        "missing": missing,
        "filled": len(filled),
        "total": len(names),
    }
    if template.kind == "text":
        preview["lines"] = preview_lines(template.body or "", values)
    else:
        preview["form_fields"] = [
            {"pdf_field": pdf_field, "expression": expression, "value": apply(expression, values)}
            for pdf_field, expression in template.field_map.items()
        ]
    return preview


# --------------------------------------------------------------------------- output files


def safe_filename(name: str, suffix: str = ".pdf") -> str:
    """A portable file name: ASCII letters, digits, ``.``, ``_`` and ``-``; no directories."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().endswith(suffix):
        name = name[: -len(suffix)]
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_name).strip("._-")[:120]
    return (stem or "document") + suffix


def suggest_filename(template: StandardDocument, values: Mapping[str, str]) -> str:
    parts = [template.name, values.get("last_name"), values.get("first_name")]
    parts.append(date.today().isoformat())
    return safe_filename("_".join(part for part in parts if part))


def save_output(directory: Path, filename: str, data: bytes) -> Path:
    """Write ``data`` as ``directory/filename``, adding ``-2``, ``-3``... instead of
    overwriting an existing file."""
    directory.mkdir(parents=True, exist_ok=True)
    name = safe_filename(filename)
    stem, suffix = name[: -len(".pdf")], ".pdf"
    path = directory / name
    counter = 2
    while True:
        try:
            with path.open("xb") as handle:  # exclusive create: never overwrite
                handle.write(data)
            return path
        except FileExistsError:
            path = directory / f"{stem}-{counter}{suffix}"
            counter += 1
