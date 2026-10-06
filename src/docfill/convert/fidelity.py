"""How faithful a converted document is: scores from 0 to 100.

A converted document is compared with the original and, when it is given, with the real
document (the PDF Word saves, the Word document a PDF was made from) on five points:

=======  ======  ==================================================================================
Check    Weight  What is measured
=======  ======  ==================================================================================
layout     50    the pages drawn side by side: the share of the ink (text, lines, pictures) found at
                 the same place in both, within 2 points (0.7 mm); a missing or extra page counts 0
text       30    the words, in their order (the longest common subsequence of the two texts)
pages       8    the number of pages
fonts       8    the fonts of the text: kept, or replaced by a font of the same widths (the lines
                 break at the same place, the letters look a little different: 90%)
images      4    the number of pictures
=======  ======  ==================================================================================

The score is the weighted mean of the checks that could be made. A Word document is laid out by
LibreOffice to be compared page by page, except a Word document LibreOffice itself converted to
PDF: it would be compared with itself, so its layout is only measured against the real PDF.
"""

from __future__ import annotations

import base64
import ctypes
import io
import re
import unicodedata
import zipfile
from collections import Counter
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from lxml import etree
from PIL import Image, ImageFilter
from pypdf import PdfReader
from pypdf.errors import PdfReadError
from pypdf.generic import IndirectObject
from rapidfuzz.distance import Indel

from docfill.convert.engines import run_libreoffice
from docfill.convert.ocr import clean_page
from docfill.errors import DocumentReadError
from docfill.ro import counted

WEIGHTS = {"layout": 50, "text": 30, "pages": 8, "fonts": 8, "images": 4}
TITLES = {
    "layout": "Aranjarea în pagină, pagină cu pagină",
    "text": "Text",
    "pages": "Numărul de pagini",
    "fonts": "Fonturi",
    "images": "Imagini",
}
# (lowest score, verdict, what it means)
VERDICTS = [
    (97.0, "identical", "1:1 — practic identic"),
    (90.0, "very_close", "Foarte apropiat — diferențe mici"),
    (75.0, "close", "Apropiat — diferențe vizibile"),
    (0.0, "different", "Diferit — verificați documentul"),
]
OK, WARN = 95.0, 80.0  # a check is ok from 95, to look at from 80, bad under it
# The picture of a page: white, ink in both, only in the original, only in the converted one.
DIFF_COLOURS = [(255, 255, 255), (110, 110, 110), (214, 40, 40), (37, 99, 235)]

SCALE = 1.0  # pages are drawn at 72 dpi to be compared
INK = 180  # a pixel darker than this grey is ink
TOLERANCE = 2  # points: ink this close is at the same place
# A scan typed again breaks its lines a little differently than the printer did: its lines and
# paragraphs are compared, not its letters.
SCAN_TOLERANCE = 6
MAX_PAGES = 100  # pages compared at most
METRIC_MATCH = 0.9  # a font of the same widths: same layout, the letters look a little different

# Fonts of the same widths (metric-compatible): a document keeps its line and page breaks.
TWINS = [
    {"timesnewroman", "times", "liberationserif", "tinos", "nimbusroman"},
    {"arial", "helvetica", "liberationsans", "arimo", "nimbussans"},
    {"couriernew", "courier", "liberationmono", "cousine", "nimbusmono"},
    {"calibri", "carlito"},
    {"cambria", "caladea"},
    {"georgia", "gelasio"},
    {"arialnarrow", "liberationsansnarrow", "nimbussansnarrow"},
]

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
V_IMAGE = "{urn:schemas-microsoft-com:vml}imagedata"
FALLBACK = "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"
EXTENDED = "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}"
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)


# --------------------------------------------------------------------------- what is compared


@dataclass
class Rendition:
    """A document as it is compared: its pages (as a PDF: the file itself, or its layout by
    LibreOffice), its words, the fonts of its text, its pictures and its number of pages."""

    kind: str  # "pdf" or "docx"
    pdf: bytes | None = None  # None: its pages cannot be laid out apart from the conversion
    words: list[str] = field(default_factory=list)
    words_from_pages: bool = True  # False: read from the Word file (no page headers, numbers)
    fonts: dict[str, int] = field(default_factory=dict)  # name -> characters written with it
    images: int = 0
    pages: int | None = None
    pages_source: str = ""
    warnings: list[str] = field(default_factory=list)
    scan: bool = False  # a scanned PDF: pictures of pages (compared once cleaned, see ocr.py)
    stamps: bool = False  # a scan whose stamps and signatures the converted document kept


