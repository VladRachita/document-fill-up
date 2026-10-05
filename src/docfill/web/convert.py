"""Convert documents: the ``/convert`` page and its API, apart from the wizard.

A Word document (.docx, .doc) becomes a PDF, a PDF becomes a Word document (.docx). Every
conversion is scored against the original and, when it is given, against the real document
(the PDF Word saves, the Word document the PDF was made from): see :mod:`docfill.convert`.
"""

# No ``from __future__ import annotations``: FastAPI resolves the annotations below.

from functools import cache
from importlib import resources
from pathlib import PurePosixPath
from typing import Annotated, Any

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import FileResponse, HTMLResponse

from docfill.app import App
from docfill.convert import PDF, WORD, compare_files, convert_and_score, engines_for
from docfill.web import read_upload, saved_file
from docfill.wizard import save_output

CONVERTED = "converted"  # the folder, in the output folder, of the converted documents
MAX_PREVIEWS = 30


@cache
def convert_page() -> str:
    return (resources.files("docfill.web") / "convert.html").read_text(encoding="utf-8")


def _given(upload: UploadFile | None) -> bool:
    """A file was chosen (a form without one may still send an empty part)."""
    return upload is not None and bool(upload.filename)


def register_convert(app: FastAPI, context: App) -> None:
    settings = context.settings
    folder = settings.output_dir / CONVERTED
    tags = ["convert"]

    @app.get("/convert", response_class=HTMLResponse, include_in_schema=False)
    def page() -> HTMLResponse:
        return HTMLResponse(convert_page())

    @app.get("/convert/engines", tags=tags)
    def list_engines() -> dict[str, list[dict[str, Any]]]:
        """The engines of each direction (``pdf``: Word to PDF, ``docx``: PDF to Word), best
        first, each with what it needs when it is not installed."""
        return {
            target: [engine.as_dict() for engine in engines_for(target)] for target in (PDF, WORD)
        }

    @app.post("/convert", tags=tags)
    def convert(
        file: Annotated[UploadFile, File(description="A Word (.docx, .doc) or PDF document.")],
        to: Annotated[
            str | None, Form(description="pdf or docx; by default Word becomes PDF and back.")
        ] = None,
        engine: Annotated[str, Form(description="auto: the best engine installed.")] = "auto",
        reference: Annotated[
            UploadFile | None,
            File(
                description="Optional: the real document to compare with, of the same kind as "
                "the converted one (the PDF Word saves; the Word document the PDF was made "
                "from)."
            ),
        ] = None,
        filename: Annotated[str | None, Form(description="Name of the converted file.")] = None,
        previews: Annotated[
            int, Form(ge=0, le=MAX_PREVIEWS, description="Pages given as pictures.")
        ] = 12,
        marks: Annotated[
            bool | None,
            Form(
                description="A scan read with OCR: keep its stamps, signatures and handwriting "
                "as pictures where they are on the page (false: a clean copy). By default, "
                "the server's setting (DOCFILL_OCR_KEEP_MARKS)."
            ),
        ] = None,
    ) -> dict[str, Any]:
        """Convert a document, save it in the output folder and score how faithful it is:
        ``fidelity`` against the original, ``reference`` against the real document."""
        name, data = read_upload(file, settings.max_file_size)
        real = read_upload(reference, settings.max_file_size) if _given(reference) else None
        if marks is not None:  # this time only
            chosen = settings.model_copy(update={"ocr_keep_marks": marks})
        else:
            chosen = settings
        conversion = convert_and_score(data, name, to or None, engine, real, previews, chosen)
        stem = filename or PurePosixPath(name.replace("\\", "/")).stem
        path = save_output(folder, stem, conversion.data, conversion.suffix)
        return {
            **conversion.as_dict(),
            "filename": path.name,
            "saved_to": str(path),
            "url": f"/convert/files/{path.name}",
        }

    @app.post("/convert/compare", tags=tags)
    def compare(
        real: Annotated[UploadFile, File(description="The real document (Word or PDF).")],
        converted: Annotated[UploadFile, File(description="The converted document to score.")],
        previews: Annotated[
            int, Form(ge=0, le=MAX_PREVIEWS, description="Pages given as pictures.")
        ] = 12,
    ) -> dict[str, Any]:
        """Score a document converted by any program against the real one, with the same
        checks as a conversion."""
        expected = read_upload(real, settings.max_file_size)
        actual = read_upload(converted, settings.max_file_size)
        return compare_files(expected, actual, previews, settings).as_dict()

    @app.get("/convert/files/{filename}", tags=tags)
    def download(filename: str) -> FileResponse:
        """Download a converted document."""
        return saved_file(folder, filename)
