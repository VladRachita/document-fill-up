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
marks         what is not text (a stamp, a signature, handwriting) is kept as a picture laid where
              it is on the page, its paper transparent: over the text, as on the scan; handwriting
              is not read (Tesseract reads print)
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
from scipy import ndimage

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
# A mark (a stamp, a signature, handwriting): coloured ink, or ink darker than MARK_INK where no
# word was read (show-through stays lighter); its strokes are taken down to MARK_LIGHT.
MARK_INK, MARK_LIGHT = 170, 225
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
class Mark:
    """What is not text on a page (a stamp, a signature, handwriting): its picture, the paper
    transparent, and where it is on the page (pixels at DPI)."""

    left: int
    top: int
    picture: Image.Image  # RGBA


@dataclass
class Page:
    number: int
    width: int  # in pixels at DPI
    height: int
    lines: list[Line]
    left: float = 0.0  # where the text starts and ends: the margins of the page
    right: float = 0.0
    dropped: int = 0  # words left out as noise
    marks: list[Mark] = field(default_factory=list)


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
        return median(line.size for line in self.lines) if self.lines else 0.0


@dataclass
class OcrResult:
    """The Word document, and how sure the reading is."""

    data: bytes
    pages: int
    words: int
    confidence: float  # mean confidence of the words, weighted by their letters
    uncertain: list[str]  # words read with less than 60% confidence
    dropped: int  # words left out as noise (stamps, signatures)
    marks: int = 0  # stamps, signatures, handwriting kept as pictures


# --------------------------------------------------------------------------- the picture


def _paper(gray: Image.Image) -> np.ndarray:
    """The shade of the paper around each point: the brightest within ~5 mm."""
    small = gray.reduce(8).filter(ImageFilter.MaxFilter(9))
    return np.maximum(np.asarray(small.resize(gray.size, Image.Resampling.BILINEAR), np.float32), 1)


def _even_out(gray: Image.Image, paper: np.ndarray | None = None) -> Image.Image:
    """The paper evened out to white: each point divided by the shade of the paper around it.
    A darker edge or the show-through of the other side fades; the ink stays dark."""
    paper = _paper(gray) if paper is None else paper
    evened = np.asarray(gray, np.float32) / paper * 255
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


def prepare(image: Image.Image) -> tuple[Image.Image, Image.Image, Image.Image]:
    """The page to read (grey, evened out, straightened), its coloured ink, and its colours
    (evened out and straightened too: the pictures of its stamps and signatures)."""
    rgb = image.convert("RGB")
    gray = rgb.convert("L")
    paper = _paper(gray)
    evened = np.asarray(rgb, np.float32) / paper[..., None] * 255
    colours = Image.fromarray(np.clip(evened, 0, 255).astype(np.uint8))
    gray, colour = _even_out(gray, paper), _colour(rgb)
    angle = _skew(gray)
    if abs(angle) >= 0.1:
        gray = gray.rotate(angle, Image.Resampling.BICUBIC, fillcolor=255)
        colour = colour.rotate(angle, Image.Resampling.NEAREST, fillcolor=0)
        colours = colours.rotate(angle, Image.Resampling.BICUBIC, fillcolor=(255, 255, 255))
    return gray, colour, colours


def clean_page(image: Image.Image, stamps: bool = False) -> Image.Image:
    """A scanned page as it is compared with the document read from it: the paper evened out,
    the page straightened and, unless they were kept (``stamps``), the stamps taken off."""
    gray, colour, _ = prepare(image)
    if stamps:
        return gray
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
            raise ConversionError(f"OCR nu a terminat în {timeout} s") from exc
        except OSError as exc:
            raise ConversionError(f"Tesseract nu a putut fi pornit ({exc})") from exc
    if completed.returncode != 0:
        raise ConversionError(f"Tesseract nu a putut citi pagina ({completed.stderr[-300:]})")
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
    """Read one page: its lines of words, noise left out, and (``ocr_keep_marks``) the marks
    that are not text."""
    gray, colour, colours = prepare(image)
    gray_pixels, colour_pixels = np.asarray(gray), np.asarray(colour) > 0
    grouped: dict[tuple[str, str, str], list[Word]] = {}
    dropped = 0
    # the stamps are taken off the page (the black text over them stays): Tesseract would take
    # the part of the page they cover for a picture
    destamped = np.where(colour_pixels, 255, gray_pixels).astype(np.uint8)
    rows = _tesseract(Image.fromarray(destamped), settings, timeout)
    missed = _missed(gray_pixels, colour_pixels, rows)
    if missed is not None:  # read again, alone on the page: the print Tesseract passed over
        again = _tesseract(missed, settings, timeout)
        rows += [{**row, "block_num": f"again {row['block_num']}"} for row in again]
    for row in rows:
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
            or (line.confidence >= 45 and letters >= 20)  # a long line: one word misread
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
    if settings.ocr_keep_marks:
        page.marks = marks(gray_pixels, np.asarray(colours), page.lines)
    return page


