"""End-to-end pipeline: read -> sanitize -> extract -> fill a standard document -> PDF."""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from docfill.computed import COMPUTED, with_computed
from docfill.config import Settings, get_settings
from docfill.doctypes import DocTypeClassifier, Prediction, match_form
from docfill.errors import MissingFieldsError
from docfill.export import fill_pdf_form, render_text_docx, render_text_pdf
from docfill.extraction import FieldExtractor, merge_extractions
from docfill.extraction.clauses import fold
from docfill.extraction.derive import complete_values
from docfill.extraction.fields import (
    FIELDS,
    MAX_PERSONS,
    PERSON_FIELDS,
    PERSON_PREFIXES,
    REPRESENTATIVE,
    REPRESENTATIVE_FIELDS,
    person_prefix,
    person_view,
    persons_with,
)
from docfill.extraction.rules import validate
from docfill.models import ExtractedField, ExtractionResult, RawDocument, SanitizedDocument
from docfill.readers import read_bytes
from docfill.ro import parse_amount, parse_date
from docfill.sanitize import Sanitizer
from docfill.templates.models import StandardDocument, list_cells
from docfill.templates.placeholders import (
    apply,
    choose,
    clean_fill_value,
    clean_list_value,
    parse_expression,
    preview_lines,
    render_body,
    split_row,
)


def comb_characters(value: str | None, spec: Mapping[str, Any]) -> list[str] | None:
    """The character of each box of a comb (``None`` when the value does not fit)."""
    value = (value or "").strip()
    if not value:
        return None
    kind = spec.get("format", "text")
    if kind == "date":
        parsed = parse_date(value)
        text = parsed.strftime("%d%m%Y") if parsed else ""
    elif kind == "amount":
        amount = parse_amount(value)
        text = str(round(amount)) if amount is not None else ""
    elif kind == "digits":
        text = re.sub(r"\D", "", value)
    else:
        text = value
    boxes = len(spec["boxes"])
    if not text or len(text) > boxes:
        return None
    text = text.rjust(boxes) if spec.get("align") == "right" else text.ljust(boxes)
    return list(text)


def form_values(template: StandardDocument, values: Mapping[str, str]) -> dict[str, str]:
    """PDF field name -> text for a pdf_form standard document (mapped fields, composed
    fields, option buttons, list rows, one-character boxes, blocks filled on a condition)."""
    filled: dict[str, str] = {}
    for pdf_field, expression in template.field_map.items():
        if "{{" in expression:  # "{{ last_name | upper }} {{ first_name | upper }}"
            value = " ".join(render_body(expression, values, blank="").split())
        else:
            value = apply(expression, values)
        if value and pdf_field in template.choices:
            value = choose(template.choices[pdf_field], value)
        if value:
            filled[pdf_field] = value
    for pdf_field, names in template.ticks.items():  # section boxes
        if any((values.get(name) or "").strip() for name in names):
            filled[pdf_field] = "x"
    for name, spec in template.lists.items():
        cells = list_cells(spec)
        rows = [row for row in (values.get(name) or "").splitlines() if row.strip()]
        for row_cells, row in zip(cells, rows, strict=False):
            for cell, part in zip(row_cells, split_row(row, len(row_cells)), strict=True):
                if part:
                    filled[cell] = part
    for name, spec in template.combs.items():
        chars = comb_characters(values.get(name), spec)
        for box, char in zip(spec["boxes"], chars or [], strict=bool(chars)):
            if char.strip():
                filled[box] = char
    for name, options in template.box_choices.items():
        if (value := (values.get(name) or "").strip()) and (box := choose(options, value)):
            filled[box] = "x"
    for name, pdf_fields in template.filled_when.items():
        if not (values.get(name) or "").strip():
            for pdf_field in pdf_fields:
                filled.pop(pdf_field, None)
    return filled