def words(text: str) -> list[str]:
    """The words of a text: ligatures split (ﬁ), soft hyphens removed."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"[\u00ad\u0002]\s*", "", text)  # pdfium marks a soft hyphen with U+0002
    return re.findall(r"\w+", text)


def _open_pdf(data: bytes) -> pdfium.PdfDocument:
    try:
        return pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise DocumentReadError(
            f"PDF-ul nu poate fi deschis: este deteriorat sau protejat cu parolă ({exc})"
        ) from exc


def _pdf_text(data: bytes) -> tuple[str, Counter[str], int]:
    """The text of a PDF, the font of each of its characters (spaces left out), its pages."""
    document = _open_pdf(data)
    name = ctypes.create_string_buffer(256)
    try:
        texts, fonts = [], Counter()
        for index in range(len(document)):
            page = document[index]
            textpage = page.get_textpage()
            texts.append(textpage.get_text_range())
            for char in range(textpage.count_chars()):
                if pdfium_c.FPDFText_GetUnicode(textpage.raw, char) <= 32:
                    continue
                if pdfium_c.FPDFText_GetFontInfo(textpage.raw, char, name, len(name), None):
                    fonts[font_name(name.value.decode("utf-8", "replace"))] += 1
            textpage.close()
            page.close()
        return "\n".join(texts), fonts, len(texts)
    finally:
        document.close()


def font_name(name: str) -> str:
    """The family of a font as a PDF names it: ``ABCDEF+TimesNewRomanPS-BoldMT`` (subset tag,
    PostScript name, style) → ``Times New Roman``."""
    name = re.sub(r"^[A-Z]{6}\+", "", name.strip().lstrip("/"))
    family = re.split(r"[-,]", name, maxsplit=1)[0] or name
    family = re.sub(r"(?<=[a-z])(PSMT|PS|MT)$", "", family)
    if " " not in family:  # LiberationSerif: Liberation Serif; SegoeUISymbol: Segoe UI Symbol
        family = re.sub(r"(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", family)
        family = family.replace("Deja Vu", "DejaVu")
    return family


def font_key(name: str) -> str:
    """The family of a font, whatever the way it is written: ``Times New Roman``,
    ``TimesNewRomanPS-BoldMT`` and ``TimesNewRomanPSMT`` are all ``timesnewroman``."""
    key = re.sub(r"[^a-z0-9]", "", font_name(name).lower())
    return re.sub(r"(bold|italic|oblique|regular)+$", "", key) or key


def twin(name: str) -> str | None:
    """A free font of the same widths as ``name`` (Carlito for Calibri), if one is known."""
    names = {
        "liberationserif": "Liberation Serif",
        "liberationsans": "Liberation Sans",
        "liberationmono": "Liberation Mono",
        "carlito": "Carlito",
        "caladea": "Caladea",
        "gelasio": "Gelasio",
        "liberationsansnarrow": "Liberation Sans Narrow",
    }
    key = font_key(name)
    for twins in TWINS:
        if key in twins:
            return next((names[k] for k in sorted(twins) if k in names and k != key), None)
    return None


def match_font(name: str, available: dict[str, str]) -> tuple[str, str | None]:
    """How the font ``name`` is rendered among ``available`` (font key -> name): ``same``,
    ``metric`` (a font of the same widths) or ``replaced``; with the font used."""
    key = font_key(name)
    if key in available:
        return "same", available[key]
    for twins in TWINS:
        if key in twins:
            for other in sorted(twins):
                if other in available:
                    return "metric", available[other]
    return "replaced", None


def _pdf_images(data: bytes) -> int:
    """The pictures of a PDF, each counted once (a logo on every page is one picture)."""
    try:
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            reader.decrypt("")
        images: set[object] = set()
        seen: set[object] = set()

        def walk(resources: Any) -> None:
            if resources is None:
                return
            resources = resources.get_object()
            for reference in (resources.get("/XObject") or {}).values():
                key = reference.idnum if isinstance(reference, IndirectObject) else id(reference)
                if key in seen:
                    continue
                seen.add(key)
                xobject = reference.get_object()
                if xobject.get("/Subtype") == "/Image":
                    images.add(key)
                elif xobject.get("/Subtype") == "/Form":
                    walk(xobject.get("/Resources"))

        for page in reader.pages:
            walk(page.get("/Resources"))
        return len(images)
    except (PdfReadError, ValueError, KeyError, TypeError, AttributeError):
        return 0  # read what can be read: the pages and the text are compared anyway


def pdf_rendition(data: bytes) -> Rendition:
    text, fonts, pages = _pdf_text(data)
    found = words(text)
    rendition = Rendition(
        "pdf", data, found, True, dict(fonts), _pdf_images(data), pages, "în PDF-ul original"
    )
    if pages and not found:
        rendition.scan = True
        rendition.images = 0  # the pictures are the pages themselves
        rendition.warnings.append(
            "PDF-ul nu conține text, ci doar imagini ale paginilor (este scanat): textul său nu "
            "poate fi comparat, iar un document Word creat din el afișează paginile ca imagini. "
            "Citiți-l mai întâi prin OCR pentru a-i putea modifica textul."
        )
    return rendition


# --------------------------------------------------------------------------- Word documents


def _in_fallback(node: Any) -> bool:
    """Inside the copy Word keeps for old readers (``mc:Fallback``): counted once already."""
    return next(node.iterancestors(FALLBACK), None) is not None


class _WordFile:
    """The parts of a .docx compared: text, fonts, pictures, the statistics Word saved."""

    def __init__(self, data: bytes):
        try:
            self.archive = zipfile.ZipFile(io.BytesIO(data))
            self.body = self._xml("word/document.xml")
        except (zipfile.BadZipFile, KeyError, etree.XMLSyntaxError) as exc:
            raise DocumentReadError(f"document Word invalid ({exc})") from exc
        names = sorted(self.archive.namelist())
        self.headers = self._all(n for n in names if re.fullmatch(r"word/header\d*\.xml", n))
        self.footers = self._all(n for n in names if re.fullmatch(r"word/footer\d*\.xml", n))
        self.notes = self._all(["word/footnotes.xml", "word/endnotes.xml"])
        self.styles = self._optional("word/styles.xml")
        themes = [n for n in names if re.fullmatch(r"word/theme/theme\d*\.xml", n)]
        theme = self._optional(themes[0]) if themes else None
        self.theme = {
            kind: font.get("typeface")
            for kind in ("major", "minor")
            if theme is not None and (font := theme.find(f".//{A}{kind}Font/{A}latin")) is not None
        }

    def _xml(self, name: str) -> Any:
        return etree.fromstring(self.archive.read(name), _PARSER)

    def _optional(self, name: str) -> Any:
        try:
            return self._xml(name)
        except (KeyError, etree.XMLSyntaxError):
            return None

    def _all(self, names: Iterable[str]) -> list[Any]:
        """The parts that are there and can be read (a damaged header is left out)."""
        return [part for name in names if (part := self._optional(name)) is not None]

    @property
    def parts(self) -> list[Any]:
        """In the order a page prints them: header, body, notes, footer."""
        return [*self.headers, self.body, *self.notes, *self.footers]

    @staticmethod
    def paragraphs(root: Any) -> Iterator[str]:
        for paragraph in root.iter(f"{W}p"):
            if _in_fallback(paragraph):
                continue
            pieces = []
            for node in paragraph.iter(f"{W}t", f"{W}tab", f"{W}br", f"{W}cr"):
                # a text box inside the paragraph is a paragraph of its own
                if next(node.iterancestors(f"{W}p")) is paragraph:
                    pieces.append(node.text or "" if node.tag == f"{W}t" else " ")
            yield "".join(pieces)

    def text(self) -> str:
        return "\n".join(text for part in self.parts for text in self.paragraphs(part))

    def _font(self, fonts: Any) -> str | None:
        if fonts is None:
            return None
        theme = fonts.get(f"{W}asciiTheme") or fonts.get(f"{W}hAnsiTheme")
        if theme and (name := self.theme.get("major" if theme.startswith("major") else "minor")):
            return name
        return fonts.get(f"{W}ascii") or fonts.get(f"{W}hAnsi")

    def fonts(self) -> dict[str, int]:
        """The font of every character of text, as Word picks it: the run's own font, then
        its character style, its paragraph's style (each with the styles it is based on),
        then the document's default."""
        styles: dict[str, Any] = {}
        default_style = defaults = None
        if self.styles is not None:
            for style in self.styles.iter(f"{W}style"):
                styles[style.get(f"{W}styleId")] = style
                if style.get(f"{W}type") == "paragraph" and style.get(f"{W}default") in (
                    "1",
                    "true",
                ):
                    default_style = style.get(f"{W}styleId")
            defaults = self._font(
                self.styles.find(f"{W}docDefaults/{W}rPrDefault/{W}rPr/{W}rFonts")
            )

        def style_font(style_id: str | None) -> str | None:
            seen = set()
            while style_id and style_id in styles and style_id not in seen:
                seen.add(style_id)
                style = styles[style_id]
                if name := self._font(style.find(f"{W}rPr/{W}rFonts")):
                    return name
                based = style.find(f"{W}basedOn")
                style_id = based.get(f"{W}val") if based is not None else None
            return None

        def value(node: Any, path: str) -> str | None:
            found = node.find(path) if node is not None else None
            return found.get(f"{W}val") if found is not None else None

        counted: Counter[str] = Counter()
        for part in self.parts:
            for run in part.iter(f"{W}r"):
                text = "".join(node.text or "" for node in run.findall(f"{W}t"))
                if not text.strip() or _in_fallback(run):
                    continue
                paragraph = next(run.iterancestors(f"{W}p"), None)
                name = (
                    self._font(run.find(f"{W}rPr/{W}rFonts"))
                    or style_font(value(run, f"{W}rPr/{W}rStyle"))
                    or style_font(value(paragraph, f"{W}pPr/{W}pStyle") or default_style)
                    or defaults
                )
                if name:
                    counted[name] += len(text.strip())
        return dict(counted)

    def images(self) -> int:
        found = set()
        for index, part in enumerate(self.parts):
            for node in part.iter(f"{A}blip", V_IMAGE):
                if _in_fallback(node):
                    continue
                target = node.get(f"{R}embed") or node.get(f"{R}link") or node.get(f"{R}id")
                found.add((index, target or id(node)))
        return len(found)

    def recorded_pages(self) -> tuple[int, str] | None:
        """The number of pages saved with the document, when it is up to date: the number of
        words saved with it is the number of words it has (a document changed by another
        program keeps the statistics of its last save in Word)."""
        app = self._optional("docProps/app.xml")
        if app is None:
            return None
        try:
            pages = int(app.findtext(f"{EXTENDED}Pages") or 0)
            recorded = int(app.findtext(f"{EXTENDED}Words") or 0)
        except ValueError:
            return None
        counted = sum(len(text.split()) for text in self.paragraphs(self.body))
        if not pages or not recorded or abs(recorded - counted) > max(20, 0.1 * counted):
            return None
        application = (app.findtext(f"{EXTENDED}Application") or "").strip()
        return pages, f"după numărarea făcută de {application or 'editor'} la ultima salvare"


