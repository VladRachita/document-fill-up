"""What the knowledge base is made of: validated specs, as written in YAML or sent to the API.

Four kinds of entries, each identified by a ``key``:

* ``entity`` - a legal form: SRL, SRL-D, SA (companies), PFA, II, IF (natural persons).
* ``procedure`` - what is done for an entity at the trade register: ``infiintare`` (open),
  ``modificare`` (change) or ``radiere`` (close). It lists the forms docfill fills, the supporting
  documents of the file (dosar), extra fields to collect and the legal basis.
* ``rule`` - a legal check on the values (e.g. the minimum share capital of an SA), with the
  article it comes from. Checks are declarative (see :class:`Check`), never code, so rules can
  be added and corrected without a new release.
* ``doc_type`` - a kind of document the classifier learns to recognise (act constitutiv, proof
  of the registered office...), from seed texts and examples.

Every entry carries its legal basis. Entries start as ``draft`` and are ``verified`` by a
named person; the wizard shows which knowledge is not verified yet.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

KEY_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,99}$"
DOC_TYPE_PATTERN = r"^[a-z][a-z0-9_]{1,49}$"
FIELD_PATTERN = r"^[a-z][a-z0-9_]{0,59}$"

Kind = Literal["entity", "procedure", "rule", "doc_type"]
KINDS: tuple[str, ...] = ("entity", "procedure", "rule", "doc_type")
Status = Literal["draft", "verified", "retired"]
Operation = Literal["infiintare", "modificare", "radiere"]
Category = Literal["legal_person", "natural_person"]
Severity = Literal["error", "warning", "info"]

OPERATIONS: dict[str, dict[str, str]] = {
    "infiintare": {
        "title": "Înființare",
        "english": "Open",
        "description": "Înmatriculare / înregistrare în registrul comerțului",
    },
    "modificare": {
        "title": "Modificare",
        "english": "Change",
        "description": "Înscriere de mențiuni (sediu, asociați, administratori, obiect...)",
    },
    "radiere": {
        "title": "Radiere",
        "english": "Close",
        "description": "Radierea din registrul comerțului",
    },
}
CATEGORIES: dict[str, str] = {
    "legal_person": "Societăți (persoane juridice)",
    "natural_person": "Persoane fizice (PFA, PFI, II, IF)",
}


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _http(value: str | None) -> str | None:
    if value and not re.match(r"https?://", value):
        raise ValueError("url must start with http:// or https://")
    return value


class LegalRef(_Model):
    """Where a piece of knowledge comes from: ``Legea nr. 31/1990``, ``art. 10 alin. (1)``."""

    citation: str = Field(min_length=3, max_length=200)
    article: str | None = Field(default=None, max_length=80)
    note: str = Field(default="", max_length=500)
    url: str | None = Field(default=None, max_length=500)

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        return _http(value)


class _Entry(_Model):
    kind: ClassVar[str]

    key: str = Field(pattern=KEY_PATTERN)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=4000)
    legal_basis: list[LegalRef] = Field(default_factory=list, max_length=20)
    notes: list[str] = Field(default_factory=list, max_length=30)

    def references(self) -> dict[str, list[str]]:
        """Other entries this one depends on, by kind."""
        return {}

    def text(self) -> str:
        """Plain text used to search the knowledge."""
        parts = [self.title, self.description, *self.notes]
        parts += [f"{ref.citation} {ref.article or ''} {ref.note}" for ref in self.legal_basis]
        return "\n".join(part for part in parts if part)


class EntitySpec(_Entry):
    """A legal form: ``SRL`` (company) or ``PFA`` (natural person)..."""

    kind: ClassVar[str] = "entity"

    abbreviation: str = Field(min_length=1, max_length=20)
    category: Category
    order: int = Field(default=100, ge=0, le=10000)  # position in the wizard


class FormRef(_Model):
    """An official form of the procedure. ``template`` is the docfill standard document that
    fills it; ``None`` when docfill does not have it yet (shown as "to add")."""

    title: str = Field(min_length=1, max_length=200)
    template: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_-]{1,99}$")
    required: bool = True
    note: str = Field(default="", max_length=500)
    # Where the official form is published (to add it to docfill, or to fill it by hand).
    url: str | None = Field(default=None, max_length=500)

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        return _http(value)


class DocumentRef(_Model):
    """A supporting document of the file (act constitutiv, proof of the registered office...).
    ``doc_type`` lets docfill tick it off when an uploaded file is recognised as that type."""

    title: str = Field(min_length=1, max_length=200)
    doc_type: str | None = Field(default=None, pattern=DOC_TYPE_PATTERN)
    required: bool = True
    note: str = Field(default="", max_length=500)
    legal_basis: list[LegalRef] = Field(default_factory=list, max_length=10)


class ProcedureSpec(_Entry):
    kind: ClassVar[str] = "procedure"

    entity: str = Field(pattern=KEY_PATTERN)
    operation: Operation
    # Where the file is submitted: the trade register (ONRC) or, e.g., the tax office (ANAF).
    authority: str = Field(default="ONRC", min_length=2, max_length=100)
    forms: list[FormRef] = Field(default_factory=list, max_length=20)
    documents: list[DocumentRef] = Field(default_factory=list, max_length=40)
    # Fields to collect besides the forms' own fields (e.g. the share capital for the checks).
    fields: list[str] = Field(default_factory=list, max_length=50)
    required_fields: list[str] = Field(default_factory=list, max_length=50)

    @field_validator("fields", "required_fields")
    @classmethod
    def _field_names(cls, names: list[str]) -> list[str]:
        for name in names:
            if not re.fullmatch(FIELD_PATTERN, name):
                raise ValueError(f"invalid field name {name!r}")
        return list(dict.fromkeys(names))

    def references(self) -> dict[str, list[str]]:
        return {
            "entity": [self.entity],
            "doc_type": [d.doc_type for d in self.documents if d.doc_type],
            "template": [f.template for f in self.forms if f.template],
            "field": [*self.fields, *self.required_fields],
        }

    def text(self) -> str:
        parts = [super().text()]
        parts += [f"{form.title} {form.note}" for form in self.forms]
        parts += [f"{doc.title} {doc.note}" for doc in self.documents]
        return "\n".join(parts)


CheckType = Literal[
    "required",
    "required_any",
    "min_amount",
    "max_amount",
    "min_count",
    "max_count",
    "contains_any",
    "contains_field",
    "min_age",
    "pattern",
    "lines_pattern",
    "one_of",
]
_NEEDS_VALUE = {"min_amount", "max_amount", "min_count", "max_count", "min_age"}
_NEEDS_PHRASES = {"contains_any", "one_of"}
_NEEDS_PATTERN = {"pattern", "lines_pattern"}


class Check(_Model):
    """A declarative check on the values (no code runs from the knowledge base).

    ============== ===============================================================
    required       every field of ``fields`` (or ``field``) has a value
    required_any   at least one field of ``fields`` has a value (e.g. one change ticked)
    min_amount     ``field`` is an amount (``90.000 lei``) of at least ``value``
    max_amount     ... at most ``value``
    min_count      ``field`` (one item per line) has at least ``value`` lines
    max_count      ... at most ``value`` lines
    contains_any   ``field`` contains one of ``any_of`` (whole words, accents and
                   dots ignored: ``S.R.L.`` == ``SRL``)
    contains_field ``field`` contains the value of ``other`` (``company_name``
                   contains ``last_name``)
    min_age        the person (``field``: ``cnp`` or a date of birth) is at least
                   ``value`` years old
    pattern        ``field`` matches the regular expression ``pattern``
    lines_pattern  every line of ``field`` starts with ``pattern``
    one_of         ``field`` is one of ``any_of``
    ============== ===============================================================

    Except ``required`` / ``required_any``, a check whose field is empty is skipped (not
    failed): missing values are the job of those checks and of the forms' required fields.
    """

    type: CheckType
    field: str | None = Field(default=None, pattern=FIELD_PATTERN)
    fields: list[str] = Field(default_factory=list, max_length=30)
    value: float | None = None
    any_of: list[str] = Field(default_factory=list, max_length=50)
    other: str | None = Field(default=None, pattern=FIELD_PATTERN)
    pattern: str | None = Field(default=None, max_length=300)

    @model_validator(mode="after")
    def _complete(self) -> Check:
        if self.type in ("required", "required_any"):
            if not self.fields and not self.field:
                raise ValueError(f"a '{self.type}' check needs 'fields' (or 'field')")
            for name in self.fields:
                if not re.fullmatch(FIELD_PATTERN, name):
                    raise ValueError(f"invalid field name {name!r}")
        elif not self.field:
            raise ValueError(f"a '{self.type}' check needs 'field'")
        if self.type in _NEEDS_VALUE and self.value is None:
            raise ValueError(f"a '{self.type}' check needs 'value'")
        if self.type in _NEEDS_PHRASES and not self.any_of:
            raise ValueError(f"a '{self.type}' check needs 'any_of'")
        if self.type == "contains_field" and not self.other:
            raise ValueError("a 'contains_field' check needs 'other'")
        if self.type in _NEEDS_PATTERN:
            if not self.pattern:
                raise ValueError(f"a '{self.type}' check needs 'pattern'")
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"invalid pattern: {exc}") from exc
        return self

    def targets(self) -> list[str]:
        """The fields the check reads (the first one is where a problem is shown)."""
        if self.type in ("required", "required_any"):
            return list(self.fields) or [self.field or ""]
        return [self.field or ""] + ([self.other] if self.other else [])


class AppliesTo(_Model):
    """Empty lists mean "all"."""

    entities: list[str] = Field(default_factory=list, max_length=30)
    operations: list[Operation] = Field(default_factory=list, max_length=3)

    def matches(self, entity: str, operation: str) -> bool:
        return (not self.entities or entity in self.entities) and (
            not self.operations or operation in self.operations
        )


class RuleSpec(_Entry):
    kind: ClassVar[str] = "rule"

    applies_to: AppliesTo = Field(default_factory=AppliesTo)
    severity: Severity = "warning"
    check: Check
    # Shown when the check fails, e.g. "Capitalul social al unei SA este de minimum 90.000 lei".
    message: str = Field(min_length=3, max_length=500)

    def references(self) -> dict[str, list[str]]:
        return {"entity": list(self.applies_to.entities), "field": self.check.targets()}

    def text(self) -> str:
        return f"{super().text()}\n{self.message}"


class DocTypeSpec(_Entry):
    """A document type the classifier learns. ``seeds`` are typical texts of the document
    (headings, standard wording); confirmed uploads and ``docfill knowledge teach`` add real
    examples."""

    kind: ClassVar[str] = "doc_type"

    key: str = Field(pattern=DOC_TYPE_PATTERN)
    seeds: list[str] = Field(min_length=1, max_length=20)

    @field_validator("seeds")
    @classmethod
    def _long_enough(cls, seeds: list[str]) -> list[str]:
        for seed in seeds:
            if len(seed.split()) < 8:
                raise ValueError("each seed should be a typical text of at least 8 words")
        return seeds


SPEC_TYPES: dict[str, type[_Entry]] = {
    "entity": EntitySpec,
    "procedure": ProcedureSpec,
    "rule": RuleSpec,
    "doc_type": DocTypeSpec,
}
_SECTIONS = {
    "entities": "entity",
    "procedures": "procedure",
    "rules": "rule",
    "doc_types": "doc_type",
}

AnySpec = EntitySpec | ProcedureSpec | RuleSpec | DocTypeSpec


class KnowledgeFile(_Model):
    """A YAML / JSON document with any of the four sections."""

    entities: list[EntitySpec] = Field(default_factory=list)
    procedures: list[ProcedureSpec] = Field(default_factory=list)
    rules: list[RuleSpec] = Field(default_factory=list)
    doc_types: list[DocTypeSpec] = Field(default_factory=list)

    def entries(self) -> Iterator[AnySpec]:
        yield from self.entities
        yield from self.doc_types
        yield from self.rules
        yield from self.procedures

    @classmethod
    def of(cls, specs: list[AnySpec]) -> KnowledgeFile:
        data: dict[str, list[Any]] = {section: [] for section in _SECTIONS}
        for spec in specs:
            section = next(s for s, kind in _SECTIONS.items() if kind == spec.kind)
            data[section].append(spec)
        return cls(**data)

    def dump(self) -> dict[str, Any]:
        """Plain data (YAML-ready) without empty sections or default values."""
        dumped = self.model_dump(mode="json", exclude_defaults=True)
        return {section: items for section, items in dumped.items() if items}


def parse_spec(kind: str, data: dict[str, Any]) -> AnySpec:
    if kind not in SPEC_TYPES:
        raise ValueError(f"unknown knowledge kind {kind!r}; expected one of {', '.join(KINDS)}")
    return SPEC_TYPES[kind].model_validate(data)  # type: ignore[return-value]
