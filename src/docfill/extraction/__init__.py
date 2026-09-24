"""Field extraction: labelled values, address patterns and ML named-entity recognition.

Every candidate carries a confidence; for each field the most confident one wins
(labelled > derived > pattern / NER). Values are only ever *copied* from the source
documents, never generated.
"""

from __future__ import annotations

from collections.abc import Iterable

from docfill.config import Settings, get_settings
from docfill.extraction.fields import FIELDS, FieldSpec, field_labels
from docfill.extraction.ner import NERExtractor
from docfill.extraction.patterns import extract_patterns
from docfill.extraction.rules import extract_labeled
from docfill.extraction.text_utils import compose_address, parse_address, split_full_name
from docfill.models import ExtractedField, ExtractionResult, SanitizedDocument

DERIVED_FACTOR = 0.95
_ADDRESS_PARTS = ("street_address", "postal_code", "city", "region", "country")


def _derived(
    name: str, value: str, confidence: float, evidence: str, document: str | None
) -> ExtractedField:
    return ExtractedField(
        name=name,
        value=value,
        confidence=round(confidence, 4),
        source="derived",
        evidence=evidence,
        document=document,
    )


def derive_fields(result: ExtractionResult) -> ExtractionResult:
    """Fill gaps from related fields: full name <-> first/last, address <-> its components."""
    fields = result.fields

    if (full := fields.get("full_name")) and (split := split_full_name(full.value)):
        first, last = split
        conf = full.confidence * DERIVED_FACTOR
        result.offer(_derived("first_name", first, conf, full.value, full.document))
        result.offer(_derived("last_name", last, conf, full.value, full.document))

    if (first := fields.get("first_name")) and (last := fields.get("last_name")):
        conf = min(first.confidence, last.confidence) * DERIVED_FACTOR
        full_value = f"{first.value} {last.value}"
        result.offer(_derived("full_name", full_value, conf, full_value, first.document))

    if address := fields.get("full_address"):
        conf = address.confidence * DERIVED_FACTOR
        for name, value in parse_address(address.value).items():
            result.offer(_derived(name, value, conf, address.value, address.document))

    parts = {name: fields[name] for name in _ADDRESS_PARTS if name in fields}
    if composed := compose_address({name: field.value for name, field in parts.items()}):
        conf = min(field.confidence for field in parts.values()) * DERIVED_FACTOR
        current = fields.get("full_address")
        if current and composed != current.value and composed.startswith(current.value):
            # "Hauptstraße 5" + city/country found elsewhere -> "Hauptstraße 5, Berlin, Germany"
            conf = max(conf, current.confidence)
            result.replace(_derived("full_address", composed, conf, composed, current.document))
        else:
            result.offer(_derived("full_address", composed, conf, composed, None))
    return result


class FieldExtractor:
    def __init__(self, settings: Settings | None = None, use_ner: bool = True):
        settings = settings or get_settings()
        self.ner = NERExtractor(settings.spacy_model) if use_ner and settings.spacy_model else None

    @property
    def ner_available(self) -> bool:
        return bool(self.ner and self.ner.available)

    def extract(self, document: SanitizedDocument) -> ExtractionResult:
        result = ExtractionResult()
        candidates = extract_labeled(document.text, document.source)
        candidates += extract_patterns(document.text, document.source)
        if self.ner:
            candidates += self.ner.extract(document.text, document.source)
        for candidate in candidates:
            result.offer(candidate)
        return derive_fields(result)


def merge_extractions(results: Iterable[ExtractionResult]) -> ExtractionResult:
    """Merge the extractions of several documents; the most confident value per field wins."""
    merged = ExtractionResult()
    for result in results:
        merged = merged.merge(result)
    return derive_fields(merged)


__all__ = [
    "FIELDS",
    "FieldExtractor",
    "FieldSpec",
    "NERExtractor",
    "derive_fields",
    "extract_labeled",
    "extract_patterns",
    "field_labels",
    "merge_extractions",
]