def _missed(gray: np.ndarray, stamps: np.ndarray, rows: list[dict[str, str]]) -> Image.Image | None:
    """The print no word was read from, alone on a white page; None when there is little.
    Tesseract takes a part of a page for a picture at times (the remains of a stamp, the other
    side showing through, next to a heading) and reads nothing in it."""
    covered = np.zeros(gray.shape, bool)
    for row in rows:  # and next to a word: the end of a word cut short (a bent line), no line
        left, top, width, height = (int(row[name]) for name in _TSV_FIELDS)
        reach = 6 + int(1.5 * height)
        covered[max(0, top - 6) : top + height + 6, max(0, left - reach) : left + width + reach] = (
            True
        )
    ink = (gray < 140) & ~covered & ~stamps
    if ink.sum() < 1500:
        return None
    alone = ndimage.binary_dilation(ink, iterations=8) & ~covered
    return Image.fromarray(np.where(alone, gray, 255).astype(np.uint8))


def marks(gray: np.ndarray, colours: np.ndarray, lines: list[Line]) -> list[Mark]:
    """What is not text on a page: a stamp or a coloured pen (blue, violet, red ink), a signature
    or handwriting (dark ink where no word was read). Strokes ~2 mm apart are one mark; the two
    kinds are found apart, so that print never rides along with a stamp. Left out: specks, the
    print the OCR did not take into a word (dark ink along the lines of text: a word missed, the
    end of a line bent on a warped page), the coloured fringe a camera leaves on print, a punched
    hole or the edge of the paper (grey ink at the edge of the page)."""
    height, width = gray.shape
    pixels = colours.astype(np.int16)
    red, green, blue = pixels[..., 0], pixels[..., 1], pixels[..., 2]
    bluish = blue - np.maximum(red, green)
    reddish = red - np.maximum(green, blue)
    tall = median(line.bottom - line.top for line in lines) if lines else 60
    words = np.zeros(gray.shape, bool)
    bands = np.zeros(gray.shape, bool)  # the lines of text, half a line above and below
    for line in lines:
        reach = int(tall / 2)
        bands[max(0, line.top - reach) : line.bottom + reach, :width] = True
        for word in line.words:  # a little round each word: its ink a box can cut on a bent page
            words[
                max(0, word.top - 6) : word.bottom + 6, max(0, word.left - 6) : word.right + 6
            ] = True
    # coloured ink, not the coloured edge of a black stroke (a camera's fringe)
    print_ink = ndimage.binary_dilation((gray < 150) & (bluish < 15) & (reddish < 15), iterations=2)
    coloured = ((bluish > 28) | (reddish > 40)) & (gray < 235) & ~print_ink
    coloured = _off_words(coloured, words, bluish - reddish)
    pale = ((bluish > 10) | (reddish > 18)) & (gray < 245)  # the lighter parts of a stamp
    dark = _off_print((gray < MARK_INK) & ~words & ~coloured, bands, tall)
    edge = 0.04 * width
    found, taken = [], np.zeros(gray.shape, bool)
    for box in _groups(coloured):
        region = (slice(box[1], box[3]), slice(box[0], box[2]))
        ink = int(coloured[region].sum())
        on_words = (coloured[region] & words[region]).sum() > 0.6 * max(ink, 1)
        if ink < 300 or (on_words and box[3] - box[1] < 2.2 * tall):
            continue  # a speck, or the fringe of print: a stamp spreads off the words
        if box[2] < edge or box[0] > width - edge:
            continue  # the coloured edge of the paper
        # the pale parts of a stamp lie next to its strong colour (show-through lies apart); over
        # a word, only the clearly coloured ink: the paler is the fringe of the print
        near = ndimage.binary_dilation(coloured[region], iterations=8)
        lighter = np.zeros_like(pale)
        lighter[region] = pale[region] & ~words[region] & near
        mark = _picture(gray, colours, coloured, lighter, box, taken)
        if mark:
            found.append(_take(mark, taken))
    light = (gray < MARK_LIGHT) & ~words  # the strokes of a pen, however lightly pressed
    for box in _groups(dark):
        left, top, right, bottom = box
        region = (slice(top, bottom), slice(left, right))
        ink = int(dark[region].sum())
        if ink < 150 or max(right - left, bottom - top) < 0.8 * tall:
            continue  # a speck
        on_lines = (dark[region] & bands[region]).sum() / ink
        if on_lines > 0.8 or (on_lines > 0.6 and bottom - top < 2.2 * tall):
            continue  # print the OCR did not take into a word: no mark
        if right < edge or left > width - edge:
            continue  # a punched hole, the edge of the paper
        mark = _picture(gray, colours, dark, light, box, taken, least=800)  # not a speck
        if mark:
            found.append(_take(mark, taken))
    return found


