"""Optical character recognition with Tesseract.

Scans and photos differ a lot (clean printouts, identity cards on a guilloche background, old
certificates with a coat of arms behind the text), so several preprocessing variants are tried
and the one Tesseract is most confident about wins.
"""

from __future__ import annotations

from collections import defaultdict
from functools import lru_cache

import pytesseract
from PIL import Image, ImageFilter, ImageOps

from docfill.errors import OCRUnavailableError

# Tesseract works best when text is roughly 30px high; small scans are upscaled.
_MIN_LONG_SIDE = 1800
# A first pass this confident (mean word confidence) needs no other variant.
_GOOD_ENOUGH = 85.0
_PREFERRED_LANGUAGES = ("ron", "eng")


@lru_cache
def tesseract_available(tesseract_cmd: str | None = None) -> bool:
    if tesseract_cmd:
        pytesseract.pytesseract.tesseract_cmd = tesseract_cmd
    try:
        pytesseract.get_tesseract_version()
    except (pytesseract.TesseractNotFoundError, OSError):
        return False
    return True


@lru_cache
def resolve_languages(languages: str) -> str:
    """``auto`` -> the installed ones of Romanian and English (``ron+eng``)."""
    if languages != "auto":
        return languages
    try:
        installed = set(pytesseract.get_languages(config=""))
    except (pytesseract.TesseractError, OSError):
        return "eng"
    chosen = [lang for lang in _PREFERRED_LANGUAGES if lang in installed]
    return "+".join(chosen) or "eng"


def _upscale(image: Image.Image) -> Image.Image:
    long_side = max(image.size)
    if 0 < long_side < _MIN_LONG_SIDE:
        factor = _MIN_LONG_SIDE / long_side
        image = image.resize(
            (round(image.width * factor), round(image.height * factor)),
            Image.Resampling.LANCZOS,
        )
    return image


def _flatten(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image) or image
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        background = Image.new("RGBA", image.size, "white")
        image = Image.alpha_composite(background, image)
    return _upscale(image.convert("L"))


def preprocess(image: Image.Image) -> Image.Image:
    """Orient, grayscale, upscale and stretch contrast to improve recognition."""
    # Plain min/max stretch: a percentile cutoff would treat the (<1%) text pixels of a mostly
    # white page as outliers and smear JPEG halos into black blobs.
    return ImageOps.autocontrast(_flatten(image))


def _dark_ink(gray: Image.Image, threshold: int) -> Image.Image:
    """Keep only dark ink: drops coloured backgrounds, stamps and guilloche patterns."""
    return gray.point(lambda p: 0 if p < threshold else 255).filter(ImageFilter.MedianFilter(3))


def _run(image: Image.Image, languages: str, psm: int) -> tuple[str, float, int]:
    """OCR once; return (text, mean word confidence, confident characters)."""
    data = pytesseract.image_to_data(
        image, lang=languages, config=f"--psm {psm}", output_type=pytesseract.Output.DICT
    )
    lines: dict[tuple[int, int, int], list[str]] = defaultdict(list)
    confidences: list[float] = []
    confident_chars = 0
    for index, word in enumerate(data["text"]):
        word = word.strip()
        if not word:
            continue
        key = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
        lines[key].append(word)
        confidence = float(data["conf"][index])
        if confidence >= 0:
            confidences.append(confidence)
        if confidence >= 60 and len(word) >= 2:
            confident_chars += len(word)
    text_lines: list[str] = []
    previous: tuple[int, int] | None = None
    for (block, paragraph, _), words in lines.items():
        if previous is not None and previous != (block, paragraph):
            text_lines.append("")
        text_lines.append(" ".join(words))
        previous = (block, paragraph)
    mean = sum(confidences) / len(confidences) if confidences else 0.0
    return "\n".join(text_lines), mean, confident_chars


def ocr_image(image: Image.Image, languages: str = "auto", tesseract_cmd: str | None = None) -> str:
    if not tesseract_available(tesseract_cmd):
        raise OCRUnavailableError(
            "Tesseract OCR is not installed. Install it (e.g. `apt install tesseract-ocr`) "
            "or set DOCFILL_TESSERACT_CMD to its path."
        )
    languages = resolve_languages(languages)
    gray = _flatten(image)
    stretched = ImageOps.autocontrast(gray)
    text, mean, best_score = _run(stretched, languages, 3)
    if mean >= _GOOD_ENOUGH:
        return text
    best_text = text
    for variant in (_dark_ink(gray, 130), _dark_ink(stretched, 135)):
        candidate, _, score = _run(variant, languages, 4)
        if score > best_score:
            best_text, best_score = candidate, score
    return best_text
