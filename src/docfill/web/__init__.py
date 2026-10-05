"""Web wizard: a step-by-step page to scan documents, check and correct what was found in real
time and save the filled reference documents under new file names. Every saved review teaches
docfill (see :mod:`docfill.learning`).

Routes are registered on the main FastAPI app by :func:`register_wizard`.
"""

# No ``from __future__ import annotations``: FastAPI resolves the local ``Repo`` alias below.

import re
from collections.abc import Callable, Iterator
from functools import cache
from importlib import resources
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, model_validator

from docfill.app import App
from docfill.errors import MissingFieldsError
from docfill.extraction import merge_extractions
from docfill.extraction.fields import (
    CONTROL_OPTIONS,
    MAX_PERSONS,
    REPRESENTATIVE,
    REPRESENTATIVE_TYPES,
    person_prefix,
)
from docfill.knowledge import ProcedureSpec, RuleResult
from docfill.learning import Review, ReviewedDocument, reviewed_fields_from_rows
from docfill.models import ExtractedField, ExtractionResult
from docfill.pipeline import (
    MEDIA_TYPES,
    NAMED_PERSONS_DOC_TYPES,
    NO_PERSON_DOC_TYPES,
    DocumentAnalysis,
)
from docfill.templates import TemplateRepository
from docfill.templates.models import StandardDocument
from docfill.wizard import (
    assist,
    build_preview,
    field_rows,
    other_fields,
    safe_filename,
    save_output,
    suggest_filename,
    with_operation,
)

MAX_DOCUMENT_CHARS = 500_000


class _Templates(BaseModel):
    """Accepts ``templates: [...]`` (several reference documents) or a single ``template``,
    and optionally the ``procedure`` being done (e.g. ``srl.infiintare``): its extra fields are
    asked for and its legal checks run."""

    templates: list[str] = Field(default_factory=list, max_length=10)
    template: str | None = None
    procedure: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def _merge(self) -> "_Templates":
        if self.template and self.template not in self.templates:
            self.templates.insert(0, self.template)
        if not self.templates:
            raise ValueError("choose at least one reference document")
        return self


class DocumentText(BaseModel):
    source: str = Field(max_length=255)
    text: str = Field(max_length=MAX_DOCUMENT_CHARS)
    doc_type: str | None = None
    detected_type: str | None = None
    # Whose document it is: 1 = the applicant, 2 or 3 = another person (shareholder...), 0 = the
    # representative filing the request (avocat / împuternicit). Ignored for documents that name
    # their persons themselves (act constitutiv) or none (proof of the firm name / office).
    person: int = Field(default=1, ge=REPRESENTATIVE, le=MAX_PERSONS)


class ReextractIn(_Templates):
    documents: list[DocumentText] = Field(default_factory=list, max_length=20)


class PreviewIn(_Templates):
    values: dict[str, str] = Field(default_factory=dict, max_length=500)
    # Fields the page filled by derivation (e.g. from the CNP), not typed by the user.
    derived: list[str] = Field(default_factory=list, max_length=500)


class ExportIn(PreviewIn):
    filename: str | None = Field(default=None, max_length=200)
    allow_missing: bool = False
    # What the wizard showed (rows) and the documents it was based on: used for learning.
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=500)
    documents: list[DocumentText] = Field(default_factory=list, max_length=20)
    # The user saw the failed legal checks of the procedure and goes on anyway.
    legal_acknowledged: bool = False


# The roles of the persons given apart in step 1: the administrator, and the sole associate who
# holds every share and is the beneficial owner (Legea nr. 129/2019, art. 4 alin. (2) lit. a)).
_ADMINISTRATOR_ROLES = {"board_role": "administrator unic"}
COMPANY_SHEET = "date_societate"  # the type of the sheet given in its own box in step 1


def _associate_roles(administrator: bool) -> dict[str, str]:
    roles = {
        "associate": "x",
        "beneficial_owner": CONTROL_OPTIONS[0],
        "control_description": (
            "deținere directă a unui procent de peste 25% din părțile sociale, respectiv 100%"
        ),
    }
    return {**roles, **_ADMINISTRATOR_ROLES} if administrator else roles


def read_upload(upload: UploadFile, limit: int) -> tuple[str, bytes]:
    data = upload.file.read(limit + 1)
    name = upload.filename or "upload"
    if len(data) > limit:
        raise HTTPException(413, f"{name} is too large")
    return name, data


