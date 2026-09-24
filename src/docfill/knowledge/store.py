"""The knowledge base: versioned entries, legal texts and what the wizard teaches it.

How it improves over time:

* **Fed knowledge.** Legal forms, procedures, rules and document types are added or corrected
  as YAML (CLI, API or the ``/knowledge`` page); every change is a new version kept in the
  history, and goes back to ``draft`` until a named person verifies it.
* **Fed laws.** Legal texts are split into articles and indexed; rules show the text of the
  article they cite, and everything is searchable.
* **Feedback.** Every file saved in the wizard records which rules passed, failed or were
  overridden and which documents were provided. Rules that people often override are flagged
  for review; documents often provided but missing from a procedure's list are suggested.
* **Updates keep your work.** Seeding the bundled knowledge never overwrites an entry you
  changed or retired.

Nothing is invented: rules only check values, and every entry cites its legal basis.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Integer,
    String,
    Text,
    UniqueConstraint,
    delete,
    func,
    select,
)
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from docfill.doctypes import DOC_TYPES, DocType
from docfill.errors import KnowledgeError, KnowledgeNotFoundError
from docfill.extraction.fields import FIELDS
from docfill.knowledge.checks import RuleResult, evaluate
from docfill.knowledge.loader import bundled_dir, load_directory
from docfill.knowledge.specs import (
    CATEGORIES,
    KINDS,
    OPERATIONS,
    AnySpec,
    DocTypeSpec,
    EntitySpec,
    ProcedureSpec,
    RuleSpec,
    parse_spec,
)
from docfill.knowledge.texts import (
    Document,
    SearchIndex,
    article_number,
    citation_key,
    split_passages,
)
from docfill.templates.models import Base

REVIEW_MIN_OVERRIDES = 3
REVIEW_OVERRIDE_RATE = 0.5
SUGGEST_MIN_CASES = 3
SUGGEST_RATE = 0.5
SNIPPET_CHARS = 600
LAW_TEXT_CHARS = 1500


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def spec_checksum(spec: AnySpec) -> str:
    payload = {"kind": spec.kind, **spec.model_dump(mode="json")}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- tables


class KnowledgeEntry(Base):
    __tablename__ = "knowledge_entries"
    __table_args__ = (UniqueConstraint("kind", "key", name="uq_knowledge_kind_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20), index=True)
    key: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(200))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(12), default="draft")
    origin: Mapped[str] = mapped_column(String(12), default="local")  # bundled | local
    version: Mapped[int] = mapped_column(Integer, default=1)
    checksum: Mapped[str] = mapped_column(String(64))
    verified_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


class KnowledgeRevision(Base):
    """Every change of an entry, kept forever (who, when, what)."""

    __tablename__ = "knowledge_revisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))
    key: Mapped[str] = mapped_column(String(100), index=True)
    version: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(20))  # created|updated|verified|retired|restored
    status: Mapped[str] = mapped_column(String(12))
    data: Mapped[dict[str, Any]] = mapped_column(JSON)
    actor: Mapped[str | None] = mapped_column(String(100), nullable=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class LegalSource(Base):
    __tablename__ = "legal_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    citation: Mapped[str] = mapped_column(String(200), unique=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    filename: Mapped[str] = mapped_column(String(255), default="")
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    checksum: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class LegalPassage(Base):
    __tablename__ = "legal_passages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(Integer, index=True)
    position: Mapped[int] = mapped_column(Integer)
    article: Mapped[str | None] = mapped_column(String(40), nullable=True)
    text: Mapped[str] = mapped_column(Text)


class CaseRecord(Base):
    """One file (dosar) saved in the wizard: which procedure, which documents were provided."""

    __tablename__ = "knowledge_cases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    procedure: Mapped[str] = mapped_column(String(100), index=True)
    doc_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RuleOutcomeRecord(Base):
    """How a rule fared on a saved file; ``overridden`` = it failed and the user went on."""

    __tablename__ = "knowledge_rule_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    procedure: Mapped[str] = mapped_column(String(100))
    rule: Mapped[str] = mapped_column(String(100), index=True)
    rule_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(12))  # passed | failed | skipped
    overridden: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


# --------------------------------------------------------------------------- views


@dataclass
class Entry:
    kind: str
    key: str
    spec: AnySpec
    status: str
    origin: str
    version: int
    checksum: str
    verified_by: str | None = None
    verified_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def title(self) -> str:
        return self.spec.title

    @property
    def active(self) -> bool:
        return self.status != "retired"

    def summary(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "origin": self.origin,
            "version": self.version,
            "verified_by": self.verified_by,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def as_dict(self) -> dict[str, Any]:
        return {**self.summary(), "data": self.spec.model_dump(mode="json", exclude_defaults=True)}


@dataclass
class SaveResult:
    kind: str
    key: str
    action: str  # created | updated | unchanged | kept-local | verified | restored
    version: int
    status: str

    @property
    def changed(self) -> bool:
        return self.action in ("created", "updated", "restored", "verified")

    def as_dict(self) -> dict[str, Any]:
        return {**self.__dict__, "changed": self.changed}


@dataclass
class _Passage:
    id: int
    citation: str
    source_title: str
    article: str | None
    text: str


@dataclass
class _Snapshot:
    entries: dict[tuple[str, str], Entry]
    broken: dict[tuple[str, str], str]  # entries failing validation or their checksum
    passages: list[_Passage]
    index: SearchIndex | None = None
    doc_types: tuple[DocType, ...] = field(default_factory=tuple)
    # (act "31/1990", article "10") -> the article's first passage
    articles: dict[tuple[str, str], str] = field(default_factory=dict)


def _entry_of(row: KnowledgeEntry) -> Entry:
    return Entry(
        kind=row.kind,
        key=row.key,
        spec=parse_spec(row.kind, row.data),
        status=row.status,
        origin=row.origin,
        version=row.version,
        checksum=row.checksum,
        verified_by=row.verified_by,
        verified_at=row.verified_at,
        updated_at=row.updated_at,
    )


class KnowledgeBase:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        templates: Callable[[], Iterable[str]] | None = None,
    ):
        self._sessions = session_factory
        # Names of the registered standard documents (to tell which forms docfill can fill).
        self._templates = templates or (lambda: [])
        self._lock = threading.Lock()
        self._snapshot: _Snapshot | None = None

    # ------------------------------------------------------------------ loading

    def _load(self) -> _Snapshot:
        with self._lock:
            if self._snapshot is not None:
                return self._snapshot
            entries: dict[tuple[str, str], Entry] = {}
            broken: dict[tuple[str, str], str] = {}
            with self._sessions() as session:
                for row in session.scalars(select(KnowledgeEntry)):
                    try:
                        entry = _entry_of(row)
                    except (ValidationError, ValueError) as exc:
                        broken[(row.kind, row.key)] = f"invalid: {str(exc).splitlines()[0]}"
                        continue
                    if spec_checksum(entry.spec) != row.checksum:
                        broken[(row.kind, row.key)] = "modified outside docfill (checksum)"
                        continue
                    entries[(row.kind, row.key)] = entry
                sources = {s.id: s for s in session.scalars(select(LegalSource))}
                passages = [
                    _Passage(
                        p.id,
                        sources[p.source_id].citation,
                        sources[p.source_id].title,
                        p.article,
                        p.text,
                    )
                    for p in session.scalars(
                        select(LegalPassage).order_by(LegalPassage.source_id, LegalPassage.position)
                    )
                    if p.source_id in sources
                ]
            doc_types = tuple(
                DocType(e.key, e.title, e.spec.description, tuple(e.spec.seeds))
                for (kind, _), e in sorted(entries.items())
                if kind == "doc_type" and e.active and isinstance(e.spec, DocTypeSpec)
            )
            articles: dict[tuple[str, str], str] = {}
            for passage in passages:
                number = article_number(passage.article)
                if number:
                    articles.setdefault((citation_key(passage.citation), number), passage.text)
            self._snapshot = _Snapshot(entries, broken, passages, None, doc_types, articles)
            return self._snapshot

    def invalidate(self) -> None:
        with self._lock:
            self._snapshot = None

    # ------------------------------------------------------------------ reading entries

    def entries(self, kind: str | None = None, include_retired: bool = False) -> list[Entry]:
        return [
            entry
            for (entry_kind, _), entry in sorted(self._load().entries.items())
            if (kind is None or entry_kind == kind) and (include_retired or entry.active)
        ]

    def find(self, key: str, kind: str | None = None) -> list[Entry]:
        if kind:
            entry = self._load().entries.get((kind, key))
            return [entry] if entry else []
        return [e for (_, k), e in sorted(self._load().entries.items()) if k == key]

    def get(self, kind: str, key: str) -> Entry:
        entry = self._load().entries.get((kind, key))
        if entry is None:
            raise KnowledgeNotFoundError(f"No {kind} '{key}' in the knowledge base")
        return entry

    def resolve(self, reference: str) -> Entry:
        """``procedure/srl.infiintare`` or just ``srl.infiintare`` when the key is unique."""
        kind, _, key = reference.rpartition("/")
        if kind and kind not in KINDS:
            raise KnowledgeNotFoundError(f"Unknown kind '{kind}' (use one of {', '.join(KINDS)})")
        found = self.find(key, kind or None)
        if not found:
            raise KnowledgeNotFoundError(f"Nothing called '{reference}' in the knowledge base")
        if len(found) > 1:
            kinds = ", ".join(f"{e.kind}/{e.key}" for e in found)
            raise KnowledgeError(f"'{key}' is ambiguous: {kinds}")
        return found[0]

    def doc_types(self) -> list[DocType]:
        """Document types taught through the knowledge base (for the classifier)."""
        return list(self._load().doc_types)

    def history(self, kind: str, key: str) -> list[dict[str, Any]]:
        with self._sessions() as session:
            rows = session.scalars(
                select(KnowledgeRevision)
                .where(KnowledgeRevision.kind == kind, KnowledgeRevision.key == key)
                .order_by(KnowledgeRevision.id)
            )
            return [
                {
                    "version": row.version,
                    "action": row.action,
                    "status": row.status,
                    "actor": row.actor,
                    "note": row.note,
                    "at": row.created_at.isoformat() if row.created_at else None,
                    "data": row.data,
                }
                for row in rows
            ]

    # ------------------------------------------------------------------ writing entries

    def check_references(self, specs: list[AnySpec]) -> tuple[list[str], list[str]]:
        """``(errors, warnings)`` for specs about to be saved, against what is stored."""
        errors: list[str] = []
        warnings: list[str] = []
        entities = {e.key for e in self.entries("entity")} | {
            s.key for s in specs if s.kind == "entity"
        }
        doc_types = set(DOC_TYPES) | {e.key for e in self.entries("doc_type")}
        doc_types |= {s.key for s in specs if s.kind == "doc_type"}
        templates = set(self._templates())
        for spec in specs:
            where = f"{spec.kind}/{spec.key}"
            if spec.kind == "doc_type" and spec.key in DOC_TYPES:
                errors.append(f"{where}: '{spec.key}' is a built-in document type")
            for kind, names in spec.references().items():
                for name in names:
                    if kind == "entity" and name not in entities:
                        errors.append(f"{where}: unknown legal form '{name}'")
                    elif kind == "doc_type" and name not in doc_types:
                        errors.append(f"{where}: unknown document type '{name}'")
                    elif kind == "template" and name not in templates:
                        warnings.append(
                            f"{where}: standard document '{name}' is not registered "
                            "(docfill templates seed / add)"
                        )
                    elif kind == "field" and name not in FIELDS:
                        warnings.append(f"{where}: '{name}' is not a known field (plain text)")
        return errors, list(dict.fromkeys(warnings))

    def save_all(
        self,
        specs: list[AnySpec],
        origin: str = "local",
        actor: str | None = None,
        verified_by: str | None = None,
        note: str = "",
    ) -> list[SaveResult]:
        """Validate the references of all specs, then save them (one version per change)."""
        seen: set[tuple[str, str]] = set()
        for spec in specs:
            if (spec.kind, spec.key) in seen:
                raise KnowledgeError(f"{spec.kind}/{spec.key} is defined twice")
            seen.add((spec.kind, spec.key))
        errors, _ = self.check_references(specs)
        if errors:
            raise KnowledgeError("Knowledge not saved:\n  " + "\n  ".join(errors))
        results = []
        with self._sessions() as session:
            for spec in specs:
                results.append(self._save(session, spec, origin, actor, verified_by, note))
            session.commit()
        if any(result.changed for result in results):
            self.invalidate()
        return results

    def save(self, spec: AnySpec, **options: Any) -> SaveResult:
        return self.save_all([spec], **options)[0]

    @staticmethod
    def _revision(
        session: Session, row: KnowledgeEntry, action: str, actor: str | None, note: str
    ) -> None:
        session.add(
            KnowledgeRevision(
                kind=row.kind,
                key=row.key,
                version=row.version,
                action=action,
                status=row.status,
                data=row.data,
                actor=actor,
                note=note,
            )
        )

    def _save(
        self,
        session: Session,
        spec: AnySpec,
        origin: str,
        actor: str | None,
        verified_by: str | None,
        note: str,
    ) -> SaveResult:
        checksum = spec_checksum(spec)
        row = session.scalar(
            select(KnowledgeEntry).where(
                KnowledgeEntry.kind == spec.kind, KnowledgeEntry.key == spec.key
            )
        )
        if row is not None and origin == "bundled" and row.origin == "local":
            # changed or retired here: an update of docfill never overwrites it
            return SaveResult(spec.kind, spec.key, "kept-local", row.version, row.status)
        if row is not None and row.checksum == checksum:
            if row.status == "retired" and origin != "bundled":
                action = "restored"
            elif verified_by and row.status == "draft":
                action = "verified"
            else:
                return SaveResult(spec.kind, spec.key, "unchanged", row.version, row.status)
            row.status = "verified" if verified_by else "draft"
            row.verified_by = verified_by
            row.verified_at = _utcnow() if verified_by else None
            row.origin = origin if action == "restored" else row.origin
            self._revision(session, row, action, actor or verified_by, note)
            return SaveResult(spec.kind, spec.key, action, row.version, row.status)
        action = "created" if row is None else "updated"
        if row is None:
            row = KnowledgeEntry(kind=spec.kind, key=spec.key, version=1)
            session.add(row)
        else:
            row.version += 1
        row.title = spec.title
        row.data = spec.model_dump(mode="json")
        row.checksum = checksum
        row.origin = origin
        # any change needs to be checked again by a person
        row.status = "verified" if verified_by else "draft"
        row.verified_by = verified_by
        row.verified_at = _utcnow() if verified_by else None
        session.flush()
        self._revision(session, row, action, actor or verified_by, note)
        return SaveResult(spec.kind, spec.key, action, row.version, row.status)

    def seed(self, directory: str | Path | None = None) -> list[SaveResult]:
        """Load the bundled knowledge (or a directory). Entries changed or retired locally are
        kept; bundled entries that changed come back as ``draft``."""
        specs = load_directory(directory or bundled_dir())
        return self.save_all(specs, origin="bundled", actor="docfill")

    def _set_status(self, kind: str, key: str, status: str, actor: str, note: str) -> Entry:
        with self._sessions() as session:
            row = session.scalar(
                select(KnowledgeEntry).where(KnowledgeEntry.kind == kind, KnowledgeEntry.key == key)
            )
            if row is None:
                raise KnowledgeNotFoundError(f"No {kind} '{key}' in the knowledge base")
            if status == "verified":
                if row.status == "retired":
                    raise KnowledgeError(f"{kind}/{key} is retired; add it again to use it")
                row.verified_by, row.verified_at = actor, _utcnow()
            else:  # retiring is a local decision: bundled updates will not bring it back
                row.origin = "local"
            row.status = status
            self._revision(session, row, status, actor, note)
            session.commit()
        self.invalidate()
        return self.get(kind, key)

    def verify(self, kind: str, key: str, by: str, note: str = "") -> Entry:
        """A person (lawyer, notary, expert) confirms the entry is correct and current."""
        if not by.strip():
            raise KnowledgeError("say who verified it")
        return self._set_status(kind, key, "verified", by.strip(), note)

    def retire(self, kind: str, key: str, by: str, note: str = "") -> Entry:
        """No longer applies (e.g. the law changed). Kept in the history, not used."""
        if not by.strip():
            raise KnowledgeError("say who retired it")
        return self._set_status(kind, key, "retired", by.strip(), note)

    # ------------------------------------------------------------------ procedures

    def entity(self, key: str) -> EntitySpec:
        spec = self.get("entity", key).spec
        assert isinstance(spec, EntitySpec)
        return spec

    def procedure(self, key: str) -> ProcedureSpec:
        entry = self.get("procedure", key)
        if not entry.active:
            raise KnowledgeNotFoundError(f"Procedure '{key}' is retired")
        assert isinstance(entry.spec, ProcedureSpec)
        return entry.spec

    def rules_for(self, procedure: ProcedureSpec) -> list[Entry]:
        return [
            entry
            for entry in self.entries("rule")
            if isinstance(entry.spec, RuleSpec)
            and entry.spec.applies_to.matches(procedure.entity, procedure.operation)
        ]

    def procedure_view(self, key: str) -> dict[str, Any]:
        entry = self.get("procedure", key)
        spec = entry.spec
        assert isinstance(spec, ProcedureSpec)
        templates = set(self._templates())
        entity = self.find(spec.entity, "entity")
        entity_entry = entity[0] if entity else None
        rules = self.rules_for(spec)
        used = [entry, *(e for e in [entity_entry] if e), *rules]
        used += [
            e for doc in spec.documents if doc.doc_type for e in self.find(doc.doc_type, "doc_type")
        ]
        forms = [
            {**form.model_dump(), "available": bool(form.template and form.template in templates)}
            for form in spec.forms
        ]
        entity_data = (
            {
                "key": entity_entry.key,
                "title": entity_entry.title,
                "abbreviation": entity_entry.spec.abbreviation,  # type: ignore[union-attr]
                "category": entity_entry.spec.category,  # type: ignore[union-attr]
            }
            if entity_entry
            else {"key": spec.entity, "title": spec.entity, "abbreviation": spec.entity}
        )
        return {
            **entry.summary(),
            "description": spec.description,
            "entity": entity_data,
            "operation": {"key": spec.operation, **OPERATIONS[spec.operation]},
            "forms": forms,
            "templates": [f["template"] for f in forms if f["available"]],
            "documents": [doc.model_dump(exclude_none=True) for doc in spec.documents],
            "fields": list(spec.fields),
            "required_fields": list(spec.required_fields),
            "rules": [
                {
                    "key": r.key,
                    "title": r.title,
                    "severity": r.spec.severity,  # type: ignore[union-attr]
                    "status": r.status,
                }
                for r in rules
            ],
            "legal_basis": [ref.model_dump(exclude_none=True) for ref in spec.legal_basis],
            "notes": list(spec.notes),
            "unverified": sorted({f"{e.kind}/{e.key}" for e in used if e.status != "verified"}),
        }

    def cases(self) -> dict[str, Any]:
        """Everything the wizard needs to let the user pick what they are doing."""
        found = sorted(
            (e for e in self.entries("entity") if isinstance(e.spec, EntitySpec)),
            key=lambda e: (list(CATEGORIES).index(e.spec.category), e.spec.order, e.title),
        )
        position = {e.key: index for index, e in enumerate(found)}
        operations = list(OPERATIONS)
        procedures = sorted(
            (e for e in self.entries("procedure") if isinstance(e.spec, ProcedureSpec)),
            key=lambda e: (
                position.get(e.spec.entity, len(position)),
                operations.index(e.spec.operation),
                e.key,
            ),
        )
        return {
            "categories": [{"key": k, "title": v} for k, v in CATEGORIES.items()],
            "operations": [{"key": k, **v} for k, v in OPERATIONS.items()],
            "entities": [
                {
                    "key": e.key,
                    "title": e.title,
                    "abbreviation": e.spec.abbreviation,
                    "category": e.spec.category,
                    "description": e.spec.description,
                    "status": e.status,
                }
                for e in found
            ],
            "procedures": [self.procedure_view(e.key) for e in procedures],
        }

    def evaluate(self, procedure_key: str, values: dict[str, str]) -> list[RuleResult]:
        """The procedure's legal checks on ``values``, with the cited article's text when that
        law has been fed to docfill."""
        spec = self.procedure(procedure_key)
        rules = [(e.spec, e.status, e.version) for e in self.rules_for(spec)]
        results = evaluate(rules, values)  # type: ignore[arg-type]
        for result in results:
            for ref in result.legal_basis:
                text = self.law_text(ref.get("citation", ""), ref.get("article"))
                if text:
                    result.law_text = text
                    break
        return results

    # ------------------------------------------------------------------ legal texts

    def ingest_text(
        self,
        text: str,
        citation: str,
        title: str = "",
        filename: str = "",
        url: str | None = None,
    ) -> dict[str, Any]:
        """Add (or replace, for the same citation) a legal text, split into articles."""
        citation = " ".join(citation.split())
        if len(citation) < 3:
            raise KnowledgeError("give the act's citation, e.g. 'Legea nr. 31/1990'")
        passages = split_passages(text)
        if not passages:
            raise KnowledgeError(f"{filename or citation}: no text found")
        checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._sessions() as session:
            source = session.scalar(select(LegalSource).where(LegalSource.citation == citation))
            if source is None:
                source = LegalSource(citation=citation, checksum=checksum)
                session.add(source)
            else:
                session.execute(delete(LegalPassage).where(LegalPassage.source_id == source.id))
            source.title, source.filename, source.url = title, filename, url
            source.checksum = checksum
            session.flush()
            for position, passage in enumerate(passages):
                session.add(
                    LegalPassage(
                        source_id=source.id,
                        position=position,
                        article=passage.article,
                        text=passage.text,
                    )
                )
            session.commit()
        self.invalidate()
        articles = sum(1 for p in passages if p.article)
        return {
            "citation": citation,
            "title": title,
            "passages": len(passages),
            "articles": articles,
        }

    def sources(self) -> list[dict[str, Any]]:
        with self._sessions() as session:
            counts = dict(
                session.execute(
                    select(LegalPassage.source_id, func.count()).group_by(LegalPassage.source_id)
                ).all()
            )
            return [
                {
                    "citation": s.citation,
                    "title": s.title,
                    "filename": s.filename,
                    "url": s.url,
                    "passages": counts.get(s.id, 0),
                    "added": s.created_at.isoformat() if s.created_at else None,
                }
                for s in session.scalars(select(LegalSource).order_by(LegalSource.citation))
            ]

    def remove_source(self, citation: str) -> None:
        with self._sessions() as session:
            source = session.scalar(select(LegalSource).where(LegalSource.citation == citation))
            if source is None:
                raise KnowledgeNotFoundError(f"No legal text '{citation}'")
            session.execute(delete(LegalPassage).where(LegalPassage.source_id == source.id))
            session.delete(source)
            session.commit()
        self.invalidate()

    def law_text(self, citation: str, article: str | None) -> str | None:
        """The text of ``article`` of the act ``citation``, when that act was fed."""
        number = article_number(article)
        text = self._load().articles.get((citation_key(citation), number)) if number else None
        return text[:LAW_TEXT_CHARS] if text else None

    def _index(self) -> SearchIndex:
        snapshot = self._load()
        with self._lock:
            if snapshot.index is None:
                documents = [
                    Document(
                        f"passage:{p.id}",
                        p.citation,
                        " · ".join(part for part in (p.article, p.source_title) if part),
                        p.text,
                    )
                    for p in snapshot.passages
                ]
                documents += [
                    Document(
                        f"{e.kind}:{e.key}",
                        e.title,
                        f"{e.kind} · {e.status}",
                        e.spec.text().removeprefix(e.title).strip(),
                    )
                    for e in snapshot.entries.values()
                    if e.active
                ]
                snapshot.index = SearchIndex(documents)
            return snapshot.index

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Search the fed legal texts and the knowledge entries."""
        hits = []
        for document, score in self._index().search(query, limit):
            kind, _, ref = document.ref.partition(":")
            text = document.text.strip()
            hits.append(
                {
                    "kind": "law" if kind == "passage" else kind,
                    "ref": ref,
                    "title": document.title,
                    "subtitle": document.subtitle,
                    "text": text[:SNIPPET_CHARS] + ("…" if len(text) > SNIPPET_CHARS else ""),
                    "score": score,
                }
            )
        return hits

    # ------------------------------------------------------------------ feedback

    def record_case(
        self,
        procedure_key: str,
        results: list[RuleResult],
        doc_types: Iterable[str],
    ) -> dict[str, Any]:
        """A file was saved: remember how each rule fared and which documents were provided.
        A failed rule on a saved file was overridden by the user."""
        case_id = str(uuid.uuid4())
        provided = sorted({d for d in doc_types if d})
        overridden = [r.rule for r in results if r.status == "failed"]
        with self._sessions() as session:
            session.add(CaseRecord(case_id=case_id, procedure=procedure_key, doc_types=provided))
            for result in results:
                session.add(
                    RuleOutcomeRecord(
                        case_id=case_id,
                        procedure=procedure_key,
                        rule=result.rule,
                        rule_version=result.version,
                        status=result.status,
                        overridden=result.status == "failed",
                    )
                )
            session.commit()
        return {
            "procedure": procedure_key,
            "rules_checked": sum(1 for r in results if r.status != "skipped"),
            "overridden": overridden,
            "documents": provided,
        }

    def stats(self) -> dict[str, Any]:
        snapshot = self._load()
        counts: dict[str, Counter[str]] = defaultdict(Counter)
        for (kind, _), entry in snapshot.entries.items():
            counts[kind][entry.status] += 1
        with self._sessions() as session:
            rule_rows = list(
                session.execute(
                    select(
                        RuleOutcomeRecord.rule,
                        RuleOutcomeRecord.status,
                        RuleOutcomeRecord.overridden,
                        func.count(),
                    ).group_by(
                        RuleOutcomeRecord.rule,
                        RuleOutcomeRecord.status,
                        RuleOutcomeRecord.overridden,
                    )
                )
            )
            cases = list(session.execute(select(CaseRecord.procedure, CaseRecord.doc_types)))
            sources = session.scalar(select(func.count()).select_from(LegalSource)) or 0
        per_rule: dict[str, Counter[str]] = defaultdict(Counter)
        for rule, status, overridden, count in rule_rows:
            per_rule[rule][status] += count
            if overridden:
                per_rule[rule]["overridden"] += count
        rules = []
        for rule, c in sorted(per_rule.items()):
            failed, overridden = c["failed"], c["overridden"]
            rate = overridden / failed if failed else None
            rules.append(
                {
                    "rule": rule,
                    "passed": c["passed"],
                    "failed": failed,
                    "skipped": c["skipped"],
                    "overridden": overridden,
                    "override_rate": round(rate, 3) if rate is not None else None,
                    "needs_review": overridden >= REVIEW_MIN_OVERRIDES
                    and (rate or 0) >= REVIEW_OVERRIDE_RATE,
                }
            )
        procedures = []
        by_procedure: dict[str, list[list[str]]] = defaultdict(list)
        for procedure, provided in cases:
            by_procedure[procedure].append(list(provided or []))
        for procedure, files in sorted(by_procedure.items()):
            entry = snapshot.entries.get(("procedure", procedure))
            listed = (
                {d.doc_type for d in entry.spec.documents if d.doc_type}  # type: ignore[union-attr]
                if entry
                else set()
            )
            frequency = Counter(doc for provided in files for doc in set(provided))
            suggestions = [
                {"doc_type": doc, "cases": n, "of": len(files)}
                for doc, n in frequency.most_common()
                if doc not in listed
                and doc != "other"
                and len(files) >= SUGGEST_MIN_CASES
                and n / len(files) >= SUGGEST_RATE
            ]
            procedures.append(
                {"procedure": procedure, "cases": len(files), "suggested_documents": suggestions}
            )
        return {
            "entries": {kind: dict(counter) for kind, counter in sorted(counts.items())},
            "unverified": sorted(
                f"{kind}/{key}"
                for (kind, key), e in snapshot.entries.items()
                if e.status == "draft"
            ),
            "integrity_problems": [
                {"entry": f"{kind}/{key}", "problem": problem}
                for (kind, key), problem in sorted(snapshot.broken.items())
            ],
            "legal_texts": {"sources": sources, "passages": len(snapshot.passages)},
            "rules": rules,
            "rules_to_review": [r["rule"] for r in rules if r["needs_review"]],
            "procedures": procedures,
        }

    def reset_feedback(self) -> None:
        with self._sessions() as session:
            session.execute(delete(RuleOutcomeRecord))
            session.execute(delete(CaseRecord))
            session.commit()
