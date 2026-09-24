"""Step-by-step review helpers shared by the web wizard and the ``docfill wizard`` command.

Everything the pipeline decided is exposed so a person can check and correct it: for every
field of the chosen reference documents the proposed value, its confidence, how it was found
(label, pattern, MRZ, filled form, ML model, derived, remembered, default), the text it was
found in, the other candidates and any validation problem.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from docfill.extraction.derive import complete_values
from docfill.extraction.fields import FIELDS, GROUPS
from docfill.models import ExtractedField, ExtractionResult
from docfill.templates.models import StandardDocument
from docfill.templates.placeholders import apply, preview_lines, render_body
from docfill.validation import validate_values

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
    issues: list[str] = field(default_factory=list)

    @classmethod
    def of(cls, extracted: ExtractedField) -> Candidate:
        return cls(
            value=extracted.value,
            confidence=round(extracted.confidence, 2),
            source=extracted.source,
            evidence=extracted.evidence,
            document=extracted.document,
            issues=list(extracted.issues),
        )


@dataclass
class FieldRow:
    """One field of the reference documents, ready for review."""

    name: str
    label: str
    required: bool
    group: str = "other"
    kind: str = "text"
    remember: bool = False
    choices: list[str] = field(default_factory=list)
    value: str = ""
    found: Candidate | None = None
    alternatives: list[Candidate] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _templates(templates: StandardDocument | Sequence[StandardDocument]) -> list[StandardDocument]:
    return [templates] if isinstance(templates, StandardDocument) else list(templates)


def field_rows(
    templates: StandardDocument | Sequence[StandardDocument],
    extraction: ExtractionResult | None,
    min_confidence: float,
    memory: Mapping[str, str] | None = None,
    extra_fields: Sequence[str] = (),
    extra_required: Sequence[str] = (),
) -> list[FieldRow]:
    """The fields of the reference documents in document order, each prefilled with, in this
    order: a confident extracted value, a remembered value (``remember`` fields), the
    document's default. A best candidate under ``min_confidence`` is only proposed.

    ``extra_fields`` / ``extra_required`` are asked for by the procedure (e.g. the share
    capital, needed by its legal checks) even when no form prints them."""
    templates = _templates(templates)
    extraction = extraction or ExtractionResult()
    memory = memory or {}
    names: list[str] = []
    required: set[str] = set(extra_required)
    defaults: dict[str, str] = {}
    remember: set[str] = set()
    choices: dict[str, list[str]] = {}
    for template in templates:
        names += [n for n in template.field_names() if n not in names]
        required |= set(template.required_fields())
        for name, value in template.defaults.items():
            defaults.setdefault(name, value)
        remember |= set(template.remember)
        for pdf_field, options in template.choices.items():
            expression = template.field_map.get(pdf_field, "")
            choices[expression.split("|")[0].strip()] = list(options)
    names += [n for n in [*extra_fields, *extra_required] if n not in names]

    rows: list[FieldRow] = []
    for name in names:
        spec = FIELDS.get(name)
        row = FieldRow(
            name=name,
            label=field_label(name),
            required=name in required,
            group=spec.group if spec else ("filing" if name == "today" else "other"),
            kind=spec.kind if spec else "text",
            remember=name in remember,
            choices=choices.get(name, []),
        )
        best = extraction.fields.get(name)
        if best and best.confidence >= min_confidence:
            row.value, row.found = best.value, Candidate.of(best)
        elif name in remember and memory.get(name):
            row.value = memory[name]
            row.found = Candidate(row.value, 1.0, "memory", "remembered from your last document")
        elif name == "today":
            row.value = date.today().strftime("%d.%m.%Y")
            row.found = Candidate(value=row.value, confidence=1.0, source="system")
        elif name in defaults:
            row.value = defaults[name]
            row.found = Candidate(row.value, 1.0, "default", "default of the reference document")
        row.alternatives = [
            Candidate.of(candidate)
            for candidate in extraction.candidates.get(name, [])
            if candidate.value != row.value
        ][:MAX_ALTERNATIVES]
        rows.append(row)
    return rows


def other_fields(
    templates: StandardDocument | Sequence[StandardDocument], extraction: ExtractionResult
) -> list[dict[str, Any]]:
    """Extracted data the reference documents do not use (shown for transparency)."""
    used = {name for template in _templates(templates) for name in template.field_names()}
    return [
        {"name": name, "label": field_label(name), **asdict(Candidate.of(extracted))}
        for name, extracted in extraction.fields.items()
        if name not in used
    ]


def build_preview(template: StandardDocument, values: Mapping[str, str]) -> dict[str, Any]:
    """Live preview of one filled reference document (``values`` already cleaned)."""
    missing = [name for name in template.required_fields() if name not in values]
    names = template.field_names()
    filled = [name for name in names if name in values]
    preview: dict[str, Any] = {
        "name": template.name,
        "kind": template.kind,
        "title": template.title,
        "missing": missing,
        "filled": len(filled),
        "total": len(names),
    }
    if template.kind == "text":
        preview["lines"] = preview_lines(template.body or "", values)
    else:
        rows = []
        for pdf_field, expression in template.field_map.items():
            if "{{" in expression:
                value = " ".join(render_body(expression, values, blank="").split()) or None
                parts = [e.split("|")[0].strip() for e in re.findall(r"\{\{(.*?)\}\}", expression)]
                label = " + ".join(field_label(part).split(" (")[0] for part in parts)
            else:
                value = apply(expression, values)
                label = field_label(expression.split("|")[0].strip())
            rows.append(
                {
                    "pdf_field": pdf_field,
                    "label": label,
                    "value": value,
                    "field": expression.split("|")[0].strip(),
                }
            )
        for name in template.lists:
            rows.append(
                {
                    "pdf_field": name,
                    "label": field_label(name),
                    "value": values.get(name),
                    "field": name,
                }
            )
        preview["form_fields"] = rows
    return preview


def assist(values: dict[str, str]) -> dict[str, Any]:
    """Live help for the values being edited: problems found and derivable suggestions."""
    return {
        "issues": validate_values(values),
        "derived": {
            name: {"value": value, "reason": reason}
            for name, (value, reason) in complete_values(values).items()
        },
    }


# --------------------------------------------------------------------------- output files


def safe_filename(name: str, suffix: str = ".pdf") -> str:
    """A portable file name: ASCII letters, digits, ``.``, ``_`` and ``-``; no directories."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().endswith(suffix):
        name = name[: -len(suffix)]
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_name).strip("._-")[:120]
    return (stem or "document") + suffix