def saved_file(directory: Path, filename: str) -> FileResponse:
    """A document docfill saved in ``directory`` (PDF or Word), asked for by its bare name."""
    match = re.fullmatch(r"[\w.-]+\.(pdf|docx)", filename)
    if not match or filename != safe_filename(filename, f".{match[1]}"):
        raise HTTPException(404, "not found")
    path = directory / filename
    if not path.is_file():
        raise HTTPException(404, "not found")
    return FileResponse(path, media_type=MEDIA_TYPES[match[1]], filename=filename)


@cache
def wizard_page() -> str:
    return (resources.files("docfill.web") / "wizard.html").read_text(encoding="utf-8")


def _document_json(analysis: DocumentAnalysis) -> dict[str, Any]:
    prediction = analysis.doc_type
    return {
        "source": analysis.raw.source,
        "type": analysis.raw.doc_type.value,
        "pages": len(analysis.raw.pages),
        "ocr": analysis.raw.used_ocr,
        "warnings": analysis.raw.warnings,
        "redactions": analysis.sanitized.redactions,
        "removed_lines": analysis.sanitized.removed_lines,
        "form_fields": len(analysis.raw.form_values),
        "raw_text": analysis.raw.text,
        "text": analysis.sanitized.text,
        "doc_type": prediction.doc_type if prediction else None,
        "detected_type": prediction.doc_type if prediction else None,
        "type_confidence": prediction.confidence if prediction else None,
        "type_method": prediction.method if prediction else None,
        "type_scores": prediction.scores if prediction else {},
        "person": analysis.person,
    }


def dossier(
    procedure: ProcedureSpec,
    doc_types: set[str],
    created: set[str],
    available: set[str] | None = None,
) -> dict[str, Any]:
    """The file to submit: the forms (created by docfill, available but not chosen this time,
    or still to obtain) and the supporting documents, ticked off when an uploaded file was
    recognised as that type."""
    available = created if available is None else available
    return {
        "procedure": procedure.key,
        "title": procedure.title,
        "forms": [
            {
                **form.model_dump(),
                "created": bool(form.template and form.template in created),
                "available": bool(form.template and form.template in available),
            }
            for form in procedure.forms
        ],
        "documents": [
            {**doc.model_dump(exclude={"legal_basis"}), "provided": doc.doc_type in doc_types}
            for doc in procedure.documents
        ],
    }


def person_name(values: dict[str, str], person: int) -> str:
    prefix = person_prefix(person)
    return " ".join(
        filter(None, (values.get(prefix + "last_name"), values.get(prefix + "first_name")))
    ).upper()


def relevant(
    results: list[RuleResult], templates: list[StandardDocument], procedure: ProcedureSpec
) -> list[RuleResult]:
    """The legal checks about the documents being filled: a check whose field none of the
    chosen documents (nor the procedure) asks for is left out, e.g. the beneficial owners when
    the beneficial owner declaration is not filled this time."""
    asked = {name for template in templates for name in template.input_fields()}
    asked |= set(procedure.fields) | set(procedure.required_fields)
    return [result for result in results if not result.field or result.field in asked]


def failed_errors(results: list[RuleResult]) -> list[RuleResult]:
    return [r for r in results if r.status == "failed" and r.severity == "error"]