def word_rendition(data: bytes, kind: str, timeout: int, layout: bool = True) -> Rendition:
    """A Word document (``kind``: ``docx`` or ``doc``). ``layout``: its pages are laid out by
    LibreOffice to be compared; otherwise its text is read from the file and its number of
    pages is the one saved with it."""
    docx = run_libreoffice(data, ".doc", "docx", timeout) if kind == "doc" else data
    word = _WordFile(docx)
    fonts, images = word.fonts(), word.images()
    if not layout:
        rendition = Rendition("docx", None, words(word.text()), False, fonts, images)
        if recorded := word.recorded_pages():
            rendition.pages, rendition.pages_source = recorded
        return rendition
    laid_out = pdf_rendition(run_libreoffice(data, f".{kind}", "pdf", timeout))
    rendition = Rendition(
        "docx",
        laid_out.pdf,
        laid_out.words,
        True,
        fonts,
        images,
        laid_out.pages,
        "în documentul Word paginat cu LibreOffice",
    )
    used = {font_key(name): name for name in laid_out.fonts}
    for name in fonts:
        if match_font(name, used)[0] == "replaced":
            rendition.warnings.append(
                f"Fontul {name} nu este instalat aici: documentul Word a fost paginat cu alt "
                "font pentru comparare, așa că scorul aranjării în pagină poate fi mai mic decât "
                "în Word."
            )
    return rendition


