"""Scanned PDFs to Word documents that can be edited like any other.

A scanned page is a picture: pdf2docx and LibreOffice can only put the picture in the Word
document. Here every page is read with Tesseract (Romanian and English) and its text written back
the way it was typed in Word: paragraphs that flow (not a box per line), justified or centred,
with their first-line indent, the space between them, the size of their letters and their bold
words, on pages with the margins of the scan.

============  ===================================================================================
Step          How
============  ===================================================================================
picture       each page drawn at 300 dpi; the paper evened out to white (a darker edge, the
              show-through of the other side fade, the ink stays), the page straightened
read          Tesseract, one process per page, pages side by side
noise         a word read with little confidence, or on a coloured stamp, is left out, and so is a
              line of such words (a signature, a punched hole, a stamp)
size          the line pitch gives the size of the text (Word's single spacing is 1.15 times the
              size of the letters); the height of the capitals gives the size of a title
bold          the strokes of a word thicker than those of the text (Tesseract does not tell)
paragraphs    a line starts a paragraph after a blank line or a short line (the last line of a
              paragraph), when it is indented or centred; the lines of a paragraph are joined, a
              word cut by a hyphen at the end of a line kept whole; a paragraph going on to the
              next page stays one paragraph; a page that ends early ends with a page break
alignment     justified when the lines reach the right margin, centred when they stand in the
              middle, right-aligned when they end at the margin far from the left
columns       words far apart on one line (signatures side by side) are separated by tabs
============  ===================================================================================
"""

from __future__ import annotations

import csv
import io
import os
import re
import subprocess
import tempfile
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from statistics import median

import numpy as np
import pypdfium2 as pdfium
from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Pt
from PIL import Image, ImageFilter

from docfill.config import Settings
from docfill.errors import ConversionError, DocumentReadError
from docfill.readers.ocr import resolve_languages, tesseract_available

DPI = 300
FONT = "Times New Roman"  # the font of the documents filers type (Liberation Serif has its widths)
SIZES = (8, 9, 10, 10.5, 11, 11.5, 12, 13, 14, 15, 16, 18, 20, 22, 24, 28, 32)
SINGLE = 1.15  # Word's single line spacing, in sizes of the letters (Times New Roman)
MIN_CONFIDENCE = 30  # a word read with less is noise
STAMP_CONFIDENCE = 75  # a word on a coloured stamp needs this much
# A word is bold when its strokes are thicker than those of the text of its page by more than
# the text varies (toner, a page photographed: 5 times its median deviation) and at least 15%.
BOLD_SPREAD, BOLD_LEAST = 5, 0.15
INK = 140  # grey level under which a pixel is ink (paper evened out to white)
RULE = 215  # an underline is thin: on a scan of little resolution it is only grey
PUNCTUATION = set("-–—•·,.;:!?\"'„”“’‘«»()[]/%&*+=§°")
_TSV_FIELDS = ("left", "top", "width", "height")


def _pt(pixels: float) -> float:
    return pixels / DPI * 72


# --------------------------------------------------------------------------- what is read


@dataclass
class Word:
    text: str
    left: int
    top: int
    right: int
    bottom: int
    confidence: float
    stroke: float = 0.0  # half the thickness of its strokes, in pixels (ink / outline)
    bold: bool = False
    underline: int = 0  # the height of the rule under it, with the gap above the rule (pixels)

    @property
    def height(self) -> int:
        """The height of its letters (the rule under an underlined word left out)."""
        return self.bottom - self.top - self.underline

    @property
    def letters(self) -> int:
        return sum(char.isalnum() for char in self.text)

    @property
    def capitals(self) -> bool:
        """Written in capitals: its height is the height of the capitals."""
        letters = [char for char in self.text if char.isalpha()]
        return len(letters) >= 2 and all(char.isupper() for char in letters)


@dataclass
class Line:
    words: list[Word]
    size: float = 0.0  # in points, set once the size of the text is known
    breaks: set[int] = field(default_factory=set)  # a column starts after these words

    @property
    def left(self) -> int:
        return min(word.left for word in self.words)

    @property
    def right(self) -> int:
        return max(word.right for word in self.words)

    @property
    def top(self) -> int:
        return min(word.top for word in self.words)

    @property
    def bottom(self) -> int:
        return max(word.bottom for word in self.words)

    @property
    def center(self) -> float:
        return (self.left + self.right) / 2

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)

    @property
    def confidence(self) -> float:
        return sum(word.confidence for word in self.words) / len(self.words)

    def columns(self, wide: float) -> list[int]:
        """The words after which a column starts: where two lines read apart on the same row
        were joined, or where the line jumps further than ``wide`` (a tab)."""
        return [
            index
            for index, (word, after) in enumerate(zip(self.words, self.words[1:], strict=False))
            if index in self.breaks or after.left - word.right > wide
        ]


