"""Standard documents: the validated input spec and the database table."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import JSON, DateTime, Integer, LargeBinary, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from docfill.errors import TemplateError, TemplateIntegrityError
from docfill.templates.placeholders import parse_expression, validate_body

TemplateKind = Literal["text", "pdf_form"]
# The file a filled document is written as: a PDF, or a Word document (text documents only).
OutputFormat = Literal["pdf", "docx"]


def compute_checksum(
    kind: str,
    title: str,
    body: str | None,
    pdf_data: bytes | None,
    field_map: dict[str, str],
    optional_fields: list[str],
    options: dict[str, Any] | None = None,
) -> str:
    payload = {
        "kind": kind,
        "title": title,
        "body": body,
        "pdf_sha256": hashlib.sha256(pdf_data).hexdigest() if pdf_data else None,
        "field_map": field_map,
        "optional_fields": sorted(optional_fields),
    }
    if options:  # only when set, so checksums of documents saved before options existed hold
        payload["options"] = options
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ListSpec(BaseModel):
    """Repeated rows: ``columns`` patterns with ``{i}`` and a number of ``rows``, or explicit
    ``cells`` (one list of PDF fields per row) when the field names follow no pattern."""

    rows: int | None = Field(default=None, ge=1, le=200)
    columns: list[str] | None = None
    cells: list[list[str]] | None = None

    @model_validator(mode="after")
    def _check(self) -> ListSpec:
        if self.cells is None and not (self.rows and self.columns):
            raise ValueError("a list needs 'rows' and 'columns', or 'cells'")
        return self


class CombSpec(BaseModel):
    """A value written one character per box, e.g. a date in 8 boxes (``dd/mm/yyyy``) or an
    amount in digit boxes aligned to the right."""

    boxes: list[str] = Field(min_length=1, max_length=60)
    # text: as typed; digits: the digits only; amount: a whole number (``90.000 lei`` -> 90000);
    # date: ddmmyyyy.
    format: Literal["text", "digits", "amount", "date"] = "text"
    align: Literal["left", "right"] = "left"


def list_cells(spec: dict[str, Any] | ListSpec) -> list[list[str]]:
    """The PDF fields of each row of a list, whichever way the list was declared."""
    data = spec.model_dump() if isinstance(spec, ListSpec) else spec
    if data.get("cells"):
        return [list(row) for row in data["cells"]]
    return [[column.format(i=i) for column in data["columns"]] for i in range(data["rows"])]


def _expressions_in(value: str) -> list[str]:
    """``last_name | upper`` or a small template ``{{ last_name }} {{ first_name }}``."""
    from docfill.templates.placeholders import PLACEHOLDER_RE

    if "{{" in value:
        return [match.group()[2:-2] for match in PLACEHOLDER_RE.finditer(value)]
    return [value]


class StandardDocumentSpec(BaseModel):
    """What a user provides to register a standard document (from YAML, JSON or the API).

    * ``kind: text`` - ``body`` holds the document text with ``{{ placeholders }}``.
      Lines starting with ``#`` / ``##`` are headings, ``---`` is a horizontal rule.
    * ``kind: pdf_form`` - ``pdf_data`` is an existing PDF with fillable (AcroForm) text
      fields; ``field_map`` maps PDF field names to docfill fields, e.g.
      ``{"txtSurname": "last_name | upper"}``. Without a map, PDF field names are used as-is.

    A text document is written as a PDF, or as a Word document with ``output: docx`` (an act
    the filer edits before signing).
    """

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,99}$")
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    kind: TemplateKind = "text"
    body: str | None = None
    pdf_data: bytes | None = Field(default=None, repr=False)
    field_map: dict[str, str] = Field(default_factory=dict)
    optional_fields: list[str] = Field(default_factory=list)
    # The document type this standard document is (a filled copy uploaded as a source is
    # recognised and read through the field map).
    doc_type: str | None = None
    # Values proposed when nothing was found, e.g. {"request_object": "Act constitutiv"}.
    defaults: dict[str, str] = Field(default_factory=dict)
    # Fields whose last used value is proposed next time (the filer's own details).
    remember: list[str] = Field(default_factory=list)
    # Repeated rows: {"caen_activities": {"rows": 18, "columns": ["clasa_caen.0.{i}", ...]}};
    # the field value holds one row per line, columns split at the first space.
    lists: dict[str, ListSpec] = Field(default_factory=dict)
    # Option buttons: {"PdfRadio": {"posta": "/v1", "electronic": "/v3"}} (value -> state).
    choices: dict[str, dict[str, str]] = Field(default_factory=dict)
    # Section boxes ticked when any listed field has a value, e.g. the "4.1 Acte și fapte"
    # box of Anexa 2a when any of its items is ticked: {"CheckBox15": ["change_name", ...]}.
    ticks: dict[str, list[str]] = Field(default_factory=dict)
    # One character per box: {"profit_tax_start": {"boxes": ["13", ...], "format": "date"}}.
    combs: dict[str, CombSpec] = Field(default_factory=dict)
    # A choice ticking one of several check boxes: {"vat_period": {"lunară": "BBox23", ...}}.
    box_choices: dict[str, dict[str, str]] = Field(default_factory=dict)
    # PDF fields left empty unless a field has a value, e.g. the block of a beneficial owner:
    # {"p2_beneficial_owner": ["33", "31", ...]}.
    filled_when: dict[str, list[str]] = Field(default_factory=dict)
    # One copy per person having any of these roles (e.g. ["board_role"]: a sworn statement for
    # each administrator), written with the plain person fields ({{ last_name }}...).
    per_person: list[str] = Field(default_factory=list, max_length=10)
    # Fields asked in the review though no box prints them: they decide values that are printed
    # (e.g. ["representative_type"]: a lawyer or a proxy writes "prin ... conform ...").
    asks: list[str] = Field(default_factory=list, max_length=20)
    output: OutputFormat = "pdf"
    # Fields whose values a Word document writes in bold (``output: docx``), e.g. the firm.
    bold: list[str] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _check(self) -> StandardDocumentSpec:
        from docfill.extraction.fields import PERSON_FIELDS

        unknown_roles = [name for name in self.per_person if name not in PERSON_FIELDS]
        if unknown_roles:
            raise ValueError(f"per_person takes fields of a person: {unknown_roles}")
        if self.kind == "text":
            if not self.body or not self.body.strip():
                raise ValueError("a text standard document needs a non-empty 'body'")
            if self.pdf_data or self.field_map:
                raise ValueError("'pdf_data' and 'field_map' only apply to pdf_form documents")
            if self.combs or self.box_choices or self.filled_when:
                raise ValueError(
                    "'combs', 'box_choices' and 'filled_when' only apply to pdf_form documents"
                )
            try:
                validate_body(self.body)
            except TemplateError as exc:
                raise ValueError(str(exc)) from exc
            unused = [name for name in self.bold if not re.search(rf"\{{\{{\s*{name}\b", self.body)]
            if unused:
                raise ValueError(f"bold names fields the body does not write: {unused}")
            if self.bold and self.output != "docx":
                raise ValueError("'bold' only applies to documents written as Word (output: docx)")
        else:
            if self.output != "pdf" or self.bold:
                raise ValueError("a pdf_form standard document is filled as a PDF")
            if not self.pdf_data:
                raise ValueError("a pdf_form standard document needs 'pdf_data' (or 'pdf_file')")
            if self.body:
                raise ValueError("'body' only applies to text documents")
            from docfill.export.pdf_form import list_fields

            pdf_fields = list_fields(self.pdf_data)
            if not any(kind == "text" for kind in pdf_fields.values()):
                raise ValueError("the PDF has no fillable text fields")
            if not self.field_map and not self.lists:
                self.field_map = {
                    n: n for n, kind in pdf_fields.items() if kind == "text" and "#" not in n
                }
            listed = {
                cell for spec in self.lists.values() for row in list_cells(spec) for cell in row
            }
            listed |= {box for spec in self.combs.values() for box in spec.boxes}
            listed |= {box for options in self.box_choices.values() for box in options.values()}
            listed |= {name for names in self.filled_when.values() for name in names}
            unknown = (set(self.field_map) | listed | set(self.choices) | set(self.ticks)) - set(
                pdf_fields
            )
            if unknown:
                raise ValueError(f"unknown PDF fields: {sorted(unknown)[:10]}")
            not_boxes = {
                box
                for options in self.box_choices.values()
                for box in options.values()
                if pdf_fields.get(box) != "checkbox"
            }
            if not_boxes:
                raise ValueError(f"box_choices must name check boxes: {sorted(not_boxes)[:10]}")
            for expression in self.field_map.values():
                try:
                    if "{{" in expression:
                        validate_body(expression)
                    else:
                        parse_expression(expression)
                except TemplateError as exc:
                    raise ValueError(str(exc)) from exc
        return self

    def options(self) -> dict[str, Any]:
        options: dict[str, Any] = {}
        if self.doc_type:
            options["doc_type"] = self.doc_type
        if self.defaults:
            options["defaults"] = dict(self.defaults)
        if self.remember:
            options["remember"] = list(self.remember)
        if self.lists:
            options["lists"] = {k: v.model_dump(exclude_none=True) for k, v in self.lists.items()}
        if self.choices:
            options["choices"] = {k: dict(v) for k, v in self.choices.items()}
        if self.ticks:
            options["ticks"] = {k: list(v) for k, v in self.ticks.items()}
        if self.combs:
            options["combs"] = {k: v.model_dump() for k, v in self.combs.items()}
        if self.box_choices:
            options["box_choices"] = {k: dict(v) for k, v in self.box_choices.items()}
        if self.filled_when:
            options["filled_when"] = {k: list(v) for k, v in self.filled_when.items()}
        if self.per_person:
            options["per_person"] = list(self.per_person)
        if self.asks:
            options["asks"] = list(self.asks)
        if self.output != "pdf":
            options["output"] = self.output
        if self.bold:
            options["bold"] = list(self.bold)
        return options

    def checksum(self) -> str:
        return compute_checksum(
            self.kind,
            self.title,
            self.body,
            self.pdf_data,
            self.field_map,
            self.optional_fields,
            self.options(),
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
    options: Mapped[dict[str, Any] | None] = mapped_column(JSON, default=dict, nullable=True)
    checksum: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    def expressions(self) -> list[str]:
        """Field expressions in document order (placeholders or PDF field mappings); the fields
        of line conditions of text documents are included."""
        if self.kind == "text":
            from docfill.templates.placeholders import CONDITION_RE, PLACEHOLDER_RE

            expressions = []
            for line in (self.body or "").splitlines():
                if match := CONDITION_RE.match(line):
                    expressions += match["names"].split("|")
                expressions += [m.group()[2:-2] for m in PLACEHOLDER_RE.finditer(line)]
            return expressions
        return [expr for value in self.field_map.values() for expr in _expressions_in(value)]

    def option(self, key: str, default: Any = None) -> Any:
        return (self.options or {}).get(key, default)

    @property
    def doc_type(self) -> str | None:
        return self.option("doc_type")

    @property
    def defaults(self) -> dict[str, str]:
        return self.option("defaults", {})

    @property
    def remember(self) -> list[str]:
        return self.option("remember", [])

    @property
    def lists(self) -> dict[str, dict[str, Any]]:
        return self.option("lists", {})

    @property
    def choices(self) -> dict[str, dict[str, str]]:
        return self.option("choices", {})

    @property
    def ticks(self) -> dict[str, list[str]]:
        return self.option("ticks", {})

    @property
    def combs(self) -> dict[str, dict[str, Any]]:
        return self.option("combs", {})

    @property
    def box_choices(self) -> dict[str, dict[str, str]]:
        return self.option("box_choices", {})

    @property
    def filled_when(self) -> dict[str, list[str]]:
        return self.option("filled_when", {})

    @property
    def per_person(self) -> list[str]:
        return self.option("per_person", [])

    @property
    def asks(self) -> list[str]:
        return self.option("asks", [])

    @property
    def output(self) -> str:
        """``pdf`` or ``docx``: the file the filled document is written as."""
        return self.option("output", "pdf")

    @property
    def bold(self) -> list[str]:
        return self.option("bold", [])

    def pdf_field_names(self) -> list[str]:
        """Every PDF field this document fills (mapped fields, list cells, boxes)."""
        names = list(self.field_map) + [name for name in self.ticks if name not in self.field_map]
        for spec in self.lists.values():
            names += [cell for row in list_cells(spec) for cell in row]
        for spec in self.combs.values():
            names += list(spec["boxes"])
        for options in self.box_choices.values():
            names += list(options.values())
        return list(dict.fromkeys(name.split("#")[0] for name in names))

    def field_names(self) -> list[str]:
        """The docfill fields this document uses, in document order (computed values such as
        ``domicile_line`` included; see :mod:`docfill.computed`)."""
        names: list[str] = []
        for expression in self.expressions():
            name, _ = parse_expression(expression)
            if name not in names:
                names.append(name)
        for extra in (self.lists, self.combs, self.box_choices, self.filled_when):
            names += [name for name in extra if name not in names]
        return names

    def required_fields(self) -> list[str]:
        """Fields that must have a value (the inputs of required computed values)."""
        from docfill.computed import expand_required

        optional = set(self.optional_fields or [])
        required = [name for name in self.field_names() if name not in optional]
        return [name for name in expand_required(required) if name not in optional]

    def input_fields(self) -> list[str]:
        """The fields a person provides: computed values replaced by their inputs. A document
        made for each person also asks for the fields (and roles) of the other persons."""
        from docfill.computed import expand
        from docfill.extraction.fields import PERSON_FIELDS, PERSON_PREFIXES

        names = expand(self.field_names())
        names += [name for name in self.asks if name not in names]
        if not self.per_person:
            return names
        names += [role for role in self.per_person if role not in names]
        for prefix in PERSON_PREFIXES[1:]:
            names += [prefix + name for name in names if name in PERSON_FIELDS]
        return list(dict.fromkeys(names))

    def current_checksum(self) -> str:
        return compute_checksum(
            self.kind,
            self.title,
            self.body,
            self.pdf_data,
            dict(self.field_map or {}),
            list(self.optional_fields or []),
            dict(self.options or {}),
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
            "doc_type": self.doc_type,
            "fields": self.input_fields(),
            "required_fields": self.required_fields(),
            "per_person": list(self.per_person),
            "output": self.output,
            "checksum": self.checksum,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
