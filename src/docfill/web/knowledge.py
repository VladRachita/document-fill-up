"""Knowledge base routes: the ``/knowledge`` page and its REST API.

Feed docfill what it needs to know about Romanian trade register procedures (legal forms,
procedures, rules, document types, laws), verify it, and see what the wizard taught it.
"""

# No ``from __future__ import annotations``: FastAPI resolves the annotations below.

from functools import cache
from importlib import resources
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from docfill.app import App
from docfill.knowledge import KINDS, dump_yaml, load_text
from docfill.knowledge.ingest import extract_text
from docfill.knowledge.loader import parse_knowledge
from docfill.readers import read_bytes

MAX_YAML_CHARS = 500_000


class KnowledgeIn(BaseModel):
    """Entries to add or correct: YAML text (``yaml``) or the same structure as JSON
    (``knowledge``: ``{"rules": [...], "procedures": [...]}``)."""

    yaml: str | None = Field(default=None, max_length=MAX_YAML_CHARS)
    knowledge: dict[str, Any] | None = None
    verified_by: str | None = Field(default=None, max_length=100)
    actor: str | None = Field(default=None, max_length=100)
    note: str = Field(default="", max_length=1000)


class StatusIn(BaseModel):
    by: str = Field(min_length=1, max_length=100)
    note: str = Field(default="", max_length=1000)


class CheckIn(BaseModel):
    procedure: str = Field(max_length=100)
    values: dict[str, str] = Field(default_factory=dict, max_length=300)


@cache
def knowledge_page() -> str:
    return (resources.files("docfill.web") / "knowledge.html").read_text(encoding="utf-8")


def _kind(kind: str) -> str:
    if kind not in KINDS:
        raise HTTPException(404, f"unknown kind '{kind}'")
    return kind