@dataclass
class Page:
    number: int
    width: int  # in pixels at DPI
    height: int
    lines: list[Line]
    left: float = 0.0  # where the text starts and ends: the margins of the page
    right: float = 0.0
    dropped: int = 0  # words left out as noise


@dataclass
class Paragraph:
    lines: list[Line]
    page: Page
    alignment: str = "left"  # left, center, right, justify
    first_indent: float = 0.0  # in pixels
    left_indent: float = 0.0
    space_before: float = 0.0
    page_break: bool = False
    tabs: list[float] = field(default_factory=list)  # tab stops, in pixels from the margin
    bullet: bool = False  # an item of a list: "-", a tab, the text (hanging indent)

    @property
    def size(self) -> float:
        return median(line.size for line in self.lines)


@dataclass
class OcrResult:
    """The Word document, and how sure the reading is."""

    data: bytes
    pages: int
    words: int
    confidence: float  # mean confidence of the words, weighted by their letters
    uncertain: list[str]  # words read with less than 60% confidence
    dropped: int  # words left out as noise (stamps, signatures)


# --------------------------------------------------------------------------- the picture


def _even_out(gray: Image.Image) -> Image.Image:
    """The paper evened out to white: each point divided by the shade of the paper around it
    (the brightest within ~5 mm). A darker edge or the show-through of the other side fades; the
    ink stays dark."""
    small = gray.reduce(8).filter(ImageFilter.MaxFilter(9))
    paper = np.asarray(small.resize(gray.size, Image.Resampling.BILINEAR), np.float32)
    evened = np.asarray(gray, np.float32) / np.maximum(paper, 1) * 255
    return Image.fromarray(np.clip(evened, 0, 255).astype(np.uint8))


def _colour(rgb: Image.Image) -> Image.Image:
    """The coloured ink (a blue or red stamp, a blue signature), white on black."""
    pixels = np.asarray(rgb, np.int16)
    red, green, blue = pixels[..., 0], pixels[..., 1], pixels[..., 2]
    mask = (blue - np.maximum(red, green) > 30) | (red - np.maximum(green, blue) > 45)
    return Image.fromarray(mask.astype(np.uint8) * 255)


def _skew(gray: Image.Image) -> float:
    """The angle the page is turned by (degrees): the one whose rows of ink are the sharpest."""
    small = np.asarray(gray.reduce(4)) < INK
    ink = Image.fromarray(small.astype(np.uint8) * 255)
    best, angle = -1.0, 0.0
    for candidate in np.arange(-3.0, 3.01, 0.1):
        rows = np.asarray(ink.rotate(candidate, Image.Resampling.NEAREST)).sum(axis=1)
        sharpness = float(np.var(rows))
        if sharpness > best:
            best, angle = sharpness, float(candidate)
    return angle


def prepare(image: Image.Image) -> tuple[Image.Image, Image.Image]:
    """The page to read (grey, evened out, straightened) and its coloured ink."""
    rgb = image.convert("RGB")
    gray, colour = _even_out(rgb.convert("L")), _colour(rgb)
    angle = _skew(gray)
    if abs(angle) >= 0.1:
        gray = gray.rotate(angle, Image.Resampling.BICUBIC, fillcolor=255)
        colour = colour.rotate(angle, Image.Resampling.NEAREST, fillcolor=0)
    return gray, colour


def clean_page(image: Image.Image) -> Image.Image:
    """A scanned page as it is compared with the document read from it: the paper evened out,
    the stamps taken off, the page straightened."""
    gray, colour = prepare(image)
    pixels = np.where(np.asarray(colour) > 0, 255, np.asarray(gray))
    return Image.fromarray(pixels.astype(np.uint8))


# --------------------------------------------------------------------------- reading


def _tesseract(image: Image.Image, settings: Settings, timeout: int) -> list[dict[str, str]]:
    """The words Tesseract reads, with their box and confidence (its TSV output)."""
    command = settings.tesseract_cmd or "tesseract"
    languages = resolve_languages(settings.ocr_languages)
    with tempfile.TemporaryDirectory(prefix="docfill-ocr-") as folder:
        path = Path(folder) / "page.png"
        image.save(path, dpi=(DPI, DPI))
        try:
            completed = subprocess.run(
                [command, str(path), "stdout", "-l", languages, "--psm", "3", "tsv"],
                capture_output=True,
                text=True,
                timeout=timeout,
                # pages are read side by side: one thread each
                env={**os.environ, "OMP_THREAD_LIMIT": "1"},
            )
        except subprocess.TimeoutExpired as exc:
            raise ConversionError(f"OCR did not finish within {timeout} s") from exc
        except OSError as exc:
            raise ConversionError(f"Tesseract could not be started ({exc})") from exc
    if completed.returncode != 0:
        raise ConversionError(f"Tesseract could not read the page ({completed.stderr[-300:]})")
    rows = csv.DictReader(io.StringIO(completed.stdout), delimiter="\t", quoting=csv.QUOTE_NONE)
    return [row for row in rows if row.get("level") == "5" and (row.get("text") or "").strip()]