def read_template_values(
    template: StandardDocument, pdf_values: Mapping[str, str]
) -> dict[str, tuple[str, str]]:
    """Read a filled copy of a standard document back into docfill fields:
    ``{field: (value, pdf_field)}`` through the inverse of its field map."""
    found: dict[str, tuple[str, str]] = {}
    inverse_choices = {
        pdf_field: {state: label for label, state in mapping.items()}
        for pdf_field, mapping in template.choices.items()
    }
    for pdf_field, expression in template.field_map.items():
        value = (pdf_values.get(pdf_field) or "").strip()
        if not value or "{{" in expression:  # composed values cannot be split back
            continue
        if pdf_field in inverse_choices:
            state = next((s for s in inverse_choices[pdf_field] if s == value), None)
            value = inverse_choices[pdf_field].get(state, "") if state else ""
        name, _ = parse_expression(expression)
        if value and name not in found:
            found[name] = (value, pdf_field)
    for listed, spec in template.lists.items():
        # a list printed from a computed value is read back as the field it says (3.1 of
        # Anexa 4: the activities of the company)
        computed = COMPUTED.get(listed)
        name = computed.read_as if computed and computed.read_as else listed
        cells = list_cells(spec)
        rows = []
        for row_cells in cells:
            parts = [(pdf_values.get(cell) or "").strip() for cell in row_cells]
            if any(parts):
                separator = " | " if len(row_cells) > 2 else " "
                rows.append(separator.join(parts).strip(" |"))
        if rows:
            found[name] = ("\n".join(rows), cells[0][0])
    for name, spec in template.combs.items():
        chars = "".join((pdf_values.get(box) or "").strip()[:1] for box in spec["boxes"])
        if spec.get("format") == "date" and re.fullmatch(r"\d{8}", chars):
            chars = f"{chars[:2]}.{chars[2:4]}.{chars[4:]}"
        if chars and name not in found:
            found[name] = (chars, spec["boxes"][0])
    for name, options in template.box_choices.items():
        chosen = next((label for label, box in options.items() if pdf_values.get(box)), None)
        if chosen and name not in found:
            found[name] = (chosen, options[chosen])
    return found


def form_fields_as_candidates(
    template: StandardDocument, pdf_values: Mapping[str, str], document: str
) -> list[ExtractedField]:
    """Values of a recognised filled form: structured data, trusted almost like a manual entry."""
    candidates = []
    for name, (value, pdf_field) in read_template_values(template, pdf_values).items():
        spec = FIELDS.get(name)
        if spec is None or spec.kind == "list":
            normalised = value
        else:  # normalise like any extracted value; keep the typed text if it does not validate
            normalised = validate(spec, value) or clean_fill_value(value)
        if name == "id_type":
            normalised = normalised.upper()
        candidates.append(
            ExtractedField(
                name=name,
                value=normalised,
                confidence=FORM_CONFIDENCE,
                source="form",
                evidence=f"{template.title}: field '{pdf_field}'",
                document=document,
            )
        )
    return candidates


@dataclass
class DocumentAnalysis:
    raw: RawDocument
    sanitized: SanitizedDocument
    extraction: ExtractionResult
    doc_type: Prediction | None = None
    # Whose document it is: 1 = the applicant, 2 and 3 = other persons (their fields are
    # prefixed p2_ / p3_), 0 (``REPRESENTATIVE``) = the avocat / împuternicit filing the request.
    # ``None`` until assigned (see :meth:`DocFill.assign_persons`), and for documents that name
    # their persons themselves or name none (an act constitutiv, a proof of the firm name).
    person: int | None = None


# Documents that describe one person: each one is assigned its own person (documents with the
# same CNP go to the same person).
PERSON_DOC_TYPES = {"id_card", "declaratie_administrator"}
# Documents that number their own persons (the act constitutiv: associates, administrators,
# beneficial owners; see extraction/articles.py) and documents about no person of the request
# (the owner lending the registered office, the trade register's letterhead).
NAMED_PERSONS_DOC_TYPES = {"act_constitutiv"}
NO_PERSON_DOC_TYPES = {"dovada_denumire", "dovada_sediu"}


def names_persons(doc_type: str | None) -> bool:
    """Whether a document of this type is not assigned to one person."""
    return doc_type in NAMED_PERSONS_DOC_TYPES | NO_PERSON_DOC_TYPES


# What a document says about the applicant only (the capacity in which they sign the forms):
# not taken from the documents of other persons.
APPLICANT_ONLY = {"capacity", "represented_by", "representation_basis", "marital_regime"}


def as_representative(result: ExtractionResult) -> ExtractionResult:
    """The identity of the avocat / împuternicit filing the request: their name, identity card
    and CNP go to "Filed by" (XII), their name and address to the contact person (VII). Nothing
    else is taken from their documents: they are not a person of the company, and what they say
    about it (a company, a capacity) is not the company's."""

    def renamed(found: ExtractedField) -> list[ExtractedField]:
        return [
            found.model_copy(update={"name": target})
            for target in REPRESENTATIVE_FIELDS.get(found.name, ())
        ]

    result_for = ExtractionResult()
    for found in result.fields.values():
        for copy in renamed(found):
            result_for.fields[copy.name] = copy
    for pool in result.candidates.values():
        for candidate in pool:
            for copy in renamed(candidate):
                result_for.candidates.setdefault(copy.name, []).append(copy)
    return result_for