def register_knowledge(app: FastAPI, context: App) -> None:
    knowledge, settings, docfill = context.knowledge, context.settings, context.docfill
    tags = ["knowledge"]

    @app.get("/knowledge", response_class=HTMLResponse, include_in_schema=False)
    def page() -> HTMLResponse:
        return HTMLResponse(knowledge_page())

    @app.get("/knowledge/cases", tags=tags)
    def cases() -> dict[str, Any]:
        """Legal forms, operations and procedures, for choosing what is being done."""
        return knowledge.cases()

    @app.get("/knowledge/procedures/{key}", tags=tags)
    def procedure(key: str) -> dict[str, Any]:
        """A procedure with its forms, supporting documents, rules and legal basis."""
        return knowledge.procedure_view(key)

    @app.post("/knowledge/check", tags=tags)
    def check(payload: CheckIn) -> list[dict[str, Any]]:
        """Run a procedure's legal checks on values."""
        values, _ = docfill.collect_values(None, payload.values)
        return [result.as_dict() for result in knowledge.evaluate(payload.procedure, values)]

    @app.get("/knowledge/entries", tags=tags)
    def entries(kind: str | None = None, include_retired: bool = False) -> list[dict[str, Any]]:
        return [
            entry.summary()
            for entry in knowledge.entries(_kind(kind) if kind else None, include_retired)
        ]

    @app.get("/knowledge/entries/{kind}/{key}", tags=tags)
    def entry(kind: str, key: str) -> dict[str, Any]:
        found = knowledge.get(_kind(kind), key)
        return {
            **found.as_dict(),
            "yaml": dump_yaml([found.spec]),
            "history": knowledge.history(kind, key),
        }

    @app.post("/knowledge/entries", tags=tags)
    def add(payload: KnowledgeIn) -> dict[str, Any]:
        """Add or correct entries. A changed entry gets a new version and goes back to
        ``draft`` unless ``verified_by`` names who checked it."""
        if payload.yaml:
            specs = load_text(payload.yaml, "request")
        else:
            specs = parse_knowledge(payload.knowledge or {}, "request")
        if not specs:
            raise HTTPException(422, "no knowledge entries in the request")
        _, warnings = knowledge.check_references(specs)
        results = knowledge.save_all(
            specs,
            origin="local",
            actor=payload.actor,
            verified_by=payload.verified_by,
            note=payload.note,
        )
        return {"results": [r.as_dict() for r in results], "warnings": warnings}

    @app.post("/knowledge/entries/{kind}/{key}/verify", tags=tags)
    def verify(kind: str, key: str, payload: StatusIn) -> dict[str, Any]:
        """A person confirms the entry is correct and current."""
        return knowledge.verify(_kind(kind), key, payload.by, payload.note).summary()

    @app.post("/knowledge/entries/{kind}/{key}/retire", tags=tags)
    def retire(kind: str, key: str, payload: StatusIn) -> dict[str, Any]:
        """The entry no longer applies; it is kept in the history."""
        return knowledge.retire(_kind(kind), key, payload.by, payload.note).summary()

    @app.get("/knowledge/export", tags=tags, response_class=PlainTextResponse)
    def export() -> PlainTextResponse:
        """Every active entry as YAML (edit it and add it back, or keep it under version
        control)."""
        text = dump_yaml([e.spec for e in knowledge.entries()])
        return PlainTextResponse(text, media_type="application/yaml")

    @app.post("/knowledge/seed", tags=tags)
    def seed() -> list[dict[str, Any]]:
        """Load the knowledge bundled with docfill (entries changed here are kept)."""
        return [r.as_dict() for r in knowledge.seed()]

    @app.get("/knowledge/texts", tags=tags)
    def texts() -> list[dict[str, Any]]:
        """Legal texts fed to docfill."""
        return knowledge.sources()

    @app.post("/knowledge/texts", tags=tags)
    def add_text(
        file: Annotated[UploadFile, File(description="TXT, HTML, PDF or DOCX of the act")],
        citation: Annotated[str, Form(description="e.g. Legea nr. 31/1990")],
        title: Annotated[str, Form()] = "",
        url: Annotated[str | None, Form()] = None,
    ) -> dict[str, Any]:
        """Feed a law (or an ONRC guide): it is split into articles, searchable, and shown
        next to the rules that cite it. The same citation replaces the previous text."""
        data = file.file.read(settings.max_file_size + 1)
        name = file.filename or "text"
        text = extract_text(data, name, settings)
        return knowledge.ingest_text(text, citation, title, name, url or None)

    @app.delete("/knowledge/texts", tags=tags, status_code=204)
    def remove_text(citation: Annotated[str, Query()]) -> None:
        knowledge.remove_source(citation)

    @app.get("/knowledge/search", tags=tags)
    def search(
        q: Annotated[str, Query(min_length=2, max_length=300)],
        limit: Annotated[int, Query(ge=1, le=30)] = 8,
    ) -> list[dict[str, Any]]:
        """Search the fed laws and the knowledge entries."""
        return knowledge.search(q, limit)

    @app.post("/knowledge/doctypes/{name}/examples", tags=tags)
    def teach(name: str, files: Annotated[list[UploadFile], File()]) -> dict[str, Any]:
        """Teach the document classifier with example documents of a type (sensitive numbers
        are removed; prefer blank or sample documents)."""
        if name not in docfill.classifier.types():
            raise HTTPException(404, f"unknown document type '{name}'")
        texts = []
        for upload in files:
            data = upload.file.read(settings.max_file_size + 1)
            raw = read_bytes(data, upload.filename or "example", settings)
            texts.append(docfill.sanitizer.sanitize(raw).text)
        added = context.learning.add_doc_examples(name, texts)
        return {"doc_type": name, "examples_added": added}

    @app.get("/knowledge/stats", tags=tags)
    def stats() -> dict[str, Any]:
        """Knowledge status, fed laws and what the saved files taught (rules to review,
        documents to add to procedures)."""
        return knowledge.stats()