def _underline(gray: np.ndarray, word: Word) -> int:
    """The rule under a word, when its box takes it in: rows of ink across the whole word in
    the bottom of the box. Returns how much of the box it takes (0: not underlined)."""
    ink = gray[word.top : word.bottom, word.left : word.right] < RULE
    if ink.shape[0] < 8 or ink.shape[1] < 8:
        return 0
    rows = ink.mean(axis=1)
    full = np.flatnonzero(rows >= 0.85)
    rule = full[full >= 0.6 * ink.shape[0]]
    if not rule.size:
        return 0
    letters = np.flatnonzero(rows[: rule[0]] > 0.1)
    # the rule stands apart from the letters: the flat foot of a letter is no underline
    if not letters.size or rule[0] - letters[-1] - 1 < 2:
        return 0
    return ink.shape[0] - int(letters[-1]) - 1


def _stroke(gray: np.ndarray, word: Word) -> float:
    """Half the thickness of the strokes of a word: its ink divided by the outline of the ink."""
    ink = gray[word.top : word.bottom - word.underline, word.left : word.right] < INK
    if ink.sum() < 15 or min(ink.shape) < 3:
        return 0.0
    inner = (
        ink[1:-1, 1:-1] & ink[:-2, 1:-1] & ink[2:, 1:-1] & ink[1:-1, :-2] & ink[1:-1, 2:]
    ).sum()
    return float(ink.sum() / max(ink.sum() - inner, 1))


def read_page(number: int, image: Image.Image, settings: Settings, timeout: int) -> Page:
    """Read one page: its lines of words, noise left out."""
    gray, colour = prepare(image)
    gray_pixels, colour_pixels = np.asarray(gray), np.asarray(colour) > 0
    grouped: dict[tuple[str, str, str], list[Word]] = {}
    dropped = 0
    # the stamps are taken off the page (the black text over them stays): Tesseract would take
    # the part of the page they cover for a picture
    destamped = np.where(colour_pixels, 255, gray_pixels).astype(np.uint8)
    for row in _tesseract(Image.fromarray(destamped), settings, timeout):
        left, top, width, height = (int(row[name]) for name in _TSV_FIELDS)
        word = Word(row["text"].strip(), left, top, left + width, top + height, float(row["conf"]))
        stamped = colour_pixels[word.top : word.bottom, word.left : word.right].mean() > 0.25
        garbage = not word.letters and not set(word.text) <= PUNCTUATION
        if (stamped and word.confidence < STAMP_CONFIDENCE) or garbage:
            dropped += 1
            continue
        word.underline = _underline(gray_pixels, word)
        word.stroke = _stroke(gray_pixels, word)
        grouped.setdefault((row["block_num"], row["par_num"], row["line_num"]), []).append(word)
    kept, unsure = [], []
    for words in grouped.values():
        line = Line(_apart(sorted(words, key=lambda w: w.left)))
        dropped += len(words) - len(line.words)
        if not line.words:
            continue
        letters = sum(word.letters for word in line.words)
        on_stamp = colour_pixels[line.top : line.bottom, line.left : line.right].mean() > 0.1
        # a line of noise (a signature, a stamp, a punched hole) is little and unsure; in a line
        # of text, a word read with little confidence is kept, to be corrected
        sure = (
            line.confidence >= 70
            or (line.confidence >= 60 and letters >= 20)
            or (line.confidence >= 45 and letters >= 40)
        )
        if on_stamp and line.confidence < 85:
            dropped += len(line.words)
        elif letters and sure:
            line.words = [w for w in line.words if w.confidence >= MIN_CONFIDENCE or w.letters > 1]
            kept.append(line)
        elif letters >= 3 and line.confidence >= 15:  # kept if it lines up under the text
            unsure.append(line)
        else:
            dropped += len(line.words)
    kept += _under_text(unsure, kept)
    dropped += sum(len(line.words) for line in unsure if line not in kept)
    kept, outside = _inside(kept, gray.width)
    page = Page(number, gray.width, gray.height, _rows(kept), dropped=dropped + outside)
    if page.lines:  # specks: lines much smaller than the text
        typical = median(line.bottom - line.top for line in page.lines)
        small = [line for line in page.lines if line.bottom - line.top < 0.45 * typical]
        page.lines = [line for line in page.lines if line not in small]
        page.dropped += sum(len(line.words) for line in small)
    return page


