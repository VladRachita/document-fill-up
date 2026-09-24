"""Learning from reviews: every document checked in the wizard makes the next one better.

When a filled document is saved, the review (what was found vs. what the user kept) is recorded
and used in five ways:

1. **Calibration** - each extraction method's confidence is corrected by its track record per
   field (and per document type). A method that is often wrong for a field drops below the
   auto-fill threshold, so it stops filling that field and only proposes it.
2. **Learned labels** - when the user types a value that was in the text but not found, the
   words printed before it (or the line above it) become a new label for that field.
3. **Spelling fixes** - corrections that only change the spelling of a place name
   ("Fagaras" -> "Făgăraș") are applied automatically next time.
4. **Document types** - every confirmed document type becomes a training example for the
   document classifier (with the person's values and all digits masked out).
5. **Memory** - fields a standard document marks as ``remember`` (the filer's own details) are
   proposed again next time.

Privacy: outcomes store no values; learned labels store label words only; examples have every
reviewed value and every digit removed; only ``remember`` fields keep their values.
"""

from __future__ import annotations

import re
import threading
import unicodedata
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, Integer, String, Text, delete, func, select
from sqlalchemy.orm import Mapped, Session, mapped_column, sessionmaker

from docfill.extraction.fields import FIELDS
from docfill.extraction.rules import general_labels
from docfill.models import ExtractedField
from docfill.templates.models import Base

PRIOR_STRENGTH = 4.0  # how many reviews the built-in confidence is worth
MIN_TYPE_OBSERVATIONS = 3
MAX_EXAMPLES_PER_TYPE = 300
_SPELLING_FIELDS = {
    "city",
    "place_of_birth",
    "birth_county",
    "region",
    "country",
    "birth_country",
    "citizenship",
    "id_issued_by",
    "street",
    "company_city",
    "company_street",
    "company_county",
    "orc_office",
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn").lower()


def _key(text: str) -> str:
    return " ".join(_fold(text).split())


def _spelling_key(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _fold(text))


# --------------------------------------------------------------------------- tables


class ReviewOutcome(Base):
    __tablename__ = "review_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    review_id: Mapped[str] = mapped_column(String(36), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    template: Mapped[str] = mapped_column(String(100), default="")
    doc_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    field: Mapped[str] = mapped_column(String(60), index=True)
    source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    outcome: Mapped[str] = mapped_column(String(12))  # accepted|corrected|added|cleared
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)


class LearnedLabel(Base):
    __tablename__ = "learned_labels"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field: Mapped[str] = mapped_column(String(60))
    label: Mapped[str] = mapped_column(String(60))
    doc_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    hits: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class SpellingFix(Base):
    __tablename__ = "spelling_fixes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    field: Mapped[str] = mapped_column(String(60))
    original_key: Mapped[str] = mapped_column(String(120))
    corrected: Mapped[str] = mapped_column(String(200))
    hits: Mapped[int] = mapped_column(Integer, default=1)


class DocExample(Base):
    __tablename__ = "doc_examples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    doc_type: Mapped[str] = mapped_column(String(50), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class RememberedValue(Base):
    __tablename__ = "remembered_values"

    field: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )


# --------------------------------------------------------------------------- review input


@dataclass
class Found:
    value: str
    source: str
    confidence: float | None = None


@dataclass
class ReviewedField:
    name: str
    final: str
    found: Found | None = None
    alternatives: list[Found] = field(default_factory=list)


@dataclass
class ReviewedDocument:
    source: str
    text: str
    doc_type: str | None = None
    detected_type: str | None = None


@dataclass
class Review:
    templates: list[str]
    fields: list[ReviewedField]
    documents: list[ReviewedDocument] = field(default_factory=list)
    remember: set[str] = field(default_factory=set)


@dataclass
class LearnedSummary:
    outcomes: dict[str, int] = field(default_factory=dict)
    new_labels: list[str] = field(default_factory=list)
    spelling_fixes: int = 0
    examples: int = 0
    remembered: int = 0

    def as_dict(self) -> dict:
        return {
            "outcomes": self.outcomes,
            "new_labels": self.new_labels,
            "spelling_fixes": self.spelling_fixes,
            "examples": self.examples,
            "remembered": self.remembered,
        }