# --------------------------------------------------------------------------- the scores


@dataclass
class Check:
    key: str
    score: float | None  # 0-100; None: could not be measured
    detail: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.score is None:
            return "na"
        return "ok" if self.score >= OK else "warn" if self.score >= WARN else "bad"

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "title": TITLES[self.key],
            "score": None if self.score is None else round(self.score, 1),
            "weight": WEIGHTS[self.key],
            "status": self.status,
            "detail": self.detail,
            **self.extra,
        }


@dataclass
class PageScore:
    number: int
    score: float
    # The first pages, as a PNG data URI of 4 colours (see ``_diff_image``): the page shows the
    # differences, the original or the converted page by changing its colours.
    diff: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"number": self.number, "score": round(self.score, 1), "diff": self.diff}


@dataclass
class Fidelity:
    """How close a converted document is to the document it is compared with."""

    against: str  # "original" or "reference" (the real document)
    checks: list[Check]
    pages: list[PageScore] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def score(self) -> float | None:
        measured = [check for check in self.checks if check.score is not None]
        if not measured:
            return None
        total = sum(WEIGHTS[check.key] for check in measured)
        return sum(WEIGHTS[check.key] * check.score for check in measured) / total

    @property
    def verdict(self) -> tuple[str, str]:
        score = self.score
        if score is None:
            return "unknown", "Nu s-a putut compara nimic"
        return next((key, label) for lowest, key, label in VERDICTS if score >= lowest)

    def check(self, key: str) -> Check:
        return next(check for check in self.checks if check.key == key)

    def as_dict(self) -> dict[str, Any]:
        verdict, label = self.verdict
        if self.score is not None and self.check("layout").score is None:
            label += " (paginile nu au fost comparate)"
        return {
            "against": self.against,
            "score": None if self.score is None else round(self.score, 1),
            "verdict": verdict,
            "label": label,
            "checks": [check.as_dict() for check in self.checks],
            "pages": [page.as_dict() for page in self.pages],
            "warnings": self.warnings,
        }