def _apart(words: list[Word]) -> list[Word]:
    """The words of a line without the unsure ones standing apart from the text: what remains
    of a stamp or a signature next to the end of a line."""
    if len(words) < 2:
        return words
    height = median(word.height for word in words)
    groups, group = [], [words[0]]
    for word, after in zip(words, words[1:], strict=False):
        if after.left - word.right > 4 * height:
            groups.append(group)
            group = []
        group.append(after)
    groups.append(group)
    if len(groups) == 1:
        return words
    kept = []
    for group in groups:
        sure = sum(word.confidence for word in group) / len(group)
        letters = sum(word.letters for word in group)
        if letters >= 10 or (sure >= 60 and letters >= 3) or sure >= 90:
            kept.append(group)
    return [word for group in kept for word in group]


def _under_text(unsure: list[Line], text: list[Line]) -> list[Line]:
    """The unsure short lines that are the last line of a paragraph: one line under the text,
    where the text starts (a signature or a scribble does not line up with the text)."""
    if len(text) < 3:
        return []
    ordered = sorted(text, key=lambda line: line.top)
    steps = [b.top - a.top for a, b in zip(ordered, ordered[1:], strict=False) if b.top > a.top]
    step = median(steps) if steps else 0
    margin = float(np.percentile([line.left for line in text], 10))
    found = []
    for line in unsure:
        above = [other for other in ordered if 0 < line.top - other.top <= 1.3 * step]
        starts = (above[-1].left, margin) if above else ()
        if any(abs(line.left - start) <= 0.6 * step for start in starts):
            found.append(line)
    return found


def _inside(lines: list[Line], width: int) -> tuple[list[Line], int]:
    """The lines without what lies outside the text (a punched hole, the edge of the paper):
    words far to the left or right of where the confident lines of text start and end."""
    good = [line for line in lines if line.confidence >= 80 and len(line.words) >= 3]
    if len(good) < 3:
        return lines, 0
    start = float(np.percentile([line.left for line in good], 10)) - 0.03 * width
    end = float(np.percentile([line.right for line in good], 95)) + 0.03 * width
    kept, outside = [], 0
    for line in lines:
        words = [word for word in line.words if word.right > start and word.left < end]
        outside += len(line.words) - len(words)
        if words:
            line.words = words
            kept.append(line)
    return kept, outside


def _rows(lines: list[Line]) -> list[Line]:
    """Lines top to bottom; the lines Tesseract read apart side by side on the same row (two
    columns, a signature on each side) joined into one, a column break between them."""
    rows: list[Line] = []
    for line in sorted(lines, key=lambda line: line.top):
        if rows:
            last = rows[-1]
            overlap = min(last.bottom, line.bottom) - max(last.top, line.top)
            height = max(last.bottom - last.top, line.bottom - line.top)
            # side by side: the line lies between the words of the row, over none of them
            apart = all(line.left >= w.right or line.right <= w.left for w in last.words)
            if apart and overlap > 0.6 * height:
                column, columns = 0, {}  # the column of every word, the new line's last
                for index, word in enumerate(last.words):
                    columns[id(word)] = column
                    column += index in last.breaks
                columns.update({id(word): column + 1 for word in line.words})
                words = sorted(last.words + line.words, key=lambda word: word.left)
                last.breaks = {
                    index
                    for index, (word, after) in enumerate(zip(words, words[1:], strict=False))
                    if columns[id(word)] != columns[id(after)]
                }
                last.words = words
                continue
        rows.append(line)
    return rows


def read_pages(
    data: bytes, settings: Settings, timeout: int, workers: int | None = None
) -> list[Page]:
    """Read every page of a PDF, several side by side (pdfium draws them one at a time)."""
    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as exc:
        raise DocumentReadError(
            f"the PDF cannot be opened: damaged, or protected by a password ({exc})"
        ) from exc
    workers = workers or max(1, min(4, os.cpu_count() or 1))
    pages: list[Page] = []
    try:
        with ThreadPoolExecutor(workers) as pool:
            for start in range(0, len(document), workers):
                batch = []
                for index in range(start, min(start + workers, len(document))):
                    page = document[index]
                    batch.append((index + 1, page.render(scale=DPI / 72).to_pil()))
                    page.close()
                pages += pool.map(lambda job: read_page(*job, settings, timeout), batch)
    finally:
        document.close()
    return pages


