"""PDF reader: embedded text first, OCR for scanned pages."""

from __future__ import annotations

import io
import logging

import pypdfium2 as pdfium
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from docfill.config import Settings
from docfill.errors import DocumentReadError, OCRUnavailableError
from docfill.export.pdf_form import inspect_form, read_form_values
from docfill.models import DocumentType, Page, RawDocument
from docfill.readers.ocr import ocr_image

logger = logging.getLogger(__name__)


def read_pdf(data: bytes, source: str, settings: Settings) -> RawDocument:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocumentReadError(f"{source}: PDF is password protected")
        texts = [page.extract_text() or "" for page in reader.pages]
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        raise DocumentReadError(f"{source}: invalid PDF ({exc})") from exc

    pages: list[Page] = []
    warnings: list[str] = []
    rasterized: pdfium.PdfDocument | None = None
    try:
        for index, text in enumerate(texts):
            if len(text.strip()) >= settings.pdf_min_text_chars:
                pages.append(Page(number=index + 1, text=text))
                continue
            # Little or no embedded text: most likely a scanned page.
            try:
                if rasterized is None:
                    rasterized = pdfium.PdfDocument(data)
                image = rasterized[index].render(scale=settings.ocr_dpi / 72).to_pil()
                ocr_text = ocr_image(image, settings.ocr_languages, settings.tesseract_cmd)
                pages.append(Page(number=index + 1, text=ocr_text, ocr=True))
            except pdfium.PdfiumError as exc:
                raise DocumentReadError(
                    f"{source}: cannot render page {index + 1} ({exc})"
                ) from exc
            except OCRUnavailableError as exc:
                logger.warning("%s page %d: %s", source, index + 1, exc)
                warnings.append(f"Page {index + 1} looks scanned but OCR is unavailable: {exc}")
                pages.append(Page(number=index + 1, text=text))
    finally:
        if rasterized is not None:
            rasterized.close()

    form_values = read_form_values(data)
    if form_values:
        # Filled PDF forms keep their data in fields, not in the page text: expose each value
        # with the label printed next to it so it is visible and can be extracted.
        lines = []
        for field in inspect_form(data):
            value = form_values.get(field.name)
            if value and field.kind == "text":
                lines.append(f"{field.label or field.name}: {value}")
        pages.append(Page(number=len(pages) + 1, text="\n".join(lines)))
    return RawDocument(
        source=source,
        doc_type=DocumentType.PDF,
        pages=pages,
        warnings=warnings,
        form_values=form_values,
    )
