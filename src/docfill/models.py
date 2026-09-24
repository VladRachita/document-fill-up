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


FieldSource = Literal["label", "pattern", "ner", "derived", "manual"]


class ExtractedField(BaseModel):
    name: str
    value: str
    confidence: float = Field(ge=0.0, le=1.0)
    source: FieldSource
    evidence: str | None = None
    document: str | None = None


class ExtractionResult(BaseModel):
    fields: dict[str, ExtractedField] = Field(default_factory=dict)

    def values(self, min_confidence: float = 0.0) -> dict[str, str]:
        return {
            name: field.value
            for name, field in self.fields.items()
            if field.confidence >= min_confidence
        }

    def offer(self, candidate: ExtractedField) -> bool:
        """Keep ``candidate`` if it beats the current value for its field."""
        current = self.fields.get(candidate.name)
        if current is None or candidate.confidence > current.confidence:
            self.fields[candidate.name] = candidate
            return True
        return False

    def merge(self, other: ExtractionResult) -> ExtractionResult:
        merged = ExtractionResult(fields=dict(self.fields))
        for field in other.fields.values():
            merged.offer(field)
        return merged