# --------------------------------------------------------------------------- the layout


def _snap(size: float) -> float:
    return min(SIZES, key=lambda candidate: abs(candidate - size))


def _frame(page: Page) -> None:
    """Where the text of the page starts and ends (most lines start at the left margin; the
    long lines end at the right one)."""
    lefts = sorted(line.left for line in page.lines)
    page.left = float(np.percentile(lefts, 20))
    widest = max(line.right - line.left for line in page.lines)
    long = [line.right for line in page.lines if line.right - line.left > 0.6 * widest]
    page.right = float(np.percentile(long, 90))


def _pitch(pages: list[Page]) -> float:
    """The distance from one line to the next in the text, in pixels."""
    steps = [
        after.top - line.top
        for page in pages
        for line, after in zip(page.lines, page.lines[1:], strict=False)
        if after.top > line.top
    ]
    if not steps:
        return DPI * 13.8 / 72
    close = [step for step in steps if step <= 1.25 * float(np.percentile(steps, 30))]
    return float(median(close or steps))


def _measured(line: Line) -> list[Word]:
    """The words of a line sure enough to measure."""
    return [word for word in line.words if word.confidence >= 70 and word.letters >= 2]


def _sizes(pages: list[Page], body: float) -> None:
    """The size of every line: the text's, or another when its capitals (or, without capitals,
    its tallest letters) are clearly taller (a title) or smaller than those of the text."""
    lines = [line for page in pages for line in page.lines]
    capitals = [w.height for line in lines for w in _measured(line) if w.capitals] or [1]
    tallest = [
        float(np.percentile([w.height for w in _measured(line)], 75))
        for line in lines
        if len(_measured(line)) >= 5
    ] or [1.0]
    capital, tall = median(capitals), median(tallest)
    for line in lines:
        measured = _measured(line)
        caps = [word.height for word in measured if word.capitals]
        if caps:  # the smallest: a signature or a stamp across a word only makes it taller
            ratio, enough = min(caps) / capital, len(caps)
        elif measured:
            ratio = float(np.percentile([word.height for word in measured], 75)) / tall
            enough = len(measured)
        else:
            ratio, enough = 1.0, 0
        larger, smaller = ratio >= 1.12, ratio <= 0.82 and enough >= 4
        # the text keeps one size: only a title (capitals, bold) is set larger or smaller
        line.size = _snap(body * ratio) if (larger or smaller) and _title(line) else body


def _bold(pages: list[Page]) -> None:
    """Bold words: strokes thicker than those of the text of their page (a page scanned darker
    has thicker strokes), for the size of their letters. A word too short to measure follows the
    words around it."""

    def strokes(page: Page) -> list[float]:
        return [
            word.stroke / line.size
            for line in page.lines
            for word in line.words
            if word.letters >= 3 and word.stroke
        ]

    def threshold(values: list[float]) -> float:
        middle = median(values)
        spread = median(abs(value - middle) for value in values)
        return middle + max(BOLD_SPREAD * spread, BOLD_LEAST * middle)

    everything = [stroke for page in pages for stroke in strokes(page)]
    if not everything:
        return
    for page in pages:
        measured = strokes(page)
        bold = threshold(measured if len(measured) >= 30 else everything)
        for line in page.lines:
            for word in line.words:
                word.bold = bool(word.stroke) and word.stroke / line.size > bold
            for index, word in enumerate(line.words):
                if word.letters >= 3:
                    continue
                around = [
                    other.bold
                    for other in (
                        line.words[index - 1] if index else None,
                        line.words[index + 1] if index + 1 < len(line.words) else None,
                    )
                    if other is not None and other.letters >= 3
                ]
                if around and all(around) or not word.letters and around and any(around):
                    word.bold = True
                elif around and not any(around):
                    word.bold = False


