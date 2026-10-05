"""Convert documents: Word (.docx, .doc) to PDF and PDF to Word (.docx), and score how faithful
the converted document is, against the original and against the real document when it is given
(see :mod:`docfill.convert.fidelity`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from time import perf_counter
from typing import Any

from docfill.config import Settings, get_settings
from docfill.convert.engines import PDF, WORD, Engine, choose_engine, engines_for, ocr_missing
from docfill.convert.fidelity import (
    Fidelity,
    Rendition,
    compare,
    ocr_text,
    pdf_rendition,
    word_rendition,
)
from docfill.errors import ConversionError, DocumentReadError, UnsupportedDocumentError
from docfill.models import DocumentType
from docfill.readers import detect_type
from docfill.readers.pdf import scanned_pages

MEDIA_TYPES = {
    PDF: "application/pdf",
    WORD: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}
NAMES = {PDF: "PDF", WORD: "Word", "doc": "Word"}


@dataclass
class Conversion:
    source: str  # the file name
    source_kind: str  # docx, doc or pdf
    target: str  # PDF or WORD
    engine: str
    data: bytes
    seconds: float
    warnings: list[str] = field(default_factory=list)
    ocr: dict[str, Any] | None = None  # read with OCR: words, confidence, uncertain words
    fidelity: Fidelity | None = None  # against the original
    reference: Fidelity | None = None  # against the real document

    @property
    def suffix(self) -> str:
        return f".{self.target}"

    @property
    def media_type(self) -> str:
        return MEDIA_TYPES[self.target]

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_kind": self.source_kind,
            "target": self.target,
            "engine": self.engine,
            "size": len(self.data),
            "seconds": round(self.seconds, 2),
            "warnings": self.warnings,
            "ocr": self.ocr,
            "fidelity": self.fidelity.as_dict() if self.fidelity else None,
            "reference": self.reference.as_dict() if self.reference else None,
        }


def document_kind(data: bytes, filename: str, settings: Settings | None = None) -> str:
    """``docx``, ``doc`` or ``pdf``, from the content of the file; anything else is refused."""
    settings = settings or get_settings()
    if not data:
        raise DocumentReadError(f"{filename}: file is empty")
    if len(data) > settings.max_file_size:
        raise DocumentReadError(
            f"{filename}: file is larger than the {settings.max_file_size} bytes limit"
        )
    kind = detect_type(data, filename)
    if kind is DocumentType.IMAGE:
        raise UnsupportedDocumentError(
            f"{filename}: only Word (.docx, .doc) and PDF documents can be converted"
        )
    return kind.value


def target_of(kind: str) -> str:
    """Word becomes PDF, PDF becomes Word."""
    return WORD if kind == PDF else PDF


def rendition(data: bytes, kind: str, settings: Settings, layout: bool = True) -> Rendition:
    if kind == PDF:
        return pdf_rendition(data)
    return word_rendition(data, kind, settings.convert_timeout, layout)


def convert(
    data: bytes,
    filename: str,
    target: str | None = None,
    engine: str = "auto",
    settings: Settings | None = None,
) -> Conversion:
    """Convert a Word document to PDF or a PDF to Word (``target`` follows from the file when
    not given) with ``engine`` (``auto``: the best one installed)."""
    settings = settings or get_settings()
    kind = document_kind(data, filename, settings)
    target = target or target_of(kind)
    if target not in MEDIA_TYPES:
        raise ConversionError(f"cannot convert to '{target}': choose pdf or docx")
    if target != target_of(kind):
        raise ConversionError(f"{filename} is already a {NAMES[kind]} document")
    notes: list[str] = []
    if kind == PDF:
        engine, notes = _for_scans(data, engine, settings)
    chosen: Engine = choose_engine(target, engine)
    start = perf_counter()
    made = chosen.run(data, f".{kind}", settings)
    return Conversion(
        filename,
        kind,
        target,
        chosen.name,
        made.data,
        perf_counter() - start,
        notes + made.warnings,
        made.ocr,
    )


def _for_scans(data: bytes, engine: str, settings: Settings) -> tuple[str, list[str]]:
    """A scanned PDF has no text, only pictures of its pages: Auto reads it with OCR (when most
    of its pages are scans); another engine would make a Word document of pictures."""
    scanned = scanned_pages(data, settings)
    pages = [str(number) for number, scan in enumerate(scanned, 1) if scan]
    if not pages or engine == "ocr":
        return engine, []
    mostly = len(pages) * 2 >= len(scanned)
    ocr_ready = ocr_missing() is None
    if engine == "auto" and mostly and ocr_ready:
        return "ocr", []
    if mostly:
        how = "the OCR engine reads their text" if ocr_ready else f"OCR needs: {ocr_missing()}"
        return engine, [
            f"This PDF is a scan: without OCR the Word document shows its pages as pictures, "
            f"the words cannot be edited ({how})"
        ]
    return engine, [
        f"Page{'s' if len(pages) > 1 else ''} {', '.join(pages)} "
        f"{'are scans' if len(pages) > 1 else 'is a scan'}: kept as pictures (the OCR engine "
        "reads the text of every page)"
    ]


def convert_and_score(
    data: bytes,
    filename: str,
    target: str | None = None,
    engine: str = "auto",
    reference: tuple[str, bytes] | None = None,
    previews: int = 12,
    settings: Settings | None = None,
) -> Conversion:
    """Convert, then score the converted document against the original and, when it is
    given, against ``reference`` (file name, content): the real document, of the same kind as
    the converted one (the PDF Word saves; the Word document the PDF was made from)."""
    settings = settings or get_settings()
    reference_kind = document_kind(reference[1], reference[0], settings) if reference else None
    conversion = convert(data, filename, target, engine, settings)
    if reference and target_of(reference_kind) == conversion.target:
        raise ConversionError(
            f"{reference[0]}: the real document to compare with must be a "
            f"{NAMES[conversion.target]} document, like the converted one"
        )
    # A Word document is not laid out to be compared with the PDF LibreOffice made of it: it
    # would be compared with itself.
    original = rendition(data, conversion.source_kind, settings, layout=False)
    converted = rendition(conversion.data, conversion.target, settings)
    conversion.fidelity = compare(original, converted, "original", previews)
    if original.scan and conversion.ocr:  # its text is the reading itself
        report = conversion.fidelity
        report.checks = [
            ocr_text(original, conversion.ocr) if check.key == "text" else check
            for check in report.checks
        ]
        report.warnings = [w for w in report.warnings if w not in original.warnings]
    if reference and reference_kind:
        expected = rendition(reference[1], reference_kind, settings)
        expected.pages_source = "in the real document"
        conversion.reference = compare(expected, converted, "reference", previews)
    return conversion


def compare_files(
    expected: tuple[str, bytes],
    actual: tuple[str, bytes],
    previews: int = 12,
    settings: Settings | None = None,
) -> Fidelity:
    """Score a converted document (``actual``) against the real one (``expected``), each a
    Word or PDF document (file name, content), whatever made them."""
    settings = settings or get_settings()
    sides = []
    for name, data in (expected, actual):
        sides.append(rendition(data, document_kind(data, name, settings), settings))
    sides[0].pages_source = "in the real document"
    return compare(sides[0], sides[1], "reference", previews)


__all__ = [
    "MEDIA_TYPES",
    "PDF",
    "WORD",
    "Conversion",
    "Fidelity",
    "compare_files",
    "convert",
    "convert_and_score",
    "document_kind",
    "engines_for",
    "target_of",
]
