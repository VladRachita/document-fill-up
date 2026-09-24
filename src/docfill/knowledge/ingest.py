"""Read legal texts and example documents from files (TXT, Markdown, HTML, PDF, DOCX, images)."""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

from docfill.config import Settings
from docfill.errors import DocumentReadError

TEXT_SUFFIXES = {".txt", ".md", ".text"}
HTML_SUFFIXES = {".html", ".htm"}
_BLOCK_TAGS = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section"}


class _TextOfHtml(HTMLParser):
    """Visible text of an HTML page (e.g. a law saved from legislatie.just.ro)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    parser = _TextOfHtml()
    parser.feed(html)
    return "".join(parser.parts)


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1250", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")  # pragma: no cover (latin-1 never fails)


def extract_text(data: bytes, filename: str, settings: Settings) -> str:
    """The text of a file: plain text and HTML directly, anything else through the document
    readers (text layer, Word body, OCR of scans and photos)."""
    if not data:
        raise DocumentReadError(f"{filename}: file is empty")
    if len(data) > settings.max_file_size:
        raise DocumentReadError(f"{filename}: file is larger than the upload limit")
    suffix = Path(filename).suffix.lower()
    if suffix in TEXT_SUFFIXES:
        return _decode(data)
    if suffix in HTML_SUFFIXES:
        return html_to_text(_decode(data))
    from docfill.readers import read_bytes

    return read_bytes(data, filename, settings).text