def _paragraphs(page: Page, pitch: float, wide: float) -> list[Paragraph]:
    """The paragraphs of a page, from the place of its lines."""
    tolerance = 0.02 * (page.right - page.left)
    middle = (page.left + page.right) / 2

    def centred(line: Line) -> bool:
        """Short, and about as far from both margins (a title typed with spaces is a little
        off)."""
        before, after = line.left - page.left, page.right - line.right
        return (
            before > 2 * tolerance
            and after > tolerance
            and line.right - line.left < 0.8 * (page.right - page.left)
            and abs(before - after) < max(4 * tolerance, 0.3 * (before + after))
        )

    paragraphs: list[Paragraph] = []
    previous: Line | None = None
    for line in page.lines:
        columns = bool(line.columns(wide))
        head = paragraphs[-1].lines[0] if paragraphs else None
        # the next line of an item of a list starts under its text, not under the dash
        hanging = (
            previous is not None
            and head is not None
            and _bullet(head)
            and not _bullet(line)
            and abs(line.left - head.words[1].left) <= tolerance
            and line.top - previous.top <= 1.45 * pitch
        )
        starts = (
            previous is None
            or not hanging
            and (
                line.top - previous.top > 1.45 * pitch  # a blank line before it
                or previous.right < page.right - 3 * tolerance  # the line before ended short
                or centred(line)
                or centred(previous)
                or columns
                or bool(previous.columns(wide))
                or (
                    line.left > page.left + tolerance and abs(line.left - previous.left) > tolerance
                )
                or _bullet(line)  # an item of a list
            )
        )
        if starts:
            paragraphs.append(Paragraph([line], page))
        else:
            paragraphs[-1].lines.append(line)
        previous = line

    justified = 0
    for index, paragraph in enumerate(paragraphs):
        first, lines = paragraph.lines[0], paragraph.lines
        if first.columns(wide):
            paragraph.tabs = [first.words[gap + 1].left - page.left for gap in first.columns(wide)]
            paragraph.first_indent = max(0.0, first.left - page.left)
        elif all(centred(line) for line in lines) or (
            len(lines) == 1
            and first.left - page.left > 2 * tolerance
            and abs(first.center - middle) < 0.15 * (page.right - page.left)
            and _title(first)
        ):
            paragraph.alignment = "center"
        elif first.left > middle and abs(first.right - page.right) < tolerance:  # a date
            paragraph.alignment = "right"
        else:
            full = [line.right >= page.right - 1.5 * tolerance for line in lines[:-1]]
            if full and all(full):
                paragraph.alignment = "justify"
                justified += 1
            # an item of a list: the dash at its place, the text (and its next lines) at the
            # indent; a paragraph that only starts with a dash goes on at the margin
            if _bullet(first) and all(
                abs(line.left - first.words[1].left) <= tolerance for line in lines[1:]
            ):
                paragraph.bullet = True
                paragraph.left_indent = first.words[1].left - page.left
            else:
                others = [line.left for line in lines[1:]]
                if others and median(others) - page.left > tolerance:
                    paragraph.left_indent = median(others) - page.left
            indent = first.left - page.left - paragraph.left_indent
            if abs(indent) > tolerance or paragraph.bullet:  # an item hangs from its dash
                paragraph.first_indent = indent
        if index:
            gap = first.top - paragraphs[index - 1].lines[-1].top - pitch
            paragraph.space_before = gap if gap > 0.3 * pitch else 0.0
    if justified:  # a one-line paragraph of a justified text is justified too
        for paragraph in paragraphs:
            single = len(paragraph.lines) == 1
            if single and paragraph.alignment == "left" and not paragraph.tabs:
                paragraph.alignment = "justify"
    return paragraphs


def _bullet(line: Line) -> bool:
    """A line that starts an item of a list: a dash, then its text."""
    return len(line.words) > 1 and _BULLET.match(line.words[0].text) is not None


def _title(line: Line) -> bool:
    """A line in capitals, or in bold: standing alone and indented, it is a centred title."""
    letters = [char for char in line.text if char.isalpha()]
    capitals = sum(char.isupper() for char in letters) >= 0.7 * len(letters) if letters else False
    return capitals or all(word.bold for word in line.words if word.letters)


def _even_indents(paragraphs: list[Paragraph]) -> None:
    """The indents of a typed document are a few: indents within 5 points of each other (a
    page scanned a little warped) take their common value."""
    for name in ("first_indent", "left_indent"):
        for sign in (1, -1):  # a hanging indent is negative
            values = sorted(
                sign * getattr(p, name) for p in paragraphs if sign * getattr(p, name) > 0
            )
            groups: list[list[float]] = []
            for value in values:
                if groups and _pt(value - groups[-1][0]) <= 5:
                    groups[-1].append(value)
                else:
                    groups.append([value])
            common = {sign * value: sign * median(group) for group in groups for value in group}
            for paragraph in paragraphs:
                value = getattr(paragraph, name)
                if value in common:
                    setattr(paragraph, name, common[value])


def _continues(last: Paragraph, first: Paragraph) -> bool:
    """The last paragraph of a page goes on at the top of the next one."""
    end, start = last.lines[-1], first.lines[0]
    tolerance = 0.02 * (first.page.right - first.page.left)
    return (
        last.alignment == "justify"
        and not last.tabs
        and end.right >= last.page.right - tolerance
        and first.alignment in ("justify", "left")
        and not first.tabs
        and start.left <= first.page.left + tolerance
    )


