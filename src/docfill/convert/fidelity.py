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
from docfill.errors import DocumentReadError

WEIGHTS = {"layout": 50, "text": 30, "pages": 8, "fonts": 8, "images": 4}
TITLES = {
    "layout": "Layout, page by page",
    "text": "Text",
    "pages": "Number of pages",
    "fonts": "Fonts",
    "images": "Pictures",
}
# (lowest score, verdict, what it means)
VERDICTS = [
    (97.0, "identical", "1:1 — practically identical"),
    (90.0, "very_close", "Very close — small differences"),
    (75.0, "close", "Close — visible differences"),
    (0.0, "different", "Different — check the document"),
]
OK, WARN = 95.0, 80.0  # a check is ok from 95, to look at from 80, bad under it
# The picture of a page: white, ink in both, only in the original, only in the converted one.
DIFF_COLOURS = [(255, 255, 255), (110, 110, 110), (214, 40, 40), (37, 99, 235)]

SCALE = 1.0  # pages are drawn at 72 dpi to be compared
INK = 180  # a pixel darker than this grey is ink
TOLERANCE = 2  # points: ink this close is at the same place
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
            f"the PDF cannot be opened: damaged, or protected by a password ({exc})"
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
        "pdf", data, found, True, dict(fonts), _pdf_images(data), pages, "in the original PDF"
    )
    if pages and not found:
        rendition.warnings.append(
            "The PDF has no text, only pictures of its pages (a scan): its text cannot be "
            "compared, and a Word document made from it shows the pages as pictures. Read it "
            "with OCR first to edit its text."
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
            raise DocumentReadError(f"invalid Word document ({exc})") from exc
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
        return pages, f"as {application or 'the editor'} counted them when it last saved it"


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
        "in the Word document laid out by LibreOffice",
    )
    used = {font_key(name): name for name in laid_out.fonts}
    for name in fonts:
        if match_font(name, used)[0] == "replaced":
            rendition.warnings.append(
                f"The font {name} is not installed here: the Word document was laid out with "
                "another font to be compared, so its layout score may be lower than in Word."
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
            return "unknown", "Nothing could be compared"
        return next((key, label) for lowest, key, label in VERDICTS if score >= lowest)

    def check(self, key: str) -> Check:
        return next(check for check in self.checks if check.key == key)

    def as_dict(self) -> dict[str, Any]:
        verdict, label = self.verdict
        if self.score is not None and self.check("layout").score is None:
            label += " (pages not compared)"
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


def _near(mask: np.ndarray) -> np.ndarray:
    """Everything within ``TOLERANCE`` points of the ink."""
    grown = Image.fromarray(mask.astype(np.uint8) * 255).filter(
        ImageFilter.MaxFilter(2 * TOLERANCE + 1)
    )
    return np.asarray(grown) > 0


def page_similarity(original: np.ndarray, converted: np.ndarray) -> float:
    """0-1: the ink of each page found near the ink of the other (both ways: what is missing
    and what was added count), as an F1 score. Both masks have the same size."""
    if not original.any() and not converted.any():
        return 1.0
    if not original.any() or not converted.any():
        return 0.0
    kept = float((original & _near(converted)).sum() / original.sum())
    exact = float((converted & _near(original)).sum() / converted.sum())
    return 0.0 if kept + exact == 0 else 2 * kept * exact / (kept + exact)


def _page(document: pdfium.PdfDocument, index: int) -> Image.Image | None:
    if index >= len(document):
        return None
    page = document[index]
    try:
        return page.render(scale=SCALE).to_pil().convert("L")
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
            "Not measured: LibreOffice made this PDF, so it would be compared with its own "
            "layout. Add the real PDF (the one Word saves) to compare the pages.",
        ), []
    left, right = _open_pdf(expected.pdf), _open_pdf(actual.pdf)
    try:
        count = max(len(left), len(right))
        pages = []
        for index in range(min(count, MAX_PAGES)):
            original, converted = _page(left, index), _page(right, index)
            present = [image for image in (original, converted) if image is not None]
            size = (max(i.width for i in present), max(i.height for i in present))
            a, b = _ink(_canvas(original, size)), _ink(_canvas(converted, size))
            both = original is not None and converted is not None
            score = 100 * page_similarity(a, b) if both else 0.0
            page = PageScore(index + 1, score)
            if index < previews:
                page.diff = _png(_diff_image(a, b))
            pages.append(page)
    finally:
        left.close()
        right.close()
    if not pages:
        return Check("layout", None, "Neither document has a page."), []
    mean = sum(page.score for page in pages) / len(pages)
    worst = min(pages, key=lambda page: page.score)
    detail = f"{len(pages)} page{'s' if len(pages) != 1 else ''} compared"
    if count > MAX_PAGES:
        detail += f" (the first {MAX_PAGES} of {count})"
    if len(pages) > 1:
        same = sum(page.score >= OK for page in pages)
        detail += (
            f"; {same} practically identical; lowest: page {worst.number} ({worst.score:.0f}%)"
        )
    return Check("layout", mean, detail + "."), pages