def _ink(image: Image.Image) -> np.ndarray:
    return np.asarray(image) < INK


def _near(mask: np.ndarray, tolerance: int) -> np.ndarray:
    """Everything within ``tolerance`` points of the ink."""
    grown = Image.fromarray(mask.astype(np.uint8) * 255).filter(
        ImageFilter.MaxFilter(2 * tolerance + 1)
    )
    return np.asarray(grown) > 0


def page_similarity(
    original: np.ndarray, converted: np.ndarray, tolerance: int = TOLERANCE
) -> float:
    """0-1: the ink of each page found near the ink of the other (both ways: what is missing
    and what was added count), as an F1 score. Both masks have the same size."""
    if not original.any() and not converted.any():
        return 1.0
    if not original.any() or not converted.any():
        return 0.0
    kept = float((original & _near(converted, tolerance)).sum() / original.sum())
    exact = float((converted & _near(original, tolerance)).sum() / converted.sum())
    return 0.0 if kept + exact == 0 else 2 * kept * exact / (kept + exact)


def _page(
    document: pdfium.PdfDocument, index: int, scan: bool = False, stamps: bool = False
) -> Image.Image | None:
    """A page drawn to be compared; a scanned page cleaned the way OCR reads it (stamps off,
    paper evened out, straightened)."""
    if index >= len(document):
        return None
    page = document[index]
    try:
        if not scan:
            return page.render(scale=SCALE).to_pil().convert("L")
        image = page.render(scale=150 / 72).to_pil()
        width, height = page.get_size()
        size = (round(width * SCALE), round(height * SCALE))
        return clean_page(image, stamps).resize(size, Image.Resampling.LANCZOS)
    finally:
        page.close()


def _canvas(image: Image.Image | None, size: tuple[int, int]) -> Image.Image:
    canvas = Image.new("L", size, 255)
    if image is not None:
        canvas.paste(image, (0, 0))
    return canvas