_BULLET = re.compile(r"^[-–—•·*]$")
_PAGE_NUMBER = re.compile(r"^(pag(ina|e)?\.?\s*)?\d{1,3}(\s*(/|din|of|-)\s*\d{1,3})?$", re.I)


def layout(pages: list[Page]) -> tuple[list[Paragraph], dict[str, float]]:
    """The paragraphs of the document, and its page: size of the text (pt), line pitch and
    margins (px)."""
    pages = [page for page in pages if page.lines]
    if not pages:
        return [], {}
    for page in pages:
        _frame(page)
    full = [page for page in pages if len(page.lines) >= 8] or pages
    left, right = median(p.left for p in full), median(p.right for p in full)
    for page in pages:
        if len(page.lines) < 8:  # a page with little text: the margins of the others
            page.left, page.right = left, right
    pitch = _pitch(pages)
    body = _snap(_pt(pitch) / SINGLE)
    for line in (line for page in pages for line in page.lines):
        line.size = body
    _bold(pages)  # a title is in bold: bold first at the size of the text, then at its own
    _sizes(pages, body)
    _bold(pages)
    # page numbers, alone on a line at the top or the bottom of the page, are not text
    numbered = 0
    for page in pages:
        ends = [page.lines[0], page.lines[-1]] if len(page.lines) > 1 else []
        for line in ends:
            # a stray letter next to the number is a speck read as a letter
            kept = [w.text for w in line.words if not (len(w.text) == 1 and w.text.isalpha())]
            if kept and _PAGE_NUMBER.match(" ".join(kept)) and line in page.lines:
                page.lines.remove(line)
                numbered += 1
    top = median(page.lines[0].top for page in pages if page.lines)
    # the bottom margin: room for the fullest page (every page then ends where the scan's ends);
    # the last page ends where its text ends, so a single page gets the margin of its sides
    bottoms = [page.lines[-1].bottom for page in pages[:-1] if page.lines]
    bottom = max(bottoms) + 0.3 * pitch if bottoms else pages[0].height - left
    wide = 4 * pitch  # a jump this long within a line is a column (a tab)
    paragraphs: list[Paragraph] = []
    for page in pages:
        if not page.lines:
            continue
        found = _paragraphs(page, pitch, wide)
        if paragraphs:  # a page of the scan is a page of the document (1:1)
            last = paragraphs[-1]
            if _continues(last, found[0]):  # unless a paragraph goes on from one to the next
                last.lines += found.pop(0).lines
            else:
                found[0].page_break = True
        if found:
            lowered = found[0].lines[0].top - top
            if (not paragraphs or found[0].page_break) and lowered > 0.5 * pitch:
                found[0].space_before = lowered
        paragraphs += found
    _even_indents(paragraphs)
    page = {
        "width": pages[0].width,
        "height": pages[0].height,
        "left": left,
        "right": pages[0].width - right,
        "top": max(0.0, top - 0.25 * pitch),
        "bottom": max(0.0, pages[0].height - bottom),
        "pitch": pitch,
        "size": body,
        "page_numbers": numbered >= max(2, len(pages) // 2),
    }
    return paragraphs, page


# --------------------------------------------------------------------------- the Word document


def _fonts(element: object, size: float | None = None) -> None:
    """Times New Roman for every script, in Romanian (the spelling checker's language)."""
    properties = element.get_or_add_rPr()  # type: ignore[attr-defined]
    fonts = properties.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        properties.insert(0, fonts)
    for name in list(fonts.attrib):
        del fonts.attrib[name]  # the theme's fonts would win over the font given
    for script in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(script), FONT)
    language = properties.find(qn("w:lang"))
    if language is None:
        language = OxmlElement("w:lang")
        properties.append(language)
    language.set(qn("w:val"), "ro-RO")
    if size:
        for tag in ("w:sz", "w:szCs"):
            element_size = properties.find(qn(tag))
            if element_size is None:
                element_size = OxmlElement(tag)
                properties.append(element_size)
            element_size.set(qn("w:val"), str(round(size * 2)))


def _join(paragraph: Paragraph, wide: float) -> Iterator[tuple[str, Word]]:
    """The words of a paragraph with what goes before each: nothing, a space, or a tab (a
    column, the text after the dash of a list); a word cut by a hyphen at the end of a line is
    joined to the rest of it."""
    lines = paragraph.lines
    for number, line in enumerate(lines):
        gaps = set(line.columns(wide)) | ({0} if paragraph.bullet and not number else set())
        for index, word in enumerate(line.words):
            if index:
                before = "\t" if index - 1 in gaps else " "
            elif number:
                cut = lines[number - 1].words[-1].text
                before = "" if len(cut) > 1 and cut.endswith("-") else " "
            else:
                before = ""
            yield before, word