def _take(mark: Mark, taken: np.ndarray) -> Mark:
    """The strokes of ``mark`` marked as taken."""
    width, height = mark.picture.size
    taken[mark.top : mark.top + height, mark.left : mark.left + width] |= (
        np.asarray(mark.picture)[..., 3] > 0
    )
    return mark


def _off_words(coloured: np.ndarray, words: np.ndarray, bluish: np.ndarray) -> np.ndarray:
    """Coloured ink without the tint or the fringe of printed letters (a camera's): what lies on
    the words is kept where it goes on from ink of the same hue off the words (a blue stamp over
    the text; not the red tint of the print it crosses)."""
    blue = bluish > 0
    kept = np.zeros_like(coloured)
    for hue in (coloured & blue, coloured & ~blue):
        off = hue & ~words
        labels, count = ndimage.label(off, structure=np.ones((3, 3)))
        if not count:
            continue
        size = ndimage.sum(off, labels, np.arange(1, count + 1))
        strong = np.concatenate([[False], size >= 20])[labels]  # not a speck
        kept |= ndimage.binary_propagation(strong, structure=np.ones((3, 3)), mask=hue)
    return kept


def _off_print(dark: np.ndarray, bands: np.ndarray, tall: float) -> np.ndarray:
    """Dark ink without the bits of print the words did not take in: strokes smaller than a
    line, lying on a line of text (a dot, the end of a bent line). A signature's strokes are
    larger, or lie off the lines."""
    labels, count = ndimage.label(dark)
    if not count:
        return dark
    index = np.arange(1, count + 1)
    size = ndimage.sum(dark, labels, index)
    on_lines = ndimage.sum(bands, labels, index) / np.maximum(size, 1)
    heights = np.array([found[0].stop - found[0].start for found in ndimage.find_objects(labels)])
    keep = np.concatenate([[False], (heights >= 0.8 * tall) | (on_lines < 0.5)])
    return keep[labels]


def _groups(ink: np.ndarray) -> list[tuple[int, int, int, int]]:
    """The boxes of the groups of ink: strokes ~2 mm apart are one group (found on a coarser grid:
    quicker)."""
    step = 4
    height, width = ink.shape
    rows, columns = height // step, width // step
    grid = ink[: rows * step, : columns * step].reshape(rows, step, columns, step).any(axis=(1, 3))
    labels, _ = ndimage.label(ndimage.binary_dilation(grid, iterations=6))
    return _merged(
        [
            (
                found[1].start * step,
                found[0].start * step,
                found[1].stop * step,
                found[0].stop * step,
            )
            for found in ndimage.find_objects(labels)
        ]
    )


