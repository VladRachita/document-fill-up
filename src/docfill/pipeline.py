"""End-to-end pipeline: read -> sanitize -> extract -> fill a standard document -> PDF."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from docfill.config import Settings, get_settings
from docfill.doctypes import DocTypeClassifier, Prediction, match_form
from docfill.errors import MissingFieldsError
from docfill.export import fill_pdf_form, render_text_pdf
from docfill.extraction import FieldExtractor, merge_extractions
from docfill.extraction.derive import complete_values
from docfill.extraction.fields import FIELDS
from docfill.extraction.rules import validate
from docfill.models import ExtractedField, ExtractionResult, RawDocument, SanitizedDocument
from docfill.readers import read_bytes
from docfill.sanitize import Sanitizer
from docfill.templates.models import StandardDocument, list_cells
from docfill.templates.placeholders import (
    apply,
    choose,
    clean_fill_value,
    clean_list_value,
    parse_expression,
    render_body,
    split_row,
)


def form_values(template: StandardDocument, values: Mapping[str, str]) -> dict[str, str]:
    """PDF field name -> text for a pdf_form standard document (mapped fields, composed
    fields, option buttons and list rows)."""
    filled: dict[str, str] = {}
    for pdf_field, expression in template.field_map.items():
        if "{{" in expression:  # "{{ last_name | upper }} {{ first_name | upper }}"
            value = " ".join(render_body(expression, values, blank="").split())
        else:
            value = apply(expression, values)
        if value and pdf_field in template.choices:
            value = choose(template.choices[pdf_field], value)
        if value:
            filled[pdf_field] = value
    for pdf_field, names in template.ticks.items():  # section boxes
        if any((values.get(name) or "").strip() for name in names):
            filled[pdf_field] = "x"
    for name, spec in template.lists.items():
        cells = list_cells(spec)
        rows = [row for row in (values.get(name) or "").splitlines() if row.strip()]
        for row_cells, row in zip(cells, rows, strict=False):
            for cell, part in zip(row_cells, split_row(row, len(row_cells)), strict=True):
                if part:
                    filled[cell] = part
    return filled


def read_template_values(
    template: StandardDocument, pdf_values: Mapping[str, str]
) -> dict[str, tuple[str, str]]:
    """Read a filled copy of a standard document back into docfill fields:
    ``{field: (value, pdf_field)}`` through the inverse of its field map."""
    found: dict[str, tuple[str, str]] = {}
    inverse_choices = {
        pdf_field: {state: label for label, state in mapping.items()}
        for pdf_field, mapping in template.choices.items()
    }
    for pdf_field, expression in template.field_map.items():
        value = (pdf_values.get(pdf_field) or "").strip()
        if not value or "{{" in expression:  # composed values cannot be split back
            continue
        if pdf_field in inverse_choices:
            state = next((s for s in inverse_choices[pdf_field] if s == value), None)
            value = inverse_choices[pdf_field].get(state, "") if state else ""
        name, _ = parse_expression(expression)
        if value and name not in found:
            found[name] = (value, pdf_field)
    for name, spec in template.lists.items():
        cells = list_cells(spec)
        rows = []
        for row_cells in cells:
            parts = [(pdf_values.get(cell) or "").strip() for cell in row_cells]
            if any(parts):
                separator = " | " if len(row_cells) > 2 else " "
                rows.append(separator.join(parts).strip(" |"))
        if rows:
            found[name] = ("\n".join(rows), cells[0][0])
    return found


def form_fields_as_candidates(
    template: StandardDocument, pdf_values: Mapping[str, str], document: str
) -> list[ExtractedField]:
    """Values of a recognised filled form: structured data, trusted almost like a manual entry."""
    candidates = []
    for name, (value, pdf_field) in read_template_values(template, pdf_values).items():
        spec = FIELDS.get(name)
        if spec is None or spec.kind == "list":
            normalised = value
        else:  # normalise like any extracted value; keep the typed text if it does not validate
            normalised = validate(spec, value) or clean_fill_value(value)
        if name == "id_type":
            normalised = normalised.upper()
        candidates.append(
            ExtractedField(
                name=name,
                value=normalised,
                confidence=FORM_CONFIDENCE,
                source="form",
                evidence=f"{template.title}: field '{pdf_field}'",
                document=document,
            )
        )
    return candidates


@dataclass
class DocumentAnalysis:
    raw: RawDocument
    sanitized: SanitizedDocument
    extraction: ExtractionResult
    doc_type: Prediction | None = None


FORM_CONFIDENCE = 0.97


@dataclass
class FillResult:
    pdf: bytes
    template: str
    template_version: int
    values: dict[str, str]
    sources: dict[str, str]
    missing: list[str]
    warnings: list[str] = field(default_factory=list)


class DocFill:
    def __init__(
        self,
        settings: Settings | None = None,
        extractor: FieldExtractor | None = None,
        sanitizer: Sanitizer | None = None,
        classifier: DocTypeClassifier | None = None,
        templates: Callable[[], list[StandardDocument]] | None = None,
        memory: Callable[[], dict[str, str]] | None = None,
    ):
        self.settings = settings or get_settings()
        self.extractor = extractor or FieldExtractor(self.settings)
        self.sanitizer = sanitizer or Sanitizer()
        self.classifier = classifier or DocTypeClassifier()
        # Registered standard documents, used to recognise and read filled PDF forms.
        self.templates = templates or (lambda: [])
        # Remembered values of ``remember`` fields (the filer's own details).
        self.memory = memory or (lambda: {})

    # ------------------------------------------------------------------ scanning

    def detect_type(
        self, raw: RawDocument, sanitized: SanitizedDocument
    ) -> tuple[Prediction, StandardDocument | None]:
        if raw.form_values:
            templates = [t for t in self.templates() if t.kind == "pdf_form"]
            match = match_form(
                raw.form_values, ((t.name, t.doc_type, t.pdf_field_names()) for t in templates)
            )
            if match:  # several standard documents can share a form (e.g. Anexa 2a variants)
                filled = set(raw.form_values)
                template = max(
                    (t for t in templates if t.doc_type == match.doc_type),
                    key=lambda t: len(filled & set(t.pdf_field_names())),
                )
                return match, template
        return self.classifier.predict(sanitized.text), None

    def analyze_bytes(
        self, data: bytes, filename: str, doc_type: str | None = None
    ) -> DocumentAnalysis:
        """Read, sanitize, detect the document type (unless given) and extract fields."""
        raw = read_bytes(data, filename, self.settings)
        sanitized = self.sanitizer.sanitize(raw)
        prediction, template = self.detect_type(raw, sanitized)
        if doc_type and doc_type != prediction.doc_type:
            prediction = Prediction(doc_type, 1.0, "user")
            template = next((t for t in self.templates() if t.doc_type == doc_type), None)
        extra = (
            form_fields_as_candidates(template, raw.form_values, filename)
            if template and raw.form_values
            else []
        )
        extraction = self.extractor.extract(
            sanitized, prediction.doc_type, extra, use_text=not extra
        )
        return DocumentAnalysis(raw, sanitized, extraction, prediction)

    def analyze_file(self, path: str | Path) -> DocumentAnalysis:
        path = Path(path)
        return self.analyze_bytes(path.read_bytes(), path.name)

    @staticmethod
    def combine(analyses: Iterable[DocumentAnalysis]) -> ExtractionResult:
        """Merge several source documents; the most confident value per field wins."""
        return merge_extractions(analysis.extraction for analysis in analyses)

    def extract_texts(
        self, documents: Iterable[tuple[str, str] | tuple[str, str, str | None]]
    ) -> ExtractionResult:
        """Re-run extraction on (source, text[, doc_type]) tuples, e.g. after a user corrected
        the OCR text or the detected document type."""
        results = []
        for source, text, *rest in documents:
            doc_type = rest[0] if rest and rest[0] else None
            document = SanitizedDocument(source=source, text=text)
            if doc_type is None:
                doc_type = self.classifier.predict(text).doc_type
            results.append(self.extractor.extract(document, doc_type))
        return merge_extractions(results)

    # ------------------------------------------------------------------ filling

    def collect_values(
        self,
        extraction: ExtractionResult | None,
        overrides: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Values usable for filling and where each came from."""
        values: dict[str, str] = {"today": date.today().strftime("%d.%m.%Y")}
        sources: dict[str, str] = {"today": "system"}
        if extraction:
            for name, extracted in extraction.fields.items():
                if extracted.confidence >= self.settings.min_confidence:
                    values[name] = extracted.value
                    sources[name] = f"{extracted.source} ({extracted.confidence:.2f})"
        for name, value in (overrides or {}).items():
            values[name] = value
            sources[name] = "manual"
        cleaned = {
            name: clean_list_value(value)
            if name in FIELDS and FIELDS[name].kind == "list"
            else clean_fill_value(value)
            for name, value in values.items()
        }
        cleaned = {name: value for name, value in cleaned.items() if value}
        return cleaned, {name: sources[name] for name in cleaned}

    def fill(
        self,
        template: StandardDocument,
        extraction: ExtractionResult | None = None,
        overrides: Mapping[str, str] | None = None,
        allow_missing: bool = False,
    ) -> FillResult:
        template.verify_integrity()
        values, sources = self.collect_values(extraction, overrides)
        remembered = self.memory()
        for name in template.remember:  # the filer's own details, from the last document
            if name not in values and remembered.get(name):
                values[name], sources[name] = remembered[name], "memory"
        for name, value in template.defaults.items():
            if name not in values:
                values[name], sources[name] = value, "default"
        for name, (value, reason) in complete_values(values).items():
            values[name], sources[name] = value, reason
        missing = [name for name in template.required_fields() if name not in values]
        if missing and not allow_missing:
            raise MissingFieldsError(missing)

        metadata = {
            "title": template.title,
            "subject": f"Standard document '{template.name}' v{template.version}",
            "keywords": f"docfill; template-checksum={template.checksum}",
        }
        if template.kind == "text":
            body = render_body(template.body or "", values)
            pdf = render_text_pdf(body, metadata, self.settings)
        else:
            pdf = fill_pdf_form(
                template.pdf_data or b"",
                form_values(template, values),
                {f"/{key.capitalize()}": value for key, value in metadata.items()},
                self.settings.resolve_font_path(),
            )

        used = {name: values[name] for name in template.field_names() if name in values}
        return FillResult(
            pdf=pdf,
            template=template.name,
            template_version=template.version,
            values=used,
            sources={name: sources[name] for name in used},
            missing=missing,
            warnings=[f"No value for '{name}', left blank" for name in missing],
        )

    def process(
        self,
        files: Iterable[tuple[str, bytes]],
        template: StandardDocument,
        overrides: Mapping[str, str] | None = None,
        allow_missing: bool = False,
    ) -> tuple[FillResult, list[DocumentAnalysis]]:
        analyses = [self.analyze_bytes(data, name) for name, data in files]
        result = self.fill(template, self.combine(analyses), overrides, allow_missing)
        read_warnings = [warning for analysis in analyses for warning in analysis.raw.warnings]
        result.warnings = read_warnings + result.warnings
        return result, analyses
