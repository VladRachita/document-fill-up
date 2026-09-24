"""REST API. Run with ``docfill serve`` and open http://127.0.0.1:8000/docs.

The API has no authentication: keep it on a private network or put it behind a gateway.
"""

# No ``from __future__ import annotations`` here: FastAPI must be able to resolve the
# dependency aliases declared inside ``create_app``.

import base64
import binascii
import json
from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field, ValidationError

from docfill import __version__
from docfill.app import build_app
from docfill.config import Settings, get_settings
from docfill.errors import (
    DocFillError,
    DocumentReadError,
    MissingFieldsError,
    OCRUnavailableError,
    TemplateIntegrityError,
    TemplateNotFoundError,
    UnsupportedDocumentError,
)
from docfill.extraction import FIELDS
from docfill.pipeline import DocumentAnalysis
from docfill.readers.ocr import tesseract_available
from docfill.templates import StandardDocumentSpec, TemplateRepository
from docfill.web import read_upload, register_wizard

_ERROR_STATUS: list[tuple[type[DocFillError], int]] = [
    (TemplateNotFoundError, 404),
    (MissingFieldsError, 422),
    (UnsupportedDocumentError, 415),
    (TemplateIntegrityError, 409),
    (OCRUnavailableError, 503),
    (DocumentReadError, 400),
]


class TemplateIn(BaseModel):
    name: str
    title: str
    description: str = ""
    kind: str = "text"
    body: str | None = None
    pdf_base64: str | None = Field(default=None, description="PDF form, base64 encoded.")
    field_map: dict[str, str] = Field(default_factory=dict)
    optional_fields: list[str] = Field(default_factory=list)


def _analysis_json(analysis: DocumentAnalysis) -> dict[str, Any]:
    return {
        "source": analysis.raw.source,
        "type": analysis.raw.doc_type.value,
        "pages": len(analysis.raw.pages),
        "ocr": analysis.raw.used_ocr,
        "warnings": analysis.raw.warnings,
        "redactions": analysis.sanitized.redactions,
    }


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    context = build_app(settings)
    session_factory = context.sessions
    docfill = context.docfill

    app = FastAPI(
        title="docfill",
        version=__version__,
        description="Scan documents, extract personal data and fill standard documents as PDF.",
    )

    def repository() -> Iterator[TemplateRepository]:
        session = session_factory()
        try:
            yield TemplateRepository(session)
        finally:
            session.close()

    Repo = Annotated[TemplateRepository, Depends(repository)]

    @app.exception_handler(DocFillError)
    def handle_docfill_error(_: Request, exc: DocFillError) -> JSONResponse:
        code = next(
            (code for kind, code in _ERROR_STATUS if isinstance(exc, kind)),
            400,
        )
        content: dict[str, Any] = {"detail": str(exc)}
        if isinstance(exc, MissingFieldsError):
            content["missing"] = exc.missing
        return JSONResponse(status_code=code, content=content)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "ocr": tesseract_available(settings.tesseract_cmd),
            "ner": docfill.extractor.ner_available,
        }

    @app.get("/fields")
    def fields() -> list[dict[str, Any]]:
        """Fields docfill can extract, usable as ``{{ name }}`` in standard documents."""
        return [
            {"name": spec.name, "label": spec.label, "synonyms": list(spec.synonyms)}
            for spec in FIELDS.values()
        ] + [{"name": "today", "label": "Current date (ISO)", "synonyms": []}]

    @app.get("/templates")
    def list_templates(repo: Repo) -> list[dict[str, Any]]:
        return [doc.summary() for doc in repo.list()]

    @app.get("/templates/{name}")
    def get_template(name: str, repo: Repo) -> dict[str, Any]:
        doc = repo.get(name)
        return {**doc.summary(), "body": doc.body, "field_map": doc.field_map}

    @app.post("/templates", status_code=status.HTTP_201_CREATED)
    def save_template(payload: TemplateIn, repo: Repo) -> dict[str, Any]:
        data = payload.model_dump(exclude={"pdf_base64"})
        if payload.pdf_base64:
            try:
                data["pdf_data"] = base64.b64decode(payload.pdf_base64, validate=True)
            except binascii.Error as exc:
                raise HTTPException(422, "pdf_base64 is not valid base64") from exc
        try:
            spec = StandardDocumentSpec.model_validate(data)
        except ValidationError as exc:
            raise HTTPException(422, json.loads(exc.json(include_url=False))) from exc
        doc, changed = repo.save(spec)
        return {**doc.summary(), "changed": changed}

    @app.delete("/templates/{name}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_template(name: str, repo: Repo) -> Response:
        repo.delete(name)
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post("/extract")
    def extract(files: Annotated[list[UploadFile], File()]) -> dict[str, Any]:
        """Scan and sanitize the uploaded documents and return the extracted fields."""
        uploads = [read_upload(upload, settings.max_file_size) for upload in files]
        analyses = [docfill.analyze_bytes(data, name) for name, data in uploads]
        combined = docfill.combine(analyses)
        return {
            "documents": [_analysis_json(analysis) for analysis in analyses],
            "fields": {name: field.model_dump() for name, field in combined.fields.items()},
        }

    @app.post("/fill", response_class=Response)
    def fill(
        repo: Repo,
        template: Annotated[str, Form(description="Standard document name.")],
        files: Annotated[list[UploadFile] | None, File()] = None,
        values: Annotated[
            str, Form(description='JSON object of values to set, e.g. {"city": "Paris"}')
        ] = "{}",
        allow_missing: Annotated[bool, Form()] = False,
    ) -> Response:
        """Fill a standard document from the uploaded documents and return the PDF."""
        try:
            overrides = json.loads(values or "{}")
        except json.JSONDecodeError as exc:
            raise HTTPException(422, "values must be a JSON object") from exc
        if not isinstance(overrides, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in overrides.items()
        ):
            raise HTTPException(422, "values must be a JSON object of strings")
        standard = repo.get(template)
        uploads = [read_upload(upload, settings.max_file_size) for upload in files or []]
        result, _ = docfill.process(uploads, standard, overrides, allow_missing)
        return Response(
            content=result.pdf,
            media_type="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="{standard.name}.pdf"',
                "X-Docfill-Template-Version": str(result.template_version),
                "X-Docfill-Missing": ",".join(result.missing),
            },
        )

    @app.get("/learning/stats", tags=["learning"])
    def learning_stats() -> dict[str, Any]:
        """What docfill has learned from reviewed documents, and how accurate it is."""
        return context.learning.stats()

    register_wizard(app, context, repository)
    return app
