"""Optical character recognition with Tesseract."""

from __future__ import annotations

from functools import lru_cache

import pytesseract
from PIL import Image, ImageOps

from docfill.errors import OCRUnavailableError

# Tesseract works best when text is roughly 30px high; small scans are upscaled.
_MIN_LONG_SIDE = 1800


@lru_cache
def tesseract_available(tesseract_cmd: str | None = None) -> bool:
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    try:
        pytesseract.get_tesseract_version()
    except (pytesseract.TesseractNotFoundError, OSError):
        return False
    return True


def preprocess(image: Image.Image) -> Image.Image:
    """Orient, grayscale, upscale and stretch contrast to improve recognition."""
    image = ImageOps.exif_transpose(image) or image
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        background = Image.new("RGBA", image.size, "white")
        image = Image.alpha_composite(background, image)
    image = image.convert("L")
    long_side = max(image.size)
    if 0 < long_side < _MIN_LONG_SIDE:
        factor = _MIN_LONG_SIDE / long_side
        image = image.resize(
            (round(image.width * factor), round(image.height * factor)),
            Image.Resampling.LANCZOS,
        )
    # Plain min/max stretch: a percentile cutoff would treat the (<1%) text pixels of a mostly
    # white page as outliers and smear JPEG halos into black blobs.
    return ImageOps.autocontrast(image)


def ocr_image(image: Image.Image, languages: str = "eng", tesseract_cmd: str | None = None) -> str:
    if not tesseract_available(tesseract_cmd):
        raise OCRUnavailableError(
            "Tesseract OCR is not installed. Install it (e.g. `apt install tesseract-ocr`) "
            "or set DOCFILL_TESSERACT_CMD to its path."
        )
    return pytesseract.image_to_string(preprocess(image), lang=languages, config="--psm 3")