def suggest_filename(
    templates: StandardDocument | Sequence[StandardDocument], values: Mapping[str, str]
) -> str:
    """``<template>_<company or person>_<date>``; with several documents the template name is
    left out (each file gets it as a suffix)."""
    templates = _templates(templates)
    subject = values.get("company_name") if any(t.kind == "pdf_form" for t in templates) else None
    parts = [templates[0].name] if len(templates) == 1 else []
    parts += [subject] if subject else [values.get("last_name"), values.get("first_name")]
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


GROUP_LABELS = {**GROUPS}


# --------------------------------------------------------------------------- forms tooling


def suggest_field(label: str) -> str | None:
    """Which docfill field a label printed next to a PDF form field most likely means."""
    from docfill.extraction.rules import general_labels

    folded = " ".join(
        "".join(c for c in unicodedata.normalize("NFD", label) if unicodedata.category(c) != "Mn")
        .lower()
        .replace(".", " ")
        .split()
    )
    for synonym in sorted(general_labels(), key=len, reverse=True):
        if folded == synonym or folded.endswith(" " + synonym):
            return general_labels()[synonym]
    return None


def _comparable(value: str | None) -> str:
    return " ".join((value or "").split()).strip(" ,.;").casefold()


def compare_forms(
    expected: Mapping[str, str], produced: Mapping[str, str], fields: Sequence[str]
) -> dict[str, Any]:
    """Compare two filled copies of a form on ``fields`` (ignoring case, spacing and trailing
    punctuation). ``expected`` is the correctly filled copy."""
    differences: list[tuple[str, str]] = []
    matched = total = 0
    for name in dict.fromkeys(fields):
        want, got = _comparable(expected.get(name)), _comparable(produced.get(name))
        if want:
            total += 1
        if want == got:
            matched += bool(want)
            continue
        if not got:
            problem = "missing"
        elif not want:
            problem = "filled, but empty in the expected copy"
        elif _fold_only(want) == _fold_only(got):
            problem = "differs only in diacritics / spacing"
        else:
            problem = "different value"
        differences.append((name, problem))
    return {
        "expected": total,
        "matched": matched,
        "accuracy": matched / total if total else 1.0,
        "differences": differences,
    }


def _fold_only(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return re.sub(r"[\s.]", "", "".join(c for c in decomposed if unicodedata.category(c) != "Mn"))
