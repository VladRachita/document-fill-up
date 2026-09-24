"""Domain models shared by the pipeline stages."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class DocumentType(str, Enum):
    PDF = "pdf"
    DOCX = "docx"
    IMAGE = "image"


class Page(BaseModel):
    number: int
    text: str
    ocr: bool = False


class RawDocument(BaseModel):
    """Text as read from the source file, before any cleaning."""

    source: str
    doc_type: DocumentType
    pages: list[Page]
    warnings: list[str] = Field(default_factory=list)
    # Values stored in the fields of a filled PDF form (field name -> value).
    form_values: dict[str, str] = Field(default_factory=dict)

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)

    @property
    def used_ocr(self) -> bool:
        return any(page.ocr for page in self.pages)


class SanitizedDocument(BaseModel):
    """Cleaned text: noise, page artifacts and sensitive data removed."""

    source: str
    text: str
    removed_lines: int = 0
    redactions: dict[str, int] = Field(default_factory=dict)

    @property
    def lines(self) -> list[str]:
        return [line for line in self.text.splitlines() if line.strip()]


FieldSource = Literal[
    "label", "learned", "pattern", "mrz", "form", "ner", "derived", "memory", "default", "manual"
]


class ExtractedField(BaseModel):
    name: str
    value: str
    confidence: float = Field(ge=0.0, le=1.0)
    source: FieldSource
    evidence: str | None = None
    document: str | None = None
    # Problems found by the validators (bad CNP checksum, mismatch with the MRZ, ...).
    issues: list[str] = Field(default_factory=list)


class ExtractionResult(BaseModel):
    """Best value per field, plus every distinct candidate seen (for review/correction)."""

    fields: dict[str, ExtractedField] = Field(default_factory=dict)
    candidates: dict[str, list[ExtractedField]] = Field(default_factory=dict)

    def values(self, min_confidence: float = 0.0) -> dict[str, str]:
        return {
            name: field.value
            for name, field in self.fields.items()
            if field.confidence >= min_confidence
        }

    def _record(self, candidate: ExtractedField) -> None:
        pool = self.candidates.setdefault(candidate.name, [])
        for index, existing in enumerate(pool):
            if (existing.value.casefold(), existing.source) == (
                candidate.value.casefold(),
                candidate.source,
            ):
                if candidate.confidence > existing.confidence:
                    pool[index] = candidate
                break
        else:
            pool.append(candidate)
        pool.sort(key=lambda field: field.confidence, reverse=True)

    def offer(self, candidate: ExtractedField) -> bool:
        """Record ``candidate`` and keep it if it beats the current value for its field."""
        self._record(candidate)
        current = self.fields.get(candidate.name)
        if current is None or candidate.confidence > current.confidence:
            self.fields[candidate.name] = candidate
            return True
        return False

    def replace(self, candidate: ExtractedField) -> None:
        """Record ``candidate`` and make it the value for its field unconditionally."""
        self._record(candidate)
        self.fields[candidate.name] = candidate

    def merge(self, other: ExtractionResult) -> ExtractionResult:
        merged = ExtractionResult()
        for result in (self, other):
            for pool in result.candidates.values():
                for candidate in pool:
                    merged._record(candidate)
        merged.fields = dict(self.fields)
        for field in other.fields.values():
            merged.offer(field)
        return merged