def for_person(result: ExtractionResult, person: int | None) -> ExtractionResult:
    """The same extraction with the fields of a person renamed for ``person`` (``cnp`` ->
    ``p2_cnp``); person 1 (the applicant) keeps the plain names; the representative's fields
    become the filer's and the contact person's. ``None``: the document numbered its persons."""
    if person is None:
        return result
    if person == REPRESENTATIVE:
        return as_representative(result)
    prefix = person_prefix(person)
    if not prefix:
        return result

    def rename(field: ExtractedField) -> ExtractedField:
        if field.name not in PERSON_FIELDS:
            return field
        return field.model_copy(update={"name": prefix + field.name})

    renamed = ExtractionResult()
    renamed.fields = {
        field.name: field
        for field in map(rename, result.fields.values())
        if field.name not in APPLICANT_ONLY
    }
    renamed.candidates = {
        (prefix + name if name in PERSON_FIELDS else name): [rename(c) for c in pool]
        for name, pool in result.candidates.items()
        if name not in APPLICANT_ONLY
    }
    return renamed


FORM_CONFIDENCE = 0.97


MEDIA_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


@dataclass
class FillResult:
    data: bytes  # the filled document: a PDF, or a Word document (``output``)
    template: str
    template_version: int
    values: dict[str, str]
    sources: dict[str, str]
    missing: list[str]
    warnings: list[str] = field(default_factory=list)
    output: str = "pdf"  # "pdf" | "docx", as the standard document says

    @property
    def pdf(self) -> bytes:
        if self.output != "pdf":
            raise ValueError(f"'{self.template}' is filled as a .{self.output} document")
        return self.data

    @property
    def suffix(self) -> str:
        return f".{self.output}"

    @property
    def media_type(self) -> str:
        return MEDIA_TYPES[self.output]


