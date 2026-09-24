"""Render a filled text standard document to PDF with ReportLab.

The layout follows the stored text line by line: ``# Heading``, ``## Sub heading``, ``---`` for
a horizontal rule, blank lines separate paragraphs and single line breaks are preserved.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Flowable, HRFlowable, Paragraph, SimpleDocTemplate, Spacer

from docfill.config import Settings, get_settings
from docfill.templates.markup import classify_line


@lru_cache
def _register_fonts(font_path: str | None) -> tuple[str, str]:
    """Register a TrueType font (for full unicode support) or fall back to Helvetica."""
    if not font_path:
        return "Helvetica", "Helvetica-Bold"
    regular = Path(font_path)
    pdfmetrics.registerFont(TTFont("DocFill", str(regular)))
    bold_name = "DocFill"
    for candidate in (
        regular.with_name(regular.stem + "-Bold" + regular.suffix),
        regular.with_name(regular.stem + "bd" + regular.suffix),
    ):
        if candidate.is_file():
            pdfmetrics.registerFont(TTFont("DocFill-Bold", str(candidate)))
            bold_name = "DocFill-Bold"
            break
    return "DocFill", bold_name


def _styles(regular: str, bold: str) -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle(
            "h1",
            parent=base["Title"],
            fontName=bold,
            fontSize=16,
            leading=20,
            alignment=TA_CENTER,
            spaceAfter=12,
        ),
        "h2": ParagraphStyle(
            "h2",
            parent=base["Heading2"],
            fontName=bold,
            fontSize=12.5,
            leading=16,
            spaceBefore=8,
            spaceAfter=6,
        ),
        "body": ParagraphStyle(
            "body",
            parent=base["BodyText"],
            fontName=regular,
            fontSize=11,
            leading=15,
            spaceAfter=8,
        ),
    }


def _markup(line: str) -> str:
    text = escape(line.rstrip())
    indent = len(text) - len(text.lstrip(" "))
    return "&nbsp;" * indent + text.lstrip(" ")


def build_story(body: str, styles: dict[str, ParagraphStyle]) -> list[Flowable]:
    story: list[Flowable] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            story.append(Paragraph("<br/>".join(paragraph), styles["body"]))
            paragraph.clear()

    for line in body.splitlines():
        kind, text = classify_line(line)
        if kind == "text":
            paragraph.append(_markup(text))
            continue
        flush()
        if kind in ("h1", "h2"):
            story.append(Paragraph(escape(text), styles[kind]))
        elif kind == "hr":
            story.append(Spacer(1, 4))
            story.append(HRFlowable(width="100%", thickness=0.6, color=colors.grey))
            story.append(Spacer(1, 8))
    flush()
    return story


def render_text_pdf(
    body: str,
    metadata: Mapping[str, str] | None = None,
    settings: Settings | None = None,
) -> bytes:
    settings = settings or get_settings()
    font_path = settings.resolve_font_path()
    regular, bold = _register_fonts(str(font_path) if font_path else None)
    metadata = dict(metadata or {})
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=2.2 * cm,
        rightMargin=2.2 * cm,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        title=metadata.get("title", ""),
        subject=metadata.get("subject", ""),
        keywords=metadata.get("keywords", ""),
        author=metadata.get("author", "docfill"),
        creator="docfill",
    )
    document.build(build_story(body, _styles(regular, bold)))
    return buffer.getvalue()
