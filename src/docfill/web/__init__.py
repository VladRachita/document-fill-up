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
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field, model_validator

from docfill.app import App
from docfill.doctypes import DOC_TYPES
from docfill.errors import MissingFieldsError
from docfill.learning import Review, ReviewedDocument, reviewed_fields_from_rows
from docfill.models import ExtractionResult
from docfill.pipeline import DocumentAnalysis
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
)

MAX_DOCUMENT_CHARS = 500_000


class _Templates(BaseModel):
    """Accepts ``templates: [...]`` (several reference documents) or a single ``template``."""

    templates: list[str] = Field(default_factory=list, max_length=10)
    template: str | None = None

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


class ReextractIn(_Templates):
    documents: list[DocumentText] = Field(default_factory=list, max_length=20)


class PreviewIn(_Templates):
    values: dict[str, str] = Field(default_factory=dict, max_length=300)


class ExportIn(PreviewIn):
    filename: str | None = Field(default=None, max_length=200)
    allow_missing: bool = False
    # What the wizard showed (rows) and the documents it was based on: used for learning.
    rows: list[dict[str, Any]] = Field(default_factory=list, max_length=300)
    documents: list[DocumentText] = Field(default_factory=list, max_length=20)
    # Fields the page filled by derivation (e.g. from the CNP), not typed by the user.
    derived: list[str] = Field(default_factory=list, max_length=300)


def read_upload(upload: UploadFile, limit: int) -> tuple[str, bytes]:
    data = upload.file.read(limit + 1)
    name = upload.filename or "upload"
    if len(data) > limit:
        raise HTTPException(413, f"{name} is too large")
    return name, data


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
    }


def register_wizard(
    app: FastAPI,
    context: App,
    repository: Callable[[], Iterator[TemplateRepository]],
) -> None:
    settings, docfill, learning = context.settings, context.docfill, context.learning
    Repo = Annotated[TemplateRepository, Depends(repository)]

    def load(repo: TemplateRepository, names: list[str]) -> list[StandardDocument]:
        return [repo.get(name) for name in names]

    def review(
        templates: list[StandardDocument], extraction: ExtractionResult | None
    ) -> dict[str, Any]:
        rows = field_rows(templates, extraction, settings.min_confidence, learning.remembered())
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
        """Document types docfill can recognise."""
        return [
            {"name": d.name, "label": d.label, "description": d.description}
            for d in DOC_TYPES.values()
        ]

    @app.post("/wizard/analyze", tags=["wizard"])
    def analyze(
        repo: Repo,
        files: Annotated[list[UploadFile], File()],
        templates: Annotated[list[str] | None, Form()] = None,
        template: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        """Step 2: read + sanitize each file, detect its type, extract fields for the chosen
        reference documents."""
        names = [n for n in [template, *(templates or [])] if n]
        if not names:
            raise HTTPException(422, "choose at least one reference document")
        chosen = load(repo, list(dict.fromkeys(names)))
        uploads = [read_upload(upload, settings.max_file_size) for upload in files]
        analyses = [docfill.analyze_bytes(data, name) for name, data in uploads]
        extraction = docfill.combine(analyses)
        return {"documents": [_document_json(a) for a in analyses], **review(chosen, extraction)}

    @app.post("/wizard/reextract", tags=["wizard"])
    def reextract(payload: ReextractIn, repo: Repo) -> dict[str, Any]:
        """Re-run extraction on corrected text or a corrected document type."""
        chosen = load(repo, payload.templates)
        if not payload.documents:
            return review(chosen, None)
        extraction = docfill.extract_texts(
            (doc.source, doc.text, doc.doc_type) for doc in payload.documents
        )
        return review(chosen, extraction)

    @app.post("/wizard/preview", tags=["wizard"])
    def preview(payload: PreviewIn, repo: Repo) -> dict[str, Any]:
        """Live previews of the reference documents, problems found and derivable values."""
        chosen = load(repo, payload.templates)
        values, _ = docfill.collect_values(None, payload.values)
        return {
            "previews": [build_preview(template, values) for template in chosen],
            "suggested_filename": suggest_filename(chosen, values).removesuffix(".pdf"),
            **assist(values),
        }

    @app.post("/wizard/export", tags=["wizard"])
    def export(payload: ExportIn, repo: Repo) -> dict[str, Any]:
        """Create every PDF, save them under the chosen name and learn from the review."""
        chosen = load(repo, payload.templates)
        results = []
        for template in chosen:  # fail before saving anything if a document is incomplete
            results.append(docfill.fill(template, None, payload.values, payload.allow_missing))
        values, _ = docfill.collect_values(None, payload.values)
        base = safe_filename(payload.filename or suggest_filename(chosen, values))
        base = base.removesuffix(".pdf")
        files = []
        for template, result in zip(chosen, results, strict=True):
            name = base if len(chosen) == 1 else f"{base}_{template.name}"
            path = save_output(settings.output_dir, name, result.pdf)
            files.append(
                {
                    "template": template.name,
                    "title": template.title,
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
        return {"files": files, "learned": learned.as_dict()}

    @app.get("/wizard/files/{filename}", tags=["wizard"])
    def download(filename: str) -> FileResponse:
        """Download a PDF saved by the wizard."""
        if filename != safe_filename(filename) or not re.fullmatch(r"[\w.-]+\.pdf", filename):
            raise HTTPException(404, "not found")
        path = settings.output_dir / filename
        if not path.is_file():
            raise HTTPException(404, "not found")
        return FileResponse(path, media_type="application/pdf", filename=filename)


_ = MissingFieldsError  # raised by docfill.fill, turned into 422 by the app's handler
