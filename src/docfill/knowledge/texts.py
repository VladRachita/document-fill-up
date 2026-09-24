"""Legal texts fed to docfill: split into articles and searched locally.

Laws, ordinances and ONRC guides are split into passages (one per article when the text has
``Art. 10`` / ``Articolul 10`` headings, otherwise paragraphs) and searched with TF-IDF over
words and character n-grams, which is robust to missing diacritics and Romanian word endings
("capitalul social" finds "capital social"). Nothing leaves the machine.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from docfill.knowledge.checks import fold

MAX_PASSAGE_CHARS = 3000
TARGET_CHUNK_CHARS = 1500
MIN_SCORE = 0.04

_ARTICLE = re.compile(
    r"^[ \t]*(?:Art\.|ARTICOLUL|Articolul|ART\.)[ \t]*(\d+)(?:[ \t]*\^[ \t]*(\d+))?\b",
    re.MULTILINE,
)
_ACT_NUMBER = re.compile(r"(\d+)\s*/\s*(\d{4})")
_ARTICLE_NUMBER = re.compile(r"art(?:icolul|\.)?\s*(\d+(?:\s*\^\s*\d+)?)", re.IGNORECASE)


@dataclass
class Passage:
    article: str | None
    text: str


def normalize_text(text: str) -> str:
    """NFC, comma-below ș/ț (the cedilla forms are common in older texts), tidy whitespace."""
    text = unicodedata.normalize("NFC", text)
    text = text.translate(str.maketrans("şţŞŢ", "șțȘȚ")).replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(" ".join(line.split()) for line in text.split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _pack(blocks: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for block in blocks:
        while len(block) > MAX_PASSAGE_CHARS:  # one enormous paragraph
            cut = block.rfind(" ", 0, MAX_PASSAGE_CHARS)
            cut = cut if cut > 0 else MAX_PASSAGE_CHARS
            if current:
                chunks.append(current)
                current = ""
            chunks.append(block[:cut].strip())
            block = block[cut:].strip()
        if current and len(current) + len(block) + 2 > limit:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)
    return chunks


def _split_long_article(text: str) -> list[str]:
    if len(text) <= MAX_PASSAGE_CHARS:
        return [text]
    # split before "(2)", "(3)"... paragraph markers, else at blank lines
    blocks = re.split(r"\n(?=\(\d+\))", text)
    if len(blocks) == 1:
        blocks = text.split("\n\n")
    return _pack([b.strip() for b in blocks if b.strip()], MAX_PASSAGE_CHARS)


def split_passages(text: str) -> list[Passage]:
    text = normalize_text(text)
    if not text:
        return []
    headers = list(_ARTICLE.finditer(text))
    if len(headers) >= 2:
        passages: list[Passage] = []
        preamble = text[: headers[0].start()].strip()
        if len(preamble) > 40:
            passages += [Passage(None, chunk) for chunk in _pack(preamble.split("\n\n"), 1500)]
        for index, header in enumerate(headers):
            end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
            body = text[header.start() : end].strip()
            number = header.group(1) + (f"^{header.group(2)}" if header.group(2) else "")
            passages += [Passage(f"art. {number}", part) for part in _split_long_article(body)]
        return passages
    blocks = [block.strip() for block in text.split("\n\n") if block.strip()]
    return [Passage(None, chunk) for chunk in _pack(blocks, TARGET_CHUNK_CHARS)]


def citation_key(citation: str) -> str:
    """``Legea nr. 31/1990 privind societățile`` -> ``31/1990`` (how acts are referenced)."""
    match = _ACT_NUMBER.search(citation or "")
    return f"{match.group(1)}/{match.group(2)}" if match else " ".join(fold(citation).split())


def article_number(article: str | None) -> str | None:
    """``art. 10 alin. (1)`` -> ``10``; ``Art. 7^1`` -> ``7^1``."""
    match = _ARTICLE_NUMBER.search(article or "")
    return re.sub(r"\s+", "", match.group(1)) if match else None


@dataclass
class Document:
    """Anything searchable: a passage of a law or a knowledge entry."""

    ref: str  # "passage:12" or "rule:sa-capital-minim"
    title: str
    subtitle: str
    text: str


class SearchIndex:
    """TF-IDF over words (unigrams, bigrams) and character n-grams of accent-free text."""

    def __init__(self, documents: list[Document]):
        self.documents = documents
        self._word = self._char = None
        self._word_matrix = self._char_matrix = None
        corpus = [fold(f"{d.title}\n{d.text}") for d in documents]
        if not any(text.strip() for text in corpus):
            return
        from sklearn.feature_extraction.text import TfidfVectorizer

        self._word = TfidfVectorizer(
            ngram_range=(1, 2), sublinear_tf=True, token_pattern=r"(?u)\b\w\w+\b"
        )
        self._char = TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True)
        self._word_matrix = self._word.fit_transform(corpus)
        self._char_matrix = self._char.fit_transform(corpus)

    def search(self, query: str, limit: int = 5) -> list[tuple[Document, float]]:
        if self._word is None or not query.strip():
            return []
        folded = fold(query)
        word_scores = (self._word_matrix @ self._word.transform([folded]).T).toarray().ravel()
        char_scores = (self._char_matrix @ self._char.transform([folded]).T).toarray().ravel()
        scores = 0.6 * word_scores + 0.4 * char_scores
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
        return [
            (self.documents[i], round(float(scores[i]), 4))
            for i in ranked[:limit]
            if scores[i] >= MIN_SCORE
        ]