def _clip(found: list[str], limit: int = 12) -> str:
    return " ".join(found[:limit]) + (" …" if len(found) > limit else "")


def _text(expected: Rendition, actual: Rendition) -> Check:
    if not expected.words:
        return Check("text", None, "Not measured: the original has no text (a scan?).")
    a = [word.casefold() for word in expected.words]
    b = [word.casefold() for word in actual.words]
    common = (len(a) + len(b) - Indel.distance(a, b)) // 2  # longest common subsequence
    both_printed = expected.words_from_pages and actual.words_from_pages
    if both_printed:
        score = 200 * common / (len(a) + len(b))
        detail = f"{common} of {len(a)} words in the same order"
        if added := len(b) - common:
            detail += f"; {added} added"
    else:  # the PDF also prints page headers, footers and list numbers on every page
        score = 100 * common / len(a)
        detail = f"{common} of the {len(a)} words of the document are in it, in the same order"
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
            "Not measured: the number of pages Word counted is not saved in the document, or "
            "is out of date (it was changed by another program).",
        )
    score = 100 * max(0.0, 1 - abs(actual.pages - expected.pages) / expected.pages)
    plural = "s" if actual.pages != 1 else ""
    return Check(
        "pages",
        score,
        f"{actual.pages} page{plural} for {expected.pages} {expected.pages_source}.",
    )


def _fonts_check(expected: Rendition, actual: Rendition) -> Check:
    if not expected.fonts:
        return Check("fonts", None, "Not measured: the fonts of the original are not known.")
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
        row["font"] + (f" (or {free}, of the same widths)" if (free := twin(row["font"])) else "")
        for row in rows
        if row["match"] == "replaced"
    ]
    metric = [f"{row['font']} → {row['used']}" for row in rows if row["match"] == "metric"]
    matched = {font_key(row["used"]) for row in rows if row["used"]}
    instead = [name for key, name in available.items() if key not in matched]
    if replaced:
        detail = (
            f"Replaced by other fonts: {', '.join(replaced)}. Lines may break elsewhere: "
            "install these fonts where the document is converted."
        )
        if instead:
            detail += f" Used instead: {', '.join(instead)}."
    elif metric:
        detail = (
            f"Fonts of the same widths: {', '.join(metric)} (the lines break at the same "
            "place; install the original fonts for the same letters)."
        )
    else:
        detail = "The same fonts."
    return Check("fonts", 100 * points / total, detail, {"fonts": rows})


def _images(expected: Rendition, actual: Rendition) -> Check:
    if not expected.images and not actual.images:
        return Check("images", None, "No pictures.")
    score = 100 * min(expected.images, actual.images) / max(expected.images, actual.images)
    return Check("images", score, f"{actual.images} for {expected.images} in the original.")


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
