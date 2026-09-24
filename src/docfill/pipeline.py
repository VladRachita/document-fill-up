"""End-to-end pipeline: read -> sanitize -> extract -> fill a standard document -> PDF."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from docfill.config import Settings, get_settings
from docfill.errors import MissingFieldsError
from docfill.export import fill_pdf_form, render_text_pdf
from docfill.extraction import FieldExtractor, derive_fields
from docfill.models import ExtractionResult, RawDocument, SanitizedDocument
from docfill.readers import read_bytes
from docfill.sanitize import Sanitizer
from docfill.templates.models import StandardDocument
from docfill.templates.placeholders import apply, clean_fill_value, render_body


@dataclass
class DocumentAnalysis:
    raw: RawDocument
    sanitized: SanitizedDocument
    extraction: ExtractionResult


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
    ):
        self.settings = settings or get_settings()
        self.extractor = extractor or FieldExtractor(self.settings)
        self.sanitizer = sanitizer or Sanitizer()

    # ------------------------------------------------------------------ scanning

    def analyze_bytes(self, data: bytes, filename: str) -> DocumentAnalysis:
        raw = read_bytes(data, filename, self.settings)
        sanitized = self.sanitizer.sanitize(raw)
        return DocumentAnalysis(raw, sanitized, self.extractor.extract(sanitized))

    def analyze_file(self, path: str | Path) -> DocumentAnalysis:
        path = Path(path)
        return self.analyze_bytes(path.read_bytes(), path.name)

    @staticmethod
    def combine(analyses: Iterable[DocumentAnalysis]) -> ExtractionResult:
        """Merge several source documents; the most confident value per field wins."""
        result = ExtractionResult()
        for analysis in analyses:
            result = result.merge(analysis.extraction)
        return derive_fields(result)

    # ------------------------------------------------------------------ filling

    def collect_values(
        self,
        extraction: ExtractionResult | None,
        overrides: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Values usable for filling and where each came from."""
        values: dict[str, str] = {"today": date.today().isoformat()}
        sources: dict[str, str] = {"today": "system"}
        if extraction:
            for name, extracted in extraction.fields.items():
                if extracted.confidence >= self.settings.min_confidence:
                    values[name] = extracted.value
                    sources[name] = f"{extracted.source} ({extracted.confidence:.2f})"
        for name, value in (overrides or {}).items():
            values[name] = value
            sources[name] = "manual"
        cleaned = {name: clean_fill_value(value) for name, value in values.items()}
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
            form_values = {
                pdf_field: value
                for pdf_field, expression in template.field_map.items()
                if (value := apply(expression, values))
            }
            pdf = fill_pdf_form(
                template.pdf_data or b"",
                form_values,
                {f"/{key.capitalize()}": value for key, value in metadata.items()},
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