def write_docx(paragraphs: list[Paragraph], page: dict[str, float], title: str = "") -> bytes:
    document = Document()
    section = document.sections[0]
    section.start_type = WD_SECTION.NEW_PAGE
    emu = lambda pixels: Emu(round(pixels / DPI * 914400))  # noqa: E731
    section.page_width, section.page_height = emu(page["width"]), emu(page["height"])
    section.left_margin, section.right_margin = emu(page["left"]), emu(page["right"])
    section.top_margin, section.bottom_margin = emu(page["top"]), emu(page["bottom"])
    body, pitch = page["size"], _pt(page["pitch"])

    normal = document.styles["Normal"]
    _fonts(normal.element, body)
    spacing = normal.paragraph_format
    spacing.space_before = spacing.space_after = Pt(0)
    if abs(pitch / (body * SINGLE) - 1) < 0.01:
        spacing.line_spacing_rule = WD_LINE_SPACING.SINGLE
    else:  # the line pitch of the scan, as a multiple (a larger title is not cut off)
        spacing.line_spacing = round(pitch / (body * SINGLE), 2)

    alignments = {
        "left": WD_ALIGN_PARAGRAPH.LEFT,
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
        "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
    }
    wide = 4 * page["pitch"]
    for paragraph in paragraphs:
        written = document.add_paragraph()
        layout_ = written.paragraph_format
        layout_.alignment = alignments[paragraph.alignment]
        if paragraph.left_indent:
            layout_.left_indent = Pt(round(_pt(paragraph.left_indent), 1))
        if paragraph.first_indent:
            layout_.first_line_indent = Pt(round(_pt(paragraph.first_indent), 1))
        if paragraph.space_before:
            layout_.space_before = Pt(round(_pt(paragraph.space_before)))
        if paragraph.page_break:
            layout_.page_break_before = True
        for stop in paragraph.tabs:
            layout_.tab_stops.add_tab_stop(Pt(round(_pt(stop), 1)))
        bold_line = all(word.bold for line in paragraph.lines for word in line.words)
        if paragraph.alignment == "center" or (bold_line and len(paragraph.lines) <= 2):
            layout_.keep_with_next = True  # a title stays with what it announces
        size = paragraph.size
        runs: list[tuple[str, bool, bool]] = []  # text, bold, underlined
        for before, word in _join(paragraph, wide):
            style = (word.bold, bool(word.underline))
            if runs and runs[-1][1:] == style:
                runs[-1] = (runs[-1][0] + before + word.text, *style)
            elif runs and before == " " and runs[-1][2] and style[1]:
                runs[-1] = (runs[-1][0] + before, *runs[-1][1:])  # the rule goes on under the space
                runs.append((word.text, *style))
            else:
                runs.append((before + word.text, *style))
        for text, bold, underlined in runs:
            run = written.add_run(text)
            run.bold = bold or None
            run.underline = underlined or None
            if size != body:
                run.font.size = Pt(size)

    if page.get("page_numbers"):  # the page number, centred at the bottom, as Word writes it
        footer = section.footer.paragraphs[0]
        footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
        number = OxmlElement("w:fldSimple")
        number.set(qn("w:instr"), "PAGE")
        footer._p.append(number)
    properties = document.core_properties
    properties.author, properties.title, properties.comments = "", title[:200], ""
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def ocr_missing(settings: Settings) -> str | None:
    if not tesseract_available(settings.tesseract_cmd):
        return "Tesseract OCR is not installed (apt install tesseract-ocr tesseract-ocr-ron)"
    return None


def scan_to_word(data: bytes, settings: Settings) -> OcrResult:
    """Read a scanned PDF and write it as a Word document that can be edited."""
    pages = read_pages(data, settings, settings.convert_timeout)
    paragraphs, page = layout(pages)
    if not paragraphs:
        raise ConversionError("no text was found on the pages, even with OCR")
    title = next((p.lines[0].text for p in paragraphs if p.alignment == "center"), "")
    words = [word for paragraph in paragraphs for line in paragraph.lines for word in line.words]
    letters = sum(max(word.letters, 1) for word in words)
    confidence = sum(word.confidence * max(word.letters, 1) for word in words) / letters
    uncertain = [word.text for word in words if word.confidence < 60 and word.letters]
    return OcrResult(
        write_docx(paragraphs, page, title),
        len(pages),
        len(words),
        confidence,
        uncertain,
        sum(page.dropped for page in pages),
    )
