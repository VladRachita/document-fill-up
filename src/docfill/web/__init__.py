"""Web wizard: a step-by-step page to scan documents, correct what was found in real time and
save the filled reference document under a new file name.

Routes are registered on the main FastAPI app by :func:`register_wizard`.
"""

# No ``from __future__ import annotations``: FastAPI resolves the local ``Repo`` alias below.

from collections.abc import Callable, Iterator
from functools import cache
from importlib import resources
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, Field

from docfill.config import Settings
from docfill.models import ExtractionResult
from docfill.pipeline import DocFill
from docfill.templates import TemplateRepository
from docfill.wizard import build_preview, field_rows, other_fields, save_output, suggest_filename

MAX_DOCUMENT_CHARS = 500_000


class DocumentText(BaseModel):
    source: str = Field(max_length=255)
    text: str = Field(max_length=MAX_DOCUMENT_CHARS)


class ReextractIn(BaseModel):
    template: str
    documents: list[DocumentText] = Field(default_factory=list, max_length=20)


class PreviewIn(BaseModel):
    template: str
    values: dict[str, str] = Field(default_factory=dict, max_length=200)


class ExportIn(PreviewIn):
    filename: str | None = Field(default=None, max_length=200)
    allow_missing: bool = False


def read_upload(upload: UploadFile, limit: int) -> tuple[str, bytes]:
    data = upload.file.read(limit + 1)
    name = upload.filename or "upload"
    if len(data) > limit:
        raise HTTPException(413, f"{name} is too large")
    return name, data


@cache
def wizard_page() -> str:
    return (resources.files("docfill.web") / "wizard.html").read_text(encoding="utf-8")


def register_wizard(
    app: FastAPI,
    settings: Settings,
    docfill: DocFill,
    repository: Callable[[], Iterator[TemplateRepository]],
) -> None:
    Repo = Annotated[TemplateRepository, Depends(repository)]

    def review(
        template_name: str, repo: TemplateRepository, extraction: ExtractionResult | None
    ) -> dict[str, Any]:
        template = repo.get(template_name)
        rows = field_rows(template, extraction, settings.min_confidence)
        return {
            "template": template.summary(),
            "rows": [row.as_dict() for row in rows],
            "other_fields": other_fields(template, extraction) if extraction else [],
            "min_confidence": settings.min_confidence,
        }

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/wizard")

    @app.get("/wizard", response_class=HTMLResponse, include_in_schema=False)
    def wizard() -> HTMLResponse:
        return HTMLResponse(wizard_page())

    @app.post("/wizard/analyze", tags=["wizard"])
    def analyze(
        repo: Repo,
        template: Annotated[str, Form()],
        files: Annotated[list[UploadFile], File()],
    ) -> dict[str, Any]:
        """Step 2: read + sanitize each file, extract fields for the chosen reference document."""
        repo.get(template)  # fail fast on an unknown reference document
        uploads = [read_upload(upload, settings.max_file_size) for upload in files]
        analyses = [docfill.analyze_bytes(data, name) for name, data in uploads]
        extraction = docfill.combine(analyses)
        documents = [
            {
                "source": a.raw.source,
                "type": a.raw.doc_type.value,
                "pages": len(a.raw.pages),
                "ocr": a.raw.used_ocr,
                "warnings": a.raw.warnings,
                "redactions": a.sanitized.redactions,
                "removed_lines": a.sanitized.removed_lines,
                "raw_text": a.raw.text,
                "text": a.sanitized.text,
            }
            for a in analyses
        ]
        return {"documents": documents, **review(template, repo, extraction)}

    @app.post("/wizard/reextract", tags=["wizard"])
    def reextract(payload: ReextractIn, repo: Repo) -> dict[str, Any]:
        """Re-run extraction on corrected text (called while the user edits it)."""
        extraction = docfill.extract_texts((doc.source, doc.text) for doc in payload.documents)
        return review(payload.template, repo, extraction if payload.documents else None)

    @app.post("/wizard/preview", tags=["wizard"])
    def preview(payload: PreviewIn, repo: Repo) -> dict[str, Any]:
        """Live preview of the reference document filled with the current values."""
        template = repo.get(payload.template)
        values, _ = docfill.collect_values(None, payload.values)
        return {
            **build_preview(template, values),
            "suggested_filename": suggest_filename(template, values),
        }

    @app.post("/wizard/export", tags=["wizard"])
    def export(payload: ExportIn, repo: Repo) -> Response:
        """Create the PDF, save it in the output folder under the chosen name and return it."""
        template = repo.get(payload.template)
        result = docfill.fill(template, None, payload.values, payload.allow_missing)
        values, _ = docfill.collect_values(None, payload.values)
        filename = payload.filename or suggest_filename(template, values)
        path = save_output(settings.output_dir, filename, result.pdf)
        return Response(
            content=result.pdf,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{path.name}"',
                "X-Docfill-Filename": path.name,
                "X-Docfill-Saved-To": str(path),
                "X-Docfill-Missing": ",".join(result.missing),
            },
        )
