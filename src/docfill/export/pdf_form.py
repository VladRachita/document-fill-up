"""Fill the text fields of an existing PDF form (AcroForm) standard document."""

from __future__ import annotations

import io
from collections.abc import Mapping

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError

from docfill.errors import TemplateError


def list_text_fields(pdf_data: bytes) -> list[str]:
    try:
        reader = PdfReader(io.BytesIO(pdf_data))
        fields = reader.get_fields() or {}
    except PdfReadError as exc:
        raise TemplateError(f"Invalid PDF form: {exc}") from exc
    return [name for name, field in fields.items() if field.get("/FT") == "/Tx"]


def fill_pdf_form(
    pdf_data: bytes,
    values: Mapping[str, str],
    metadata: Mapping[str, str] | None = None,
) -> bytes:
    """Write ``values`` (PDF field name -> text) into the form; the layout is untouched."""
    writer = PdfWriter(clone_from=PdfReader(io.BytesIO(pdf_data)))
    for page in writer.pages:
        if page.get("/Annots"):
            writer.update_page_form_field_values(page, dict(values), auto_regenerate=False)
    writer.set_need_appearances_writer(True)
    if metadata:
        writer.add_metadata(dict(metadata))
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()
