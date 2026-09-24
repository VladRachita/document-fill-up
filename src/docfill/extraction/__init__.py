"""Field extraction: labelled values, identity-document patterns (CNP, series/number, MRZ),
address patterns and ML named-entity recognition, then derivations and cross-checks.

Every candidate carries a confidence; for each field the most confident one wins
(labelled > derived > pattern / NER). Values are only ever *copied* from the source
documents or derived from copied values, never generated.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping

from docfill.config import Settings, get_settings
from docfill.extraction.derive import complete_values, derive_fields
from docfill.extraction.fields import FIELDS, GROUPS, FieldSpec, field_labels
from docfill.extraction.ner import NERExtractor
from docfill.extraction.patterns import extract_identity, extract_patterns
from docfill.extraction.rules import extract_labeled
from docfill.models import ExtractedField, ExtractionResult, SanitizedDocument

# (candidate, document type) -> calibrated confidence; provided by the learning store.
Calibrator = Callable[[ExtractedField, str | None], float]


class FieldExtractor:
    def __init__(
        self,
        settings: Settings | None = None,
        use_ner: bool = True,
        calibrator: Calibrator | None = None,
        learned_labels: Callable[[str | None], Mapping[str, str]] | None = None,
        fix_spelling: Callable[[ExtractedField], ExtractedField] | None = None,
    ):
        settings = settings or get_settings()
        self.ner = NERExtractor(settings.spacy_model) if use_ner and settings.spacy_model else None
        self.calibrator = calibrator
        self.learned_labels = learned_labels
        self.fix_spelling = fix_spelling

    @property
    def ner_available(self) -> bool:
        return bool(self.ner and self.ner.available)

    def candidates(
        self, document: SanitizedDocument, doc_type: str | None = None
    ) -> list[ExtractedField]:
        learned = self.learned_labels(doc_type) if self.learned_labels else None
        found = extract_labeled(document.text, document.source, doc_type, learned)
        found += extract_identity(document.text, document.source, doc_type)
        found += extract_patterns(document.text, document.source)
        if self.ner:
            found += self.ner.extract(document.text, document.source)
        if self.fix_spelling:
            found = [self.fix_spelling(candidate) for candidate in found]
        if self.calibrator:
            for candidate in found:
                candidate.confidence = round(self.calibrator(candidate, doc_type), 4)
        return found

    def extract(
        self,
        document: SanitizedDocument,
        doc_type: str | None = None,
        extra: Iterable[ExtractedField] = (),
        use_text: bool = True,
    ) -> ExtractionResult:
        """``extra``: already structured candidates (e.g. a recognised filled form). For such
        documents ``use_text=False`` skips reading the text, which would only add noise."""
        result = ExtractionResult()
        found = self.candidates(document, doc_type) if use_text else []
        for candidate in [*extra, *found]:
            result.offer(candidate)
        return _check_and_derive(result)


def merge_extractions(results: Iterable[ExtractionResult]) -> ExtractionResult:
    """Merge the extractions of several documents; the most confident value per field wins."""
    merged = ExtractionResult()
    for result in results:
        merged = merged.merge(result)
    return _check_and_derive(merged)


def _check_and_derive(result: ExtractionResult) -> ExtractionResult:
    """Validate, derive missing fields from validated ones, validate the derived ones."""
    from docfill.validation import cross_check  # imports the field catalog from this package

    return cross_check(derive_fields(cross_check(result)))


__all__ = [
    "FIELDS",
    "GROUPS",
    "Calibrator",
    "FieldExtractor",
    "FieldSpec",
    "NERExtractor",
    "complete_values",
    "derive_fields",
    "extract_identity",
    "extract_labeled",
    "extract_patterns",
    "field_labels",
    "merge_extractions",
]
