"""Sanitization: clean the raw text so only relevant content reaches the extractor.

Steps (each can be switched off through :class:`SanitizeOptions`):

1. repair broken unicode / mojibake (``ftfy``) and strip control characters;
2. re-join words hyphenated across line breaks;
3. drop page artifacts (``Page 2 of 5``, lone page numbers at page edges);
4. drop headers/footers repeated on most pages (the first occurrence is kept);
5. drop empty form blanks (``_____``) and OCR noise lines;
6. redact sensitive data that is never needed to fill a document (card numbers, IBANs).
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

import ftfy

from docfill.models import RawDocument, SanitizedDocument

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f​-‏  ﻿]")
_HSPACE = re.compile(r"[ \t  -   　]+")
_FORM_BLANKS = re.compile(r"_{3,}|\.{4,}|…{2,}")
_PAGE_LABEL = re.compile(
    r"^(?:page|pagina|pag\.?)\s*\d{1,4}(?:\s*(?:/|of|din|de)\s*\d{1,4})?$", re.IGNORECASE
)
_BARE_PAGE_NUMBER = re.compile(r"^[-–—]?\s*\d{1,4}\s*(?:/\s*\d{1,4})?\s*[-–—]?$")
_EDGE_LINES = 3


@dataclass(frozen=True)
class SanitizeOptions:
    fix_unicode: bool = True
    dehyphenate: bool = True
    remove_page_artifacts: bool = True
    remove_repeated_lines: bool = True
    remove_noise_lines: bool = True
    redact: tuple[str, ...] = ("credit_card", "iban")


# --------------------------------------------------------------------------- redaction


def _luhn_valid(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _iban_valid(iban: str) -> bool:
    rearranged = iban[4:] + iban[:4]
    numeric = "".join(str(int(char, 36)) for char in rearranged)
    return int(numeric) % 97 == 1


_CARD_RE = re.compile(r"(?<![\w-])(?:\d[ -]?){12,18}\d(?![\w-])")
_IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")


def _redact_cards(text: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        digits = re.sub(r"\D", "", match.group())
        if 13 <= len(digits) <= 19 and _luhn_valid(digits):
            count += 1
            return "[REDACTED CARD]"
        return match.group()

    return _CARD_RE.sub(replace, text), count


def _redact_ibans(text: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        compact = match.group().replace(" ", "")
        if 15 <= len(compact) <= 34 and _iban_valid(compact):
            count += 1
            return "[REDACTED IBAN]"
        return match.group()

    return _IBAN_RE.sub(replace, text), count


REDACTORS: dict[str, Callable[[str], tuple[str, int]]] = {
    "iban": _redact_ibans,
    "credit_card": _redact_cards,
}


# --------------------------------------------------------------------------- helpers


def normalize_text(text: str, fix_unicode: bool = True) -> str:
    if fix_unicode:
        text = ftfy.fix_text(text)
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n")
    return _CONTROL_CHARS.sub("", text)


def dehyphenate(lines: list[str]) -> list[str]:
    """Join ``infor-`` + ``mation`` split across lines."""
    result: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        if (
            result
            and len(result[-1]) > 1
            and result[-1].endswith("-")
            and result[-1][-2].isalpha()
            and stripped[:1].islower()
        ):
            head, _, rest = stripped.partition(" ")
            result[-1] = result[-1][:-1] + head
            if rest:
                result.append(rest)
            continue
        result.append(line)
    return result


def is_noise(line: str) -> bool:
    """Lines with no letters/digits, or mostly symbols (typical OCR debris)."""
    compact = line.replace(" ", "")
    if not compact:
        return False
    alnum = sum(char.isalnum() for char in compact)
    if alnum == 0:
        return True
    return len(compact) >= 4 and alnum / len(compact) < 0.4


def _fingerprint(line: str) -> str:
    return re.sub(r"\d+", "#", line.casefold())


def _edge_indexes(lines: list[str]) -> set[int]:
    non_empty = [index for index, line in enumerate(lines) if line]
    return set(non_empty[:_EDGE_LINES]) | set(non_empty[-_EDGE_LINES:])


# --------------------------------------------------------------------------- sanitizer


class Sanitizer:
    def __init__(self, options: SanitizeOptions | None = None):
        self.options = options or SanitizeOptions()
        unknown = set(self.options.redact) - set(REDACTORS)
        if unknown:
            raise ValueError(f"Unknown redactors: {', '.join(sorted(unknown))}")

    def _clean_page(self, text: str) -> list[str]:
        text = normalize_text(text, self.options.fix_unicode)
        lines = [_HSPACE.sub(" ", line).strip() for line in text.split("\n")]
        if self.options.dehyphenate:
            lines = dehyphenate(lines)
        return lines

    def sanitize(self, document: RawDocument) -> SanitizedDocument:
        opts = self.options
        pages = [self._clean_page(page.text) for page in document.pages]
        removed = 0

        boilerplate: set[str] = set()
        if opts.remove_repeated_lines and len(pages) > 1:
            counts: Counter[str] = Counter()
            for lines in pages:
                counts.update({_fingerprint(lines[i]) for i in _edge_indexes(lines)})
            threshold = max(2, math.ceil(len(pages) / 2))
            boilerplate = {fp for fp, count in counts.items() if count >= threshold}
        seen_boilerplate: set[str] = set()

        cleaned_pages: list[str] = []
        for lines in pages:
            edges = _edge_indexes(lines)
            kept: list[str] = []
            for index, line in enumerate(lines):
                if not line:
                    kept.append("")
                    continue
                if opts.remove_page_artifacts and (
                    _PAGE_LABEL.match(line) or (index in edges and _BARE_PAGE_NUMBER.match(line))
                ):
                    removed += 1
                    continue
                if index in edges and (fp := _fingerprint(line)) in boilerplate:
                    if fp in seen_boilerplate:
                        removed += 1
                        continue
                    seen_boilerplate.add(fp)
                if opts.remove_noise_lines:
                    line = _HSPACE.sub(" ", _FORM_BLANKS.sub(" ", line)).strip()
                    if not line or is_noise(line):
                        removed += 1
                        continue
                kept.append(line)
            cleaned_pages.append(re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip())

        text = "\n\n".join(page for page in cleaned_pages if page)
        redactions: dict[str, int] = {}
        for name in opts.redact:
            text, count = REDACTORS[name](text)
            if count:
                redactions[name] = count

        return SanitizedDocument(
            source=document.source, text=text, removed_lines=removed, redactions=redactions
        )


def sanitize(document: RawDocument, options: SanitizeOptions | None = None) -> SanitizedDocument:
    return Sanitizer(options).sanitize(document)