def register_wizard(
    app: FastAPI,
    context: App,
    repository: Callable[[], Iterator[TemplateRepository]],
) -> None:
    settings, docfill, learning = context.settings, context.docfill, context.learning
    knowledge = context.knowledge
    Repo = Annotated[TemplateRepository, Depends(repository)]

    def load(repo: TemplateRepository, names: list[str]) -> list[StandardDocument]:
        return [repo.get(name) for name in names]

    def procedure_of(key: str | None) -> ProcedureSpec | None:
        return knowledge.procedure(key) if key else None

    def review(
        templates: list[StandardDocument],
        extraction: ExtractionResult | None,
        procedure: ProcedureSpec | None = None,
    ) -> dict[str, Any]:
        if procedure:  # the operation ticks its request (înmatriculare / modificare / radiere)
            extraction = with_operation(extraction, procedure.operation, procedure.title, templates)
        rows = field_rows(
            templates,
            extraction,
            settings.min_confidence,
            learning.remembered(),
            extra_fields=procedure.fields if procedure else (),
            extra_required=procedure.required_fields if procedure else (),
        )
        return {
            "templates": [template.summary() for template in templates],
            "rows": [row.as_dict() for row in rows],
            "other_fields": other_fields(templates, extraction) if extraction else [],
            "min_confidence": settings.min_confidence,
        }

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/wizard")

    @app.get("/wizard", response_class=HTMLResponse, include_in_schema=False)
    def wizard() -> HTMLResponse:
        return HTMLResponse(wizard_page())

    @app.get("/doctypes", tags=["wizard"])
    def doc_types() -> list[dict[str, str]]:
        """Document types docfill can recognise (built-in and taught in the knowledge base).
        ``persons``: ``one`` (the document is one person's), ``named`` (it names its persons
        itself: an act constitutiv) or ``none`` (it is about no person of the request)."""
        return [
            {
                "name": d.name,
                "label": d.label,
                "description": d.description,
                "persons": "named"
                if d.name in NAMED_PERSONS_DOC_TYPES
                else "none"
                if d.name in NO_PERSON_DOC_TYPES
                else "one",
            }
            for d in docfill.classifier.types().values()
        ]

    @app.post("/wizard/analyze", tags=["wizard"])
    def analyze(
        repo: Repo,
        files: Annotated[list[UploadFile] | None, File()] = None,
        templates: Annotated[list[str] | None, Form()] = None,
        template: Annotated[str | None, Form()] = None,
        procedure: Annotated[str | None, Form()] = None,
        representative: Annotated[list[UploadFile] | None, File()] = None,
        representative_type: Annotated[str | None, Form()] = None,
        associate: Annotated[list[UploadFile] | None, File()] = None,
        administrator: Annotated[list[UploadFile] | None, File()] = None,
        company: Annotated[list[UploadFile] | None, File()] = None,
    ) -> dict[str, Any]:
        """Step 2: read + sanitize each file, detect its type, extract fields for the chosen
        reference documents (and the procedure's own fields). ``representative``: the identity
        card of the lawyer / proxy filing the request, given apart in step 1 (it fills "Filed
        by" and the contact person, never a person); ``representative_type``: lawyer or proxy,
        as chosen with it. ``associate``: the identity card of the sole associate (also the
        beneficial owner, 100%), the administrator too unless ``administrator`` gives the
        administrator's identity card (then person 1, who signs the requests; the associate is
        person 2). ``company``: the client's sheet with the CAEN activities and the share capital
        (read as such whatever its wording)."""
        names = [n for n in [template, *(templates or [])] if n]
        if not names:
            raise HTTPException(422, "choose at least one reference document")
        if not any((files, representative, associate, administrator, company)):
            raise HTTPException(422, "add at least one file to read")
        if representative_type and representative_type not in REPRESENTATIVE_TYPES:
            raise HTTPException(422, f"representative_type: one of {list(REPRESENTATIVE_TYPES)}")
        chosen = load(repo, list(dict.fromkeys(names)))
        spec = procedure_of(procedure)
        uploads = [read_upload(upload, settings.max_file_size) for upload in files or []]
        analyses = [docfill.analyze_bytes(data, name) for name, data in uploads]
        for upload in company or []:  # the activities and the capital, given apart
            name, data = read_upload(upload, settings.max_file_size)
            analyses.append(docfill.analyze_bytes(data, name, COMPANY_SHEET))
        for upload in representative or []:
            name, data = read_upload(upload, settings.max_file_size)
            analysis = docfill.analyze_bytes(data, name)
            analysis.person = REPRESENTATIVE  # given as the representative's: kept as such
            analyses.append(analysis)
        # the sole associate and the administrator, given apart: their roles come with them
        for upload, person, roles in [
            *(
                (u, 2 if administrator else 1, _associate_roles(not administrator))
                for u in associate or []
            ),
            *((u, 1, _ADMINISTRATOR_ROLES) for u in administrator or []),
        ]:
            name, data = read_upload(upload, settings.max_file_size)
            analysis = docfill.analyze_bytes(data, name)
            analysis.person = person
            for role, value in roles.items():
                analysis.extraction.replace(
                    ExtractedField(
                        name=role,
                        value=value,
                        confidence=1.0,
                        source="manual",
                        evidence="given in step 1 with the identity card",
                        document=name,
                    )
                )
            analyses.append(analysis)
        extraction = docfill.combine(analyses)  # assigns each identity card to a person
        if representative_type:  # chosen in step 1: writes "prin ... conform ..." (derived)
            chosen_type = ExtractionResult()
            chosen_type.offer(
                ExtractedField(
                    name="representative_type",
                    value=representative_type,
                    confidence=1.0,
                    source="manual",
                    evidence="chosen with the representative's identity card",
                )
            )
            extraction = merge_extractions([extraction, chosen_type])
        return {
            "documents": [_document_json(a) for a in analyses],
            **review(chosen, extraction, spec),
        }

    @app.post("/wizard/reextract", tags=["wizard"])
    def reextract(payload: ReextractIn, repo: Repo) -> dict[str, Any]:
        """Re-run extraction on corrected text or a corrected document type."""
        chosen = load(repo, payload.templates)
        spec = procedure_of(payload.procedure)
        if not payload.documents:
            return review(chosen, None, spec)
        extraction = docfill.extract_texts(
            (doc.source, doc.text, doc.doc_type, doc.person) for doc in payload.documents
        )
        return review(chosen, extraction, spec)

    @app.post("/wizard/preview", tags=["wizard"])
    def preview(payload: PreviewIn, repo: Repo) -> dict[str, Any]:
        """Live previews of the reference documents, problems found, derivable values and the
        procedure's legal checks."""
        chosen = load(repo, payload.templates)
        spec = procedure_of(payload.procedure)
        values, _ = docfill.collect_values(None, payload.values)
        legal = relevant(knowledge.evaluate(spec.key, values), chosen, spec) if spec else []
        return {
            "previews": [build_preview(template, values) for template in chosen],
            "suggested_filename": suggest_filename(chosen, values).removesuffix(".pdf"),
            "legal": [result.as_dict() for result in legal],
            **assist(values, payload.derived),
        }

    @app.post("/wizard/export", tags=["wizard"])
    def export(payload: ExportIn, repo: Repo) -> dict[str, Any]:
        """Create every document (PDF, or Word for the documents written as such), save them
        under the chosen name and learn from the review. With a
        procedure, its required fields and failed legal checks (errors) must be dealt with, or
        explicitly accepted (``allow_missing`` / ``legal_acknowledged``)."""
        chosen = load(repo, payload.templates)
        spec = procedure_of(payload.procedure)
        values, _ = docfill.collect_values(None, payload.values)
        legal = relevant(knowledge.evaluate(spec.key, values), chosen, spec) if spec else []
        if spec and not payload.allow_missing:
            missing = [name for name in spec.required_fields if name not in values]
            if missing:
                raise MissingFieldsError(missing)
        if failed_errors(legal) and not payload.legal_acknowledged:
            problems = "; ".join(f"{r.title}: {r.message}" for r in failed_errors(legal))
            raise HTTPException(422, f"Legal checks failed: {problems}")
        results = []
        for template in chosen:  # fail before saving anything if a document is incomplete
            for person, result in docfill.fill_each(
                template, None, payload.values, payload.allow_missing
            ):
                results.append((template, person, result))
        base = safe_filename(payload.filename or suggest_filename(chosen, values))
        base = base.removesuffix(".pdf")
        files = []
        for template, person, result in results:
            name = base if len(results) == 1 else f"{base}_{template.name}"
            title = template.title
            if person is not None:  # one copy per person: named after the person
                who = person_name(values, person)
                name, title = f"{name}_{who or person}", f"{title} - {who or f'persoana {person}'}"
            path = save_output(settings.output_dir, name, result.data, result.suffix)
            files.append(
                {
                    "template": template.name,
                    "title": title,
                    "person": person,
                    "filename": path.name,
                    "saved_to": str(path),
                    "url": f"/wizard/files/{path.name}",
                    "missing": result.missing,
                }
            )
        learned = learning.record_review(
            Review(
                templates=[t.name for t in chosen],
                fields=reviewed_fields_from_rows(
                    payload.rows, payload.values, set(payload.derived)
                ),
                documents=[
                    ReviewedDocument(d.source, d.text, d.doc_type, d.detected_type)
                    for d in payload.documents
                ],
                remember={name for t in chosen for name in t.remember},
            )
        )
        response: dict[str, Any] = {"files": files, "learned": learned.as_dict()}
        if spec:
            provided = {d.doc_type for d in payload.documents if d.doc_type}
            response["legal"] = [result.as_dict() for result in legal]
            available = {t.name for t in repo.list()}
            response["dossier"] = dossier(spec, provided, {t.name for t in chosen}, available)
            response["knowledge"] = knowledge.record_case(spec.key, legal, provided)
        return response

    @app.get("/wizard/files/{filename}", tags=["wizard"])
    def download(filename: str) -> FileResponse:
        """Download a document saved by the wizard (PDF or Word)."""
        return saved_file(settings.output_dir, filename)


_ = MissingFieldsError  # raised by docfill.fill, turned into 422 by the app's handler