class DocFill:
    def __init__(
        self,
        settings: Settings | None = None,
        extractor: FieldExtractor | None = None,
        sanitizer: Sanitizer | None = None,
        classifier: DocTypeClassifier | None = None,
        templates: Callable[[], list[StandardDocument]] | None = None,
        memory: Callable[[], dict[str, str]] | None = None,
    ):
        self.settings = settings or get_settings()
        self.extractor = extractor or FieldExtractor(self.settings)
        self.sanitizer = sanitizer or Sanitizer()
        self.classifier = classifier or DocTypeClassifier()
        # Registered standard documents, used to recognise and read filled PDF forms.
        self.templates = templates or (lambda: [])
        # Remembered values of ``remember`` fields (the filer's own details).
        self.memory = memory or (lambda: {})

    # ------------------------------------------------------------------ scanning

    def detect_type(
        self, raw: RawDocument, sanitized: SanitizedDocument
    ) -> tuple[Prediction, StandardDocument | None]:
        if raw.form_values:
            templates = [t for t in self.templates() if t.kind == "pdf_form"]
            match = match_form(
                raw.form_values, ((t.name, t.doc_type, t.pdf_field_names()) for t in templates)
            )
            if match:  # several standard documents can share a form (e.g. Anexa 2a variants)
                filled = set(raw.form_values)
                template = max(
                    (t for t in templates if t.doc_type == match.doc_type),
                    key=lambda t: len(filled & set(t.pdf_field_names())),
                )
                return match, template
        return self.classifier.predict(sanitized.text), None

    def analyze_bytes(
        self, data: bytes, filename: str, doc_type: str | None = None
    ) -> DocumentAnalysis:
        """Read, sanitize, detect the document type (unless given) and extract fields."""
        raw = read_bytes(data, filename, self.settings)
        sanitized = self.sanitizer.sanitize(raw)
        prediction, template = self.detect_type(raw, sanitized)
        if doc_type and doc_type != prediction.doc_type:
            prediction = Prediction(doc_type, 1.0, "user")
            template = next((t for t in self.templates() if t.doc_type == doc_type), None)
        extra = (
            form_fields_as_candidates(template, raw.form_values, filename)
            if template and raw.form_values
            else []
        )
        extraction = self.extractor.extract(
            sanitized, prediction.doc_type, extra, use_text=not extra
        )
        return DocumentAnalysis(raw, sanitized, extraction, prediction)

    def analyze_file(self, path: str | Path) -> DocumentAnalysis:
        path = Path(path)
        return self.analyze_bytes(path.read_bytes(), path.name)

    @staticmethod
    def assign_persons(analyses: Sequence[DocumentAnalysis]) -> None:
        """Give every document a person.

        An act constitutiv numbers its persons itself (person 1 is the administrator who signs
        the requests) and the proofs of the firm name and of the registered office are about no
        person: they get none. A document with the CNP (or the name, or the identity card
        number) of a person already known goes to that person (a person's identity card, their
        sworn statement). With an act constitutiv, an identity card of nobody it names is the
        representative filing the request (avocat / împuternicit). Otherwise each identity card
        or personal statement is a new person (the first is person 1, the applicant; then
        persons 2, 3); everything else is person 1. Persons already assigned (e.g. chosen in the
        wizard) are kept."""

        def doc_type(analysis: DocumentAnalysis) -> str | None:
            return analysis.doc_type.doc_type if analysis.doc_type else None

        def cnp_of(analysis: DocumentAnalysis, prefix: str = "") -> str | None:
            found = analysis.extraction.fields.get(prefix + "cnp")
            return found.value if found and not found.issues else None

        def key_of(analysis: DocumentAnalysis, names: tuple[str, ...], prefix: str = "") -> str:
            """The values of ``names`` as one key (letters and digits, no accents), or ``""``
            when one is missing or doubtful."""
            fields = analysis.extraction.fields
            parts = [fields.get(prefix + name) for name in names]
            if not all(part and not part.issues for part in parts):
                return ""
            return re.sub(r"[^a-z0-9]", "", fold(" ".join(part.value for part in parts if part)))

        def name_of(analysis: DocumentAnalysis, prefix: str = "") -> str:
            return key_of(analysis, ("last_name", "first_name"), prefix)

        def card_of(analysis: DocumentAnalysis, prefix: str = "") -> str:
            return key_of(analysis, ("id_series", "id_number"), prefix)

        by_cnp: dict[str, int] = {}
        by_key: dict[str, int] = {}  # a name, an identity card series and number
        taken: set[int] = set()
        named = False
        for analysis in analyses:
            if names_persons(doc_type(analysis)):
                analysis.person = None
            if doc_type(analysis) not in NAMED_PERSONS_DOC_TYPES:
                continue
            for index, prefix in enumerate(PERSON_PREFIXES, start=1):
                cnp = cnp_of(analysis, prefix)
                keys = [
                    key for key in (name_of(analysis, prefix), card_of(analysis, prefix)) if key
                ]
                if cnp:
                    by_cnp.setdefault(cnp, index)
                for key in keys:
                    by_key.setdefault(key, index)
                if cnp or keys:
                    taken.add(index)
                    named = True
        for analysis in analyses:
            if analysis.person is None:
                continue
            taken.add(analysis.person)
            if cnp := cnp_of(analysis):
                by_cnp.setdefault(cnp, analysis.person)
        representative = REPRESENTATIVE in taken
        for analysis in analyses:
            if analysis.person is not None or names_persons(doc_type(analysis)):
                continue
            cnp = cnp_of(analysis)
            if cnp in by_cnp:
                analysis.person = by_cnp[cnp]
                continue
            # the CNP misread on a scan: the name or the identity card number may still match
            if known := next(
                (by_key[key] for key in (name_of(analysis), card_of(analysis)) if key in by_key),
                None,
            ):
                analysis.person = known
                continue
            analysis.person = 1
            if doc_type(analysis) not in PERSON_DOC_TYPES:
                continue
            if named and not representative:  # nobody the act constitutiv names
                analysis.person = REPRESENTATIVE
                representative = True
                if cnp:
                    by_cnp[cnp] = REPRESENTATIVE
                continue
            free = next((p for p in range(1, MAX_PERSONS + 1) if p not in taken), None)
            if free is None:
                analysis.raw.warnings.append(
                    f"docfill fills at most {MAX_PERSONS} persons: this identity card was "
                    "counted as person 1; choose its person in the wizard."
                )
                continue
            analysis.person = free
            taken.add(free)
            if cnp:
                by_cnp[cnp] = free

    @classmethod
    def combine(cls, analyses: Sequence[DocumentAnalysis]) -> ExtractionResult:
        """Merge several source documents, each for its person; the most confident value per
        field wins."""
        cls.assign_persons(analyses)
        return merge_extractions(
            for_person(analysis.extraction, analysis.person) for analysis in analyses
        )

    def extract_texts(
        self,
        documents: Iterable[
            tuple[str, str] | tuple[str, str, str | None] | tuple[str, str, str | None, int]
        ],
    ) -> ExtractionResult:
        """Re-run extraction on (source, text[, doc_type[, person]]) tuples, e.g. after a user
        corrected the OCR text, the detected document type or whose document it is (0: the
        representative filing the request; ignored for an act constitutiv, which numbers its
        persons, and for the proofs of the firm name and of the registered office)."""
        results = []
        for source, text, *rest in documents:
            doc_type = rest[0] if rest and rest[0] else None
            person: int | None = rest[1] if len(rest) > 1 and rest[1] is not None else 1
            document = SanitizedDocument(source=source, text=text)
            if doc_type is None:
                doc_type = self.classifier.predict(text).doc_type
            if names_persons(doc_type):
                person = None
            results.append(for_person(self.extractor.extract(document, doc_type), person))
        return merge_extractions(results)

    # ------------------------------------------------------------------ filling

    def collect_values(
        self,
        extraction: ExtractionResult | None,
        overrides: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Values usable for filling and where each came from."""
        values: dict[str, str] = {"today": date.today().strftime("%d.%m.%Y")}
        sources: dict[str, str] = {"today": "system"}
        if extraction:
            for name, extracted in extraction.fields.items():
                if extracted.confidence >= self.settings.min_confidence:
                    values[name] = extracted.value
                    sources[name] = f"{extracted.source} ({extracted.confidence:.2f})"
        for name, value in (overrides or {}).items():
            values[name] = value
            sources[name] = "manual"
        cleaned = {
            name: clean_list_value(value)
            if name in FIELDS and FIELDS[name].kind == "list"
            else clean_fill_value(value)
            for name, value in values.items()
        }
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
        remembered = self.memory()
        for name in template.remember:  # the filer's own details, from the last document
            if name not in values and remembered.get(name):
                values[name], sources[name] = remembered[name], "memory"
        for name, value in template.defaults.items():
            if name not in values:
                values[name], sources[name] = value, "default"
        for name, (value, reason) in complete_values(values).items():
            values[name], sources[name] = value, reason
        for name, value in with_computed(values).items():
            if name not in values:
                values[name], sources[name] = value, "computed"
        missing = [name for name in template.required_fields() if name not in values]
        if missing and not allow_missing:
            raise MissingFieldsError(missing)

        metadata = {
            "title": template.title,
            "subject": f"Standard document '{template.name}' v{template.version}",
            "keywords": f"docfill; template-checksum={template.checksum}",
        }
        if template.kind == "text" and template.output == "docx":
            lines = preview_lines(template.body or "", values)
            data = render_text_docx(lines, metadata, template.bold)
        elif template.kind == "text":
            body = render_body(template.body or "", values)
            data = render_text_pdf(body, metadata, self.settings)
        else:
            data = fill_pdf_form(
                template.pdf_data or b"",
                form_values(template, values),
                {f"/{key.capitalize()}": value for key, value in metadata.items()},
                self.settings.resolve_font_path(),
            )

        used = {name: values[name] for name in template.field_names() if name in values}
        return FillResult(
            data=data,
            output=template.output,
            template=template.name,
            template_version=template.version,
            values=used,
            sources={name: sources[name] for name in used},
            missing=missing,
            warnings=[f"No value for '{name}', left blank" for name in missing],
        )

    def fill_each(
        self,
        template: StandardDocument,
        extraction: ExtractionResult | None = None,
        overrides: Mapping[str, str] | None = None,
        allow_missing: bool = False,
    ) -> list[tuple[int | None, FillResult]]:
        """Fill ``template`` once, or once per person for a document made for each person (a
        sworn statement for each administrator): ``[(person or None, result)]``. The missing
        fields of a person are named for that person (``p2_id_number``)."""
        if not template.per_person:
            return [(None, self.fill(template, extraction, overrides, allow_missing))]
        values, _ = self.collect_values(extraction, overrides)
        for name, (value, _) in complete_values(values).items():
            values.setdefault(name, value)
        results: list[tuple[int | None, FillResult]] = []
        missing: list[str] = []
        for person in persons_with(values, template.per_person):
            result = self.fill(template, None, person_view(values, person), allow_missing=True)
            prefix = person_prefix(person)
            result.missing = [prefix + n if n in PERSON_FIELDS else n for n in result.missing]
            missing += [name for name in result.missing if name not in missing]
            results.append((person, result))
        if missing and not allow_missing:
            raise MissingFieldsError(missing)
        return results

    def process(
        self,
        files: Iterable[tuple[str, bytes]],
        template: StandardDocument,
        overrides: Mapping[str, str] | None = None,
        allow_missing: bool = False,
    ) -> tuple[FillResult, list[DocumentAnalysis]]:
        analyses = [self.analyze_bytes(data, name) for name, data in files]
        combined = self.combine(analyses)
        result = self.fill(template, combined, overrides, allow_missing)
        read_warnings = [warning for analysis in analyses for warning in analysis.raw.warnings]
        result.warnings = read_warnings + result.warnings
        return result, analyses
