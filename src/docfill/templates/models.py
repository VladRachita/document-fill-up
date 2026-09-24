"""Standard documents: the validated input spec and the database table."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import JSON, DateTime, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from docfill.errors import TemplateError, TemplateIntegrityError
from docfill.templates.placeholders import parse_expression, validate_body

TemplateKind = Literal["text", "pdf_form"]


def compute_checksum(
    kind: str,
    title: str,
    body: str | None,
    pdf_data: bytes | None,
    field_map: dict[str, str],
    optional_fields: list[str],
) -> str:
    payload = {
        "kind": kind,
        "title": title,
        "body": body,
        "pdf_sha256": hashlib.sha256(pdf_data).hexdigest() if pdf_data else None,
        "field_map": field_map,
        "optional_fields": sorted(optional_fields),
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class StandardDocumentSpec(BaseModel):
    """What a user provides to register a standard document (from YAML, JSON or the API).

    * ``kind: text`` - ``body`` holds the document text with ``{{ placeholders }}``.
      Lines starting with ``#`` / ``##`` are headings, ``---`` is a horizontal rule.
    * ``kind: pdf_form`` - ``pdf_data`` is an existing PDF with fillable (AcroForm) text
      fields; ``field_map`` maps PDF field names to docfill fields, e.g.
      ``{"txtSurname": "last_name | upper"}``. Without a map, PDF field names are used as-is.
    """

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,99}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    kind: TemplateKind = "text"
    body: str | None = None
    pdf_data: bytes | None = Field(default=None, repr=False)
    field_map: dict[str, str] = Field(default_factory=dict)
    optional_fields: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> StandardDocumentSpec:
        if self.kind == "text":
            if not self.body or not self.body.strip():
                raise ValueError("a text standard document needs a non-empty 'body'")
            if self.pdf_data or self.field_map:
                raise ValueError("'pdf_data' and 'field_map' only apply to pdf_form documents")
            try:
                validate_body(self.body)
            except TemplateError as exc:
                raise ValueError(str(exc)) from exc
        else:
            if not self.pdf_data:
                raise ValueError("a pdf_form standard document needs 'pdf_data' (or 'pdf_file')")
            if self.body:
                raise ValueError("'body' only applies to text documents")
            from docfill.export.pdf_form import list_text_fields

            pdf_fields = list_text_fields(self.pdf_data)
            if not pdf_fields:
                raise ValueError("the PDF has no fillable text fields")
            if not self.field_map:
                self.field_map = {name: name for name in pdf_fields}
            unknown = set(self.field_map) - set(pdf_fields)
            if unknown:
                raise ValueError(f"field_map references unknown PDF fields: {sorted(unknown)}")
            for expression in self.field_map.values():
                try:
                    parse_expression(expression)
                except TemplateError as exc:
                    raise ValueError(str(exc)) from exc
        return self

    def checksum(self) -> str:
        return compute_checksum(
            self.kind, self.title, self.body, self.pdf_data, self.field_map, self.optional_fields
        )


class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StandardDocument(Base):
    __tablename__ = "standard_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(20), default="text")
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    pdf_data: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    field_map: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    optional_fields: Mapped[list[str]] = mapped_column(JSON, default=list)
    checksum: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    def expressions(self) -> list[str]:
        """Field expressions in document order (placeholders or PDF field mappings)."""
        if self.kind == "text":
            from docfill.templates.placeholders import PLACEHOLDER_RE

            return [match.group()[2:-2] for match in PLACEHOLDER_RE.finditer(self.body or "")]
        return list(self.field_map.values())

    def field_names(self) -> list[str]:
        names: list[str] = []
        for expression in self.expressions():
            name, _ = parse_expression(expression)
            if name not in names:
                names.append(name)
        return names

    def required_fields(self) -> list[str]:
        optional = set(self.optional_fields or [])
        return [name for name in self.field_names() if name not in optional]

    def current_checksum(self) -> str:
        return compute_checksum(
            self.kind,
            self.title,
            self.body,
            self.pdf_data,
            dict(self.field_map or {}),
            list(self.optional_fields or []),
        )

    def verify_integrity(self) -> None:
        """Refuse to use a standard document modified outside docfill (e.g. directly in SQL)."""
        if self.current_checksum() != self.checksum:
            raise TemplateIntegrityError(
                f"Standard document '{self.name}' v{self.version} failed its integrity check; "
                "it was modified outside docfill. Re-register it to accept the change."
            )

    def summary(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "kind": self.kind,
            "version": self.version,
            "fields": self.field_names(),
            "required_fields": self.required_fields(),
            "checksum": self.checksum,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