def _same(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def outcome_of(reviewed: ReviewedField) -> str | None:
    final = reviewed.final.strip()
    if reviewed.found is None:
        return "added" if final else None
    if not final:
        return "cleared"
    return "accepted" if _same(final, reviewed.found.value) else "corrected"


_SEPARATOR_BEFORE = re.compile(r"(?:\s*[:|=]\s*|\s*(?:\.{3,}|…+|_{3,})\s*|\s+)$")


def label_for_value(text: str, value: str) -> str | None:
    """The words printed right before ``value`` (same line) or the line above it.

    Only a clear label is learned: the value must stand as whole words, be separated from the
    label (colon, dot leaders, spaces or a line break) and the label must be plain words, so
    "Cluj" inside "Jud.CJ Mun.Cluj-Napoca" teaches nothing."""
    wanted = _key(value)
    if len(wanted) < 3:
        return None
    lines = text.splitlines()
    pattern = re.compile(rf"(?<![\w.\-]){re.escape(wanted)}(?![\w\-])")
    for index, line in enumerate(lines):
        folded_line = _key(line)
        match = pattern.search(folded_line)
        if not match:
            continue
        prefix = folded_line[: match.start()]
        if prefix.strip():
            separator = _SEPARATOR_BEFORE.search(prefix)
            if not separator or not separator.group():
                return None
            prefix = prefix[: separator.start()]
        else:  # value alone on its line: the label is the previous non-empty line
            previous = next(
                (lines[j] for j in range(index - 1, max(-1, index - 3), -1) if lines[j].strip()), ""
            )
            prefix = re.sub(r"[\s:|=_….]+$", "", _key(previous))
        label = " ".join(prefix.split()[-5:])
        if 3 <= len(label) <= 50 and re.fullmatch(r"[a-z][a-z /()]*[a-z)]", label):
            return label
        return None
    return None


def mask_text(text: str, values: Iterable[str]) -> str:
    """Remove the reviewed values (and every digit) from a document text."""
    masked = _fold(text)
    for value in sorted(
        {_key(v) for v in values if v and len(v.strip()) >= 2}, key=len, reverse=True
    ):
        for token in value.split():
            if len(token) >= 2:
                masked = re.sub(rf"(?<!\w){re.escape(token)}(?!\w)", " ", masked)
    return re.sub(r"\d", "0", masked)


# --------------------------------------------------------------------------- store


@dataclass
class _Snapshot:
    counts: dict[tuple[str, str], list[int]]
    type_counts: dict[tuple[str, str, str], list[int]]
    labels: dict[str | None, dict[str, str]]
    fixes: dict[tuple[str, str], str]
    examples: list[tuple[str, str]]
    remembered: dict[str, str]


class LearningStore:
    def __init__(self, session_factory: sessionmaker[Session]):
        self._sessions = session_factory
        self._lock = threading.Lock()
        self._snapshot: _Snapshot | None = None

    # ------------------------------------------------------------------ reading

    def _load(self) -> _Snapshot:
        with self._lock:
            if self._snapshot is not None:
                return self._snapshot
            with self._sessions() as session:
                counts: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
                type_counts: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0])
                rows = session.execute(
                    select(
                        ReviewOutcome.field,
                        ReviewOutcome.source,
                        ReviewOutcome.doc_type,
                        ReviewOutcome.outcome,
                        func.count(),
                    )
                    .where(ReviewOutcome.source.is_not(None))
                    .where(ReviewOutcome.outcome != "added")
                    .group_by(
                        ReviewOutcome.field,
                        ReviewOutcome.source,
                        ReviewOutcome.doc_type,
                        ReviewOutcome.outcome,
                    )
                )
                for field_name, source, doc_type, outcome, count in rows:
                    accepted = count if outcome == "accepted" else 0
                    for bucket in (
                        counts[(field_name, source)],
                        type_counts[(doc_type or "", field_name, source)],
                    ):
                        bucket[0] += accepted
                        bucket[1] += count
                labels: dict[str | None, dict[str, str]] = defaultdict(dict)
                for row in session.scalars(select(LearnedLabel)):
                    labels[row.doc_type][row.label] = row.field
                fixes = {
                    (row.field, row.original_key): row.corrected
                    for row in session.scalars(select(SpellingFix))
                }
                examples = [(row.text, row.doc_type) for row in session.scalars(select(DocExample))]
                remembered = {
                    row.field: row.value for row in session.scalars(select(RememberedValue))
                }
            self._snapshot = _Snapshot(
                dict(counts), dict(type_counts), dict(labels), fixes, examples, remembered
            )
            return self._snapshot

    def invalidate(self) -> None:
        with self._lock:
            self._snapshot = None

    def calibrate(self, candidate: ExtractedField, doc_type: str | None = None) -> float:
        """Blend the built-in confidence with the observed acceptance rate (Beta prior)."""
        snapshot = self._load()
        accepted, total = snapshot.type_counts.get(
            (doc_type or "", candidate.name, candidate.source), [0, 0]
        )
        if total < MIN_TYPE_OBSERVATIONS:
            accepted, total = snapshot.counts.get((candidate.name, candidate.source), [0, 0])
        if total == 0:
            return candidate.confidence
        posterior = (candidate.confidence * PRIOR_STRENGTH + accepted) / (PRIOR_STRENGTH + total)
        return max(0.05, min(0.99, posterior))

    def learned_labels(self, doc_type: str | None = None) -> dict[str, str]:
        labels = self._load().labels
        return {**labels.get(None, {}), **(labels.get(doc_type, {}) if doc_type else {})}

    def fix_spelling(self, candidate: ExtractedField) -> ExtractedField:
        corrected = self._load().fixes.get((candidate.name, _spelling_key(candidate.value)))
        if corrected and corrected != candidate.value:
            candidate.value = corrected
            candidate.evidence = f"{candidate.evidence or ''} (spelling learned from reviews)"
        return candidate

    def doc_examples(self) -> list[tuple[str, str]]:
        return list(self._load().examples)

    def remembered(self) -> dict[str, str]:
        return dict(self._load().remembered)

    # ------------------------------------------------------------------ writing

    def record_review(self, review: Review) -> LearnedSummary:
        summary = LearnedSummary()
        review_id = str(uuid.uuid4())
        doc_types = [d.doc_type for d in review.documents if d.doc_type]
        main_type = doc_types[0] if len(set(doc_types)) == 1 else None
        general = general_labels()
        with self._sessions() as session:
            for reviewed in review.fields:
                outcome = outcome_of(reviewed)
                if outcome is None:
                    continue
                summary.outcomes[outcome] = summary.outcomes.get(outcome, 0) + 1
                found = reviewed.found
                session.add(
                    ReviewOutcome(
                        review_id=review_id,
                        template=",".join(review.templates),
                        doc_type=main_type,
                        field=reviewed.name,
                        source=found.source if found else None,
                        outcome=outcome,
                        confidence=found.confidence if found else None,
                    )
                )
                # an alternative candidate that turned out right is a success for its method
                for alternative in reviewed.alternatives:
                    if outcome == "corrected" and _same(alternative.value, reviewed.final):
                        session.add(
                            ReviewOutcome(
                                review_id=review_id,
                                template=",".join(review.templates),
                                doc_type=main_type,
                                field=reviewed.name,
                                source=alternative.source,
                                outcome="accepted",
                                confidence=alternative.confidence,
                            )
                        )
                if outcome in ("corrected", "added"):
                    self._learn_label(session, reviewed, review.documents, general, summary)
                if (
                    outcome == "corrected"
                    and reviewed.name in _SPELLING_FIELDS
                    and found
                    and _spelling_key(found.value) == _spelling_key(reviewed.final)
                ):
                    self._upsert_fix(session, reviewed.name, found.value, reviewed.final)
                    summary.spelling_fixes += 1
            values = [f.final for f in review.fields if f.final]
            for document in review.documents:
                if document.doc_type and document.text.strip():
                    session.add(
                        DocExample(
                            doc_type=document.doc_type,
                            text=mask_text(document.text, values)[:20000],
                        )
                    )
                    summary.examples += 1
                    self._trim_examples(session, document.doc_type)
            for reviewed in review.fields:
                if reviewed.name in review.remember and reviewed.final.strip():
                    session.merge(RememberedValue(field=reviewed.name, value=reviewed.final))
                    summary.remembered += 1
            session.commit()
        self.invalidate()
        return summary

    def _learn_label(
        self,
        session: Session,
        reviewed: ReviewedField,
        documents: list[ReviewedDocument],
        general: dict[str, str],
        summary: LearnedSummary,
    ) -> None:
        spec = FIELDS.get(reviewed.name)
        if spec is None or spec.kind in ("checkbox", "list", "sex"):
            return
        for document in documents:
            label = label_for_value(document.text, reviewed.final)
            if not label or general.get(label) == reviewed.name:
                continue
            if label in general:  # never override a built-in label of another field
                continue
            existing = session.scalar(
                select(LearnedLabel).where(
                    LearnedLabel.field == reviewed.name,
                    LearnedLabel.label == label,
                    LearnedLabel.doc_type == document.doc_type,
                )
            )
            if existing:
                existing.hits += 1
            else:
                session.add(
                    LearnedLabel(field=reviewed.name, label=label, doc_type=document.doc_type)
                )
                summary.new_labels.append(f"{label} → {reviewed.name}")
            return

    @staticmethod
    def _upsert_fix(session: Session, field_name: str, original: str, corrected: str) -> None:
        key = _spelling_key(original)
        existing = session.scalar(
            select(SpellingFix).where(
                SpellingFix.field == field_name, SpellingFix.original_key == key
            )
        )
        if existing:
            existing.corrected, existing.hits = corrected, existing.hits + 1
        else:
            session.add(SpellingFix(field=field_name, original_key=key, corrected=corrected))

    @staticmethod
    def _trim_examples(session: Session, doc_type: str) -> None:
        session.flush()
        ids = session.scalars(
            select(DocExample.id)
            .where(DocExample.doc_type == doc_type)
            .order_by(DocExample.id.desc())
            .offset(MAX_EXAMPLES_PER_TYPE)
        )
        stale = list(ids)
        if stale:
            session.execute(delete(DocExample).where(DocExample.id.in_(stale)))

    def remember_values(self, values: dict[str, str]) -> int:
        with self._sessions() as session:
            for name, value in values.items():
                if value.strip():
                    session.merge(RememberedValue(field=name, value=value))
            session.commit()
        self.invalidate()
        return len(values)

    def reset(self) -> None:
        with self._sessions() as session:
            for table in (ReviewOutcome, LearnedLabel, SpellingFix, DocExample, RememberedValue):
                session.execute(delete(table))
            session.commit()
        self.invalidate()

    # ------------------------------------------------------------------ reporting

    def stats(self) -> dict:
        with self._sessions() as session:
            rows = list(
                session.execute(
                    select(
                        ReviewOutcome.field,
                        ReviewOutcome.source,
                        ReviewOutcome.outcome,
                        func.count(),
                    ).group_by(ReviewOutcome.field, ReviewOutcome.source, ReviewOutcome.outcome)
                )
            )
            reviews = session.scalar(select(func.count(func.distinct(ReviewOutcome.review_id))))
            ordered = list(
                session.execute(
                    select(ReviewOutcome.review_id, ReviewOutcome.outcome)
                    .where(ReviewOutcome.source.is_not(None))
                    .order_by(ReviewOutcome.id)
                )
            )
            examples = dict(
                session.execute(
                    select(DocExample.doc_type, func.count()).group_by(DocExample.doc_type)
                ).all()
            )
            labels = [
                (row.label, row.field, row.doc_type, row.hits)
                for row in session.scalars(select(LearnedLabel))
            ]
            fixes = session.scalar(select(func.count()).select_from(SpellingFix))
            remembered = session.scalar(select(func.count()).select_from(RememberedValue))
        per_field: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
        for field_name, source, outcome, count in rows:
            per_field[(field_name, source or "not found")][outcome] += count
        table = []
        for (field_name, source), outcomes in sorted(per_field.items()):
            judged = sum(outcomes[k] for k in ("accepted", "corrected", "cleared"))
            table.append(
                {
                    "field": field_name,
                    "source": source,
                    **outcomes,
                    "accuracy": round(outcomes["accepted"] / judged, 3) if judged else None,
                }
            )

        def accuracy(items: list[tuple[str, str]]) -> float | None:
            judged = [o for _, o in items if o in ("accepted", "corrected", "cleared")]
            return round(judged.count("accepted") / len(judged), 3) if judged else None

        review_ids = list(dict.fromkeys(r for r, _ in ordered))
        recent_ids = set(review_ids[-10:])
        return {
            "reviews": reviews or 0,
            "accuracy_all": accuracy(ordered),
            "accuracy_last_10_reviews": accuracy([x for x in ordered if x[0] in recent_ids]),
            "fields": table,
            "examples": examples,
            "learned_labels": [
                {"label": label, "field": f, "doc_type": d, "hits": h} for label, f, d, h in labels
            ],
            "spelling_fixes": fixes or 0,
            "remembered_fields": remembered or 0,
        }


def reviewed_fields_from_rows(
    rows: Iterable[dict], final: dict[str, str], derived: set[str] | None = None
) -> list[ReviewedField]:
    """Wizard rows (``found``/``alternatives`` as sent to the page) + final values. Fields in
    ``derived`` were filled by the page from other values, so they count as derived."""
    reviewed = []
    derived = derived or set()
    for row in rows:
        found = row.get("found")
        if row["name"] in derived:
            found = {"value": final.get(row["name"], ""), "source": "derived", "confidence": None}
        reviewed.append(
            ReviewedField(
                name=row["name"],
                final=final.get(row["name"], "") or "",
                found=Found(found["value"], found["source"], found.get("confidence"))
                if found and found.get("source") not in ("system", "default", "memory")
                else None,
                alternatives=[
                    Found(a["value"], a["source"], a.get("confidence"))
                    for a in row.get("alternatives") or []
                ],
            )
        )
    return reviewed


Provider = Callable[[], list[tuple[str, str]]]