def _png(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def _diff_image(original: np.ndarray, converted: np.ndarray) -> Image.Image:
    """Grey: ink in both; red: only in the original (missing or moved); blue: only in the
    converted document (added or moved)."""
    index = np.zeros(original.shape, np.uint8)
    index[original & converted] = 1
    index[original & ~converted] = 2
    index[~original & converted] = 3
    image = Image.fromarray(index)
    image.putpalette([channel for colour in DIFF_COLOURS for channel in colour])
    return image


def _layout(expected: Rendition, actual: Rendition, previews: int) -> tuple[Check, list[PageScore]]:
    if expected.pdf is None or actual.pdf is None:
        return Check(
            "layout",
            None,
            "Nemăsurat: acest PDF a fost creat de LibreOffice, deci ar fi comparat cu propria "
            "sa paginare. Adăugați PDF-ul de referință (cel salvat de Word) pentru a compara "
            "paginile.",
        ), []
    tolerance = SCAN_TOLERANCE if expected.scan else TOLERANCE
    left, right = _open_pdf(expected.pdf), _open_pdf(actual.pdf)
    try:
        count = max(len(left), len(right))
        pages = []
        for index in range(min(count, MAX_PAGES)):
            original = _page(left, index, expected.scan, expected.stamps)
            converted = _page(right, index)
            present = [image for image in (original, converted) if image is not None]
            size = (max(i.width for i in present), max(i.height for i in present))
            a, b = _ink(_canvas(original, size)), _ink(_canvas(converted, size))
            both = original is not None and converted is not None
            score = 100 * page_similarity(a, b, tolerance) if both else 0.0
            page = PageScore(index + 1, score)
            if index < previews:
                page.diff = _png(_diff_image(a, b))
            pages.append(page)
    finally:
        left.close()
        right.close()
    if not pages:
        return Check("layout", None, "Niciunul dintre documente nu are pagini."), []
    mean = sum(page.score for page in pages) / len(pages)
    worst = min(pages, key=lambda page: page.score)
    detail = counted(len(pages), "pagină comparată", "pagini comparate")
    if expected.scan:
        kept = (
            "ștampilele și semnăturile păstrate ca imagini" if expected.stamps else "fără ștampile"
        )
        detail += (
            f" (document scanat: rândurile comparate cu o toleranță de 2 mm, ca la o "
            f"retehnoredactare; {kept})"
        )
    if count > MAX_PAGES:
        detail += f" (primele {MAX_PAGES} din {count})"
    if len(pages) > 1:
        same = sum(page.score >= OK for page in pages)
        detail += (
            f"; practic identice: {same}; cel mai mic scor: pagina {worst.number} "
            f"({worst.score:.0f}%)"
        )
    return Check("layout", mean, detail + "."), pages


def _clip(found: list[str], limit: int = 12) -> str:
    return " ".join(found[:limit]) + (" …" if len(found) > limit else "")


def _text(expected: Rendition, actual: Rendition) -> Check:
    if not expected.words:
        return Check("text", None, "Nemăsurat: originalul nu conține text (este scanat?).")
    a = [word.casefold() for word in expected.words]
    b = [word.casefold() for word in actual.words]
    common = (len(a) + len(b) - Indel.distance(a, b)) // 2  # longest common subsequence
    both_printed = expected.words_from_pages and actual.words_from_pages
    if both_printed:
        score = 200 * common / (len(a) + len(b))
        detail = f"{common} din {counted(len(a), 'cuvânt', 'cuvinte')}, în aceeași ordine"
        if added := len(b) - common:
            detail += f"; adăugate: {added}"
    else:  # the PDF also prints page headers, footers and list numbers on every page
        score = 100 * common / len(a)
        detail = (
            f"se regăsesc {common} din {counted(len(a), 'cuvânt', 'cuvinte')} ale documentului, "
            "în aceeași ordine"
        )
    differences: list[dict[str, str]] = []
    pending: dict[str, Any] | None = None
    for op in [*Indel.opcodes(a, b), None]:
        if op is not None and op.tag != "equal":
            pending = pending or {"at": op.src_start, "missing": [], "added": []}
            if op.tag == "delete":
                pending["missing"] += expected.words[op.src_start : op.src_end]
            else:
                pending["added"] += actual.words[op.dest_start : op.dest_end]
        elif pending:
            if pending["missing"] or both_printed:
                at = pending["at"]
                differences.append(
                    {
                        "before": _clip(expected.words[max(0, at - 6) : at]),
                        "missing": _clip(pending["missing"]),
                        "added": _clip(pending["added"]) if both_printed else "",
                    }
                )
            pending = None
    extra = {"differences": differences[:10], "differences_total": len(differences)}
    return Check("text", score, detail + ".", extra)


def _pages_check(expected: Rendition, actual: Rendition) -> Check:
    if not expected.pages or not actual.pages:
        return Check(
            "pages",
            None,
            "Nemăsurat: numărul de pagini calculat de Word nu este salvat în document sau nu "
            "este actualizat (documentul a fost modificat cu alt program).",
        )
    score = 100 * max(0.0, 1 - abs(actual.pages - expected.pages) / expected.pages)
    return Check(
        "pages",
        score,
        f"{counted(actual.pages, 'pagină', 'pagini')} față de {expected.pages} "
        f"{expected.pages_source}.",
    )


def _fonts_check(expected: Rendition, actual: Rendition) -> Check:
    if not expected.fonts:
        return Check("fonts", None, "Nemăsurat: fonturile originalului nu sunt cunoscute.")
    families: dict[str, list[Any]] = {}  # key -> [name, characters]
    for name, characters in expected.fonts.items():
        families.setdefault(font_key(name), [font_name(name), 0])[1] += characters
    available = {font_key(name): font_name(name) for name in actual.fonts}
    total = sum(characters for _, characters in families.values())
    rows, points = [], 0.0
    for name, characters in sorted(families.values(), key=lambda family: -family[1]):
        match, used = match_font(name, available)
        points += characters * {"same": 1.0, "metric": METRIC_MATCH, "replaced": 0.0}[match]
        rows.append({"font": name, "used": used, "match": match, "share": characters / total})
    replaced = [
        row["font"] + (f" (sau {free}, cu aceleași lățimi)" if (free := twin(row["font"])) else "")
        for row in rows
        if row["match"] == "replaced"
    ]
    metric = [f"{row['font']} → {row['used']}" for row in rows if row["match"] == "metric"]
    matched = {font_key(row["used"]) for row in rows if row["used"]}
    instead = [name for key, name in available.items() if key not in matched]
    if replaced:
        detail = (
            f"Înlocuite cu alte fonturi: {', '.join(replaced)}. Rândurile se pot termina în alt "
            "loc: instalați aceste fonturi pe calculatorul pe care se face conversia."
        )
        if instead:
            detail += f" Folosite în schimb: {', '.join(instead)}."
    elif metric:
        detail = (
            f"Fonturi cu aceleași lățimi: {', '.join(metric)} (rândurile se termină în același "
            "loc; instalați fonturile originale pentru aceleași forme ale literelor)."
        )
    else:
        detail = "Aceleași fonturi."
    return Check("fonts", 100 * points / total, detail, {"fonts": rows})


def _images(expected: Rendition, actual: Rendition) -> Check:
    if expected.scan:
        return Check("images", None, "Document scanat: paginile sunt imagini și nu se numără.")
    if not expected.images and not actual.images:
        return Check("images", None, "Nicio imagine.")
    score = 100 * min(expected.images, actual.images) / max(expected.images, actual.images)
    return Check("images", score, f"{actual.images} față de {expected.images} în original.")


def ocr_text(expected: Rendition, ocr: dict[str, Any]) -> Check:
    """The text of a scan read with OCR: nothing to compare it with, so how sure the reading
    is, and the words to check."""
    uncertain = ocr.get("uncertain", [])
    detail = (
        f"Citit prin OCR (documentul scanat nu conține text): "
        f"{counted(ocr['words'], 'cuvânt', 'cuvinte')}, grad mediu de încredere "
        f"{ocr['confidence']:.0f}%"
    )
    if uncertain:
        sample = ", ".join(dict.fromkeys(uncertain))
        detail += f"; de verificat ({len(uncertain)}): {sample[:300]}"
    return Check("text", float(ocr["confidence"]), detail + ".", {"uncertain": uncertain[:100]})


def compare(
    expected: Rendition, actual: Rendition, against: str = "original", previews: int = 12
) -> Fidelity:
    """Score ``actual`` (the converted document) against ``expected`` (the original, or the
    real document). ``previews``: the first pages given as pictures (original, converted,
    differences)."""
    layout, pages = _layout(expected, actual, previews)
    checks = [
        layout,
        _text(expected, actual),
        _pages_check(expected, actual),
        _fonts_check(expected, actual),
        _images(expected, actual),
    ]
    warnings = list(dict.fromkeys(expected.warnings + actual.warnings))
    return Fidelity(against, checks, pages, warnings)
