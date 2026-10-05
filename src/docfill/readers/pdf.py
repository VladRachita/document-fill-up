"""PDF reader: embedded text first, OCR for scanned pages (also those a scanner app laid its
own text over)."""

from __future__ import annotations

import io
import logging

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from docfill.config import Settings
from docfill.errors import DocumentReadError, OCRUnavailableError
from docfill.export.pdf_form import inspect_form, read_form_values
from docfill.models import DocumentType, Page, RawDocument
from docfill.readers.ocr import ocr_image

logger = logging.getLogger(__name__)

# pdfium's text replaces pypdf's when it splits the same characters into this many more words.
_MORE_WORDS = 1.25


def _spaced(texts: list[str], data: bytes) -> list[str]:
    """pypdf glues the words of some PDFs together ("Str.Exemplelornr.7Bl.B3" in the export of
    an electronic identity card): where pdfium reads the same characters as clearly more words,
    its text is used."""
    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError:
        return texts
    try:
        spaced = []
        for index, text in enumerate(texts):
            other = document[index].get_textpage().get_text_bounded()
            other = other.replace("\r\n", "\n").replace("\r", "\n")
            same = "".join(text.split()) == "".join(other.split())
            more = len(other.split()) >= _MORE_WORDS * len(text.split())
            spaced.append(other if text.strip() and same and more else text)
        return spaced
    except pdfium.PdfiumError:
        return texts
    finally:
        document.close()


# A scanner app (Adobe Scan, CamScanner, a copier) lays its own OCR text, invisible, over the
# picture of the page. That text often lacks the Romanian letters ("Braqov", "Bra~ov" for
# Brașov, "judeftil" for județul): such pages are read again with Tesseract, which knows them.
_SCAN_COVER = 0.85  # share of the page the picture covers
_INVISIBLE_SHARE = 0.9  # share of the text drawn invisible
_OCR_MIN_SHARE = 0.6  # the scanner's text is kept when Tesseract reads much less of the page


def _open(data: bytes) -> pdfium.PdfDocument | None:
    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError:
        return None


def _hidden_text_layer(page: pdfium.PdfPage) -> bool:
    """A page that is a picture with invisible text over it: the OCR of a scanner app."""
    width, height = page.get_size()
    covered, texts, invisible = False, 0, 0
    for item in page.get_objects(max_depth=3):
        if item.type == pdfium_c.FPDF_PAGEOBJ_IMAGE:
            bounds = item.get_bounds() if hasattr(item, "get_bounds") else item.get_pos()
            left, bottom, right, top = bounds
            covered |= (right - left) * (top - bottom) >= _SCAN_COVER * width * height
        elif item.type == pdfium_c.FPDF_PAGEOBJ_TEXT:
            texts += 1
            mode = pdfium_c.FPDFTextObj_GetTextRenderMode(item.raw)
            invisible += mode == pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE
    return covered and texts > 0 and invisible >= _INVISIBLE_SHARE * texts


def _letters(text: str) -> int:
    return sum(char.isalnum() for char in text)


def read_pdf(data: bytes, source: str, settings: Settings) -> RawDocument:
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted and not reader.decrypt(""):
            raise DocumentReadError(f"{source}: PDF is password protected")
        texts = [page.extract_text() or "" for page in reader.pages]
    except (PdfReadError, ValueError, KeyError, TypeError) as exc:
        raise DocumentReadError(f"{source}: invalid PDF ({exc})") from exc
    texts = _spaced(texts, data)

    pages: list[Page] = []
    warnings: list[str] = []
    rasterized: pdfium.PdfDocument | None = None
    try:
        for index, text in enumerate(texts):
            scanner_text = False
            if len(text.strip()) >= settings.pdf_min_text_chars:
                if settings.pdf_ocr_text_layers:
                    if rasterized is None:
                        rasterized = _open(data)
                    scanner_text = rasterized is not None and _hidden_text_layer(rasterized[index])
                if not scanner_text:
                    pages.append(Page(number=index + 1, text=text))
                    continue
            # A scanned page: no embedded text, or only the text a scanner app put over it.
            try:
                if rasterized is None:
                    rasterized = pdfium.PdfDocument(data)
                image = rasterized[index].render(scale=settings.ocr_dpi / 72).to_pil()
                ocr_text = ocr_image(image, settings.ocr_languages, settings.tesseract_cmd)
                if scanner_text and _letters(ocr_text) < _OCR_MIN_SHARE * _letters(text):
                    pages.append(Page(number=index + 1, text=text))  # the picture read worse
                else:
                    pages.append(Page(number=index + 1, text=ocr_text, ocr=True))
            except pdfium.PdfiumError as exc:
                raise DocumentReadError(
                    f"{source}: cannot render page {index + 1} ({exc})"
                ) from exc
            except OCRUnavailableError as exc:
                logger.warning("%s page %d: %s", source, index + 1, exc)
                warnings.append(
                    f"Page {index + 1} is a scan whose own text layer is used, as OCR is "
                    f"unavailable: check the values read from it ({exc})"
                    if scanner_text
                    else f"Page {index + 1} looks scanned but OCR is unavailable: {exc}"
                )
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
