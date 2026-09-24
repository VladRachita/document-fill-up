"""Image reader (PNG / JPEG) through OCR."""

from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

from docfill.config import Settings
from docfill.errors import DocumentReadError
from docfill.models import DocumentType, Page, RawDocument
from docfill.readers.ocr import ocr_image


def read_image(data: bytes, source: str, settings: Settings) -> RawDocument:
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            text = ocr_image(image, settings.ocr_languages, settings.tesseract_cmd)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as exc:
        raise DocumentReadError(f"{source}: invalid image ({exc})") from exc
    return RawDocument(
        source=source,
        doc_type=DocumentType.IMAGE,
        pages=[Page(number=1, text=text, ocr=True)],
    )