def _merged(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Boxes that overlap joined into one (the parts of a stamp, a signature over a stamp)."""
    boxes = list(boxes)
    joined = True
    while joined:
        joined, result = False, []
        for box in boxes:
            for index, other in enumerate(result):
                if (
                    box[0] < other[2]
                    and other[0] < box[2]
                    and box[1] < other[3]
                    and other[1] < box[3]
                ):
                    result[index] = (
                        min(box[0], other[0]),
                        min(box[1], other[1]),
                        max(box[2], other[2]),
                        max(box[3], other[3]),
                    )
                    joined = True
                    break
            else:
                result.append(box)
        boxes = result
    return boxes


def _picture(
    gray: np.ndarray,
    colours: np.ndarray,
    seeds: np.ndarray,
    strokes: np.ndarray,
    box: tuple[int, int, int, int],
    taken: np.ndarray,
    least: int = 1,
) -> Mark | None:
    """The picture of a mark: every stroke that reaches its ink (``seeds``), however light (a pen
    pressed lightly, the pale part of a stamp), in its colour, the paper transparent. The strokes
    another mark took (``taken``) are left to it. None when less than ``least`` pixels of ink
    (all its strokes counted) or nothing is left."""
    height, width = gray.shape
    reach = 100  # ~1 cm: a light stroke leads on past the dark ones (the tail of a signature)
    left, top = max(0, box[0] - reach), max(0, box[1] - reach)
    right, bottom = min(width, box[2] + reach), min(height, box[3] + reach)
    region = (slice(top, bottom), slice(left, right))
    ink = strokes[region] | seeds[region]
    pieces, _ = ndimage.label(ink, structure=np.ones((3, 3)))
    inside = np.zeros(ink.shape, bool)
    inside[box[1] - top : box[3] - top, box[0] - left : box[2] - left] = True
    touched = np.unique(pieces[seeds[region] & inside])
    kept = np.isin(pieces, touched[touched > 0])
    rows, columns = np.nonzero(kept)
    if not rows.size:
        return None
    # the picture as large as the strokes, a little paper round them
    first, last = max(0, rows.min() - 4), rows.max() + 5
    start, stop = max(0, columns.min() - 4), columns.max() + 5
    kept = kept[first:last, start:stop]
    top, left = top + first, left + start
    region = (slice(top, top + kept.shape[0]), slice(left, left + kept.shape[1]))
    shade = gray[region].astype(np.float32)
    alpha = np.clip((250 - shade) / 190, 0, 1) * kept
    if (alpha > 0.25).sum() < least:
        return None
    alpha *= ~taken[region]
    if not alpha.any():
        return None
    opaque = alpha[..., None]
    # the colour of the ink itself: a light stroke is the ink, half transparent over the paper
    pure = (colours[region].astype(np.float32) - (1 - opaque) * 255) / np.maximum(opaque, 1e-3)
    picture = np.dstack([np.clip(pure, 0, 255), alpha * 255]).astype(np.uint8)
    return Mark(left, top, Image.fromarray(picture, "RGBA"))


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
    columns, a signature on each side) joined into one, a column break between them. A line it
    cut in two (the end of a line bent on a warped page) is joined as one, a word it cut in two
    as one word."""
    rows: list[Line] = []
    for line in sorted(lines, key=lambda line: line.top):
        if rows:
            last = rows[-1]
            # the nearest words of the two: a bent line is taller than its words
            gap, word, other = min(
                (
                    (max(word.left - other.right, other.left - word.right), word, other)
                    for word in last.words
                    for other in line.words
                ),
                key=lambda pair: pair[0],
            )
            overlap = min(word.bottom, other.bottom) - max(word.top, other.top)
            height = max(word.height, other.height)
            # side by side: the line lies between the words of the row, over none of them
            apart = all(line.left >= w.right or line.right <= w.left for w in last.words)
            if apart and overlap > 0.6 * height:
                column, columns = 0, {}  # the column of every word, the new line's last
                for index, found in enumerate(last.words):
                    columns[id(found)] = column
                    column += index in last.breaks
                joined = columns[id(word)] if gap < height else column + 1
                columns.update({id(found): joined for found in line.words})
                words = sorted(last.words + line.words, key=lambda found: found.left)
                if 0 <= gap < 0.2 * height:  # closer than a space: one word
                    first, second = sorted((word, other), key=lambda found: found.left)
                    first.text += second.text
                    first.right, first.top = second.right, min(first.top, second.top)
                    first.bottom = max(first.bottom, second.bottom)
                    first.confidence = min(first.confidence, second.confidence)
                    words.remove(second)
                last.breaks = {
                    index
                    for index, (found, after) in enumerate(zip(words, words[1:], strict=False))
                    if columns[id(found)] != columns[id(after)]
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
            f"PDF-ul nu poate fi deschis: este deteriorat sau protejat cu parolă ({exc})"
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
    margins (px). A page with marks but no text (a page of signatures) is an empty paragraph,
    to keep its page and its marks."""
    every = pages
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
    for page in every:
        if not page.lines:
            if page.marks:
                paragraphs.append(Paragraph([], page, page_break=bool(paragraphs)))
            continue
        found = _paragraphs(page, pitch, wide)
        if paragraphs:  # a page of the scan is a page of the document (1:1)
            last = paragraphs[-1]
            if last.lines and _continues(last, found[0]):  # unless a paragraph goes on
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


def write_docx(
    paragraphs: list[Paragraph],
    page: dict[str, float],
    title: str = "",
    pages: list[Page] | None = None,
) -> bytes:
    """The Word document: its paragraphs and, laid where they are, the marks of ``pages``."""
    pages = pages or []
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
    written_for: dict[int, object] = {}  # id of a Paragraph -> the paragraph written for it
    for paragraph in paragraphs:
        written = document.add_paragraph()
        written_for[id(paragraph)] = written
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

    _lay_marks(paragraphs, pages, written_for)
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


def _lay_marks(
    paragraphs: list[Paragraph], pages: list[Page], written_for: dict[int, object]
) -> None:
    """Lay the marks of every page (stamps, signatures) where they are on it: pictures floating
    over the text, held by the first paragraph that starts on that page (or, when all its text
    goes on from the page before, by that paragraph)."""
    number = 0
    for page in pages:
        if not page.marks:
            continue
        lines = {id(line) for line in page.lines}
        holder = next((p for p in paragraphs if p.page is page), None) or next(
            (p for p in paragraphs if any(id(line) in lines for line in p.lines)), None
        )
        if holder is None:
            continue
        for mark in page.marks:
            number += 1
            _float(written_for[id(holder)], mark, number)


def _float(paragraph: object, mark: Mark, number: int) -> None:
    """A picture laid at a place of the page, over the text (Word: in front of text)."""
    emu = lambda pixels: str(round(pixels / DPI * 914400))  # noqa: E731
    buffer = io.BytesIO()
    mark.picture.save(buffer, format="PNG", optimize=True)
    buffer.seek(0)
    run = paragraph.add_run()  # type: ignore[attr-defined]
    run.add_picture(
        buffer,
        width=Emu(int(emu(mark.picture.width))),
        height=Emu(int(emu(mark.picture.height))),
    )
    inline = run._r.find(".//" + qn("wp:inline"))
    anchor = OxmlElement("wp:anchor")
    for name, value in {
        "distT": "0",
        "distB": "0",
        "distL": "0",
        "distR": "0",
        "simplePos": "0",
        "relativeHeight": str(251658240 + number),
        "behindDoc": "0",
        "locked": "0",
        "layoutInCell": "1",
        "allowOverlap": "1",
    }.items():
        anchor.set(name, value)
    simple = OxmlElement("wp:simplePos")
    simple.set("x", "0")
    simple.set("y", "0")
    anchor.append(simple)
    for axis, pixels in (("H", mark.left), ("V", mark.top)):
        position = OxmlElement(f"wp:position{axis}")
        position.set("relativeFrom", "page")
        offset = OxmlElement("wp:posOffset")
        offset.text = emu(pixels)
        position.append(offset)
        anchor.append(position)
    anchor.append(inline.find(qn("wp:extent")))
    effect = OxmlElement("wp:effectExtent")
    for side in "ltrb":
        effect.set(side, "0")
    anchor.append(effect)
    anchor.append(OxmlElement("wp:wrapNone"))
    for tag in ("wp:docPr", "wp:cNvGraphicFramePr", "a:graphic"):
        anchor.append(inline.find(qn(tag)))
    inline.getparent().replace(inline, anchor)
    # at the start of the paragraph: a paragraph going on to the next page holds it on its first
    element = paragraph._p  # type: ignore[attr-defined]
    element.remove(run._r)
    properties = element.find(qn("w:pPr"))
    element.insert(0 if properties is None else 1, run._r)


def ocr_missing(settings: Settings) -> str | None:
    if not tesseract_available(settings.tesseract_cmd):
        return "Tesseract OCR nu este instalat (apt install tesseract-ocr tesseract-ocr-ron)"
    return None


def scan_to_word(data: bytes, settings: Settings) -> OcrResult:
    """Read a scanned PDF and write it as a Word document that can be edited."""
    pages = read_pages(data, settings, settings.convert_timeout)
    paragraphs, page = layout(pages)
    if not paragraphs:
        raise ConversionError("nu s-a găsit text pe pagini, nici prin OCR")
    title = next((p.lines[0].text for p in paragraphs if p.alignment == "center"), "")
    words = [word for paragraph in paragraphs for line in paragraph.lines for word in line.words]
    letters = sum(max(word.letters, 1) for word in words)
    confidence = sum(word.confidence * max(word.letters, 1) for word in words) / letters
    uncertain = [word.text for word in words if word.confidence < 60 and word.letters]
    return OcrResult(
        write_docx(paragraphs, page, title, pages),
        len(pages),
        len(words),
        confidence,
        uncertain,
        sum(page.dropped for page in pages),
        sum(len(page.marks) for page in pages),
    )
