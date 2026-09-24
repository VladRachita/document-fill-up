"""Machine-learning extraction with a spaCy named-entity recognition model.

The NER model finds people (``PERSON``) and places (``GPE``) in free text where no label is
present, e.g. *"I, John Smith, born in Paris, currently living in London..."*. Its results get
lower confidence than labelled values, so a form's explicit ``Name:`` always wins.
"""

from __future__ import annotations

import logging
import re
import threading
from collections import Counter
from typing import Any

from docfill.extraction.fields import FIELDS
from docfill.extraction.text_utils import (
    collapse,
    fold,
    is_country,
    looks_like_name,
    normalize_name,
)
from docfill.models import ExtractedField

logger = logging.getLogger(__name__)

PERSON_CONFIDENCE = 0.65
SINGLE_TOKEN_PERSON_CONFIDENCE = 0.4
COUNTRY_CONFIDENCE = 0.6
CITY_CONFIDENCE = 0.5
RESIDENCE_CITY_CONFIDENCE = 0.6
BIRTHPLACE_CONFIDENCE = 0.7
MAX_CHARS = 100_000

_BIRTH_CONTEXT = re.compile(r"\b(?:born|birth|nascut|nascuta|nasterii)\b[^.;\n]{0,25}$")
_RESIDENCE_CONTEXT = re.compile(
    r"\b(?:live|lives|living|reside|resides|residing|resident|domiciled|domiciliat|domiciliata|"
    r"located|address)\b[^.;\n]{0,40}$"
)


def _label_words() -> set[str]:
    return {collapse(fold(s)) for spec in FIELDS.values() for s in spec.synonyms}


class NERExtractor:
    def __init__(self, model: str):
        self.model = model
        self._nlp: Any = None
        self._error: str | None = None
        # spaCy pipelines are not guaranteed to be thread-safe (the API serves requests in a
        # thread pool), so loading and inference are serialised.
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._load() is not None

    @property
    def error(self) -> str | None:
        return self._error

    def _load(self) -> Any:
        with self._lock:
            return self._load_locked()

    def _load_locked(self) -> Any:
        if self._nlp is None and self._error is None:
            try:
                import spacy

                self._nlp = spacy.load(self.model)
            except (ImportError, OSError) as exc:
                self._error = (
                    f"spaCy model '{self.model}' is not available ({exc}). "
                    f"Install it with: python -m spacy download {self.model}"
                )
                logger.warning(self._error)
        return self._nlp

    def extract(self, text: str, document: str | None = None) -> list[ExtractedField]:
        nlp = self._load()
        if nlp is None or not text.strip():
            return []
        with self._lock:
            doc = nlp(text[:MAX_CHARS])
        labels = _label_words()
        found: list[ExtractedField] = []

        def emit(name: str, value: str, confidence: float, evidence: str) -> None:
            found.append(
                ExtractedField(
                    name=name,
                    value=value,
                    confidence=confidence,
                    source="ner",
                    evidence=collapse(evidence),
                    document=document,
                )
            )

        people = [
            ent
            for ent in doc.ents
            if ent.label_ == "PERSON"
            and looks_like_name(collapse(ent.text), strict=True)
            and collapse(fold(ent.text)) not in labels
        ]
        if people:
            counts = Counter(collapse(ent.text) for ent in people)
            multi = [ent for ent in people if len(ent.text.split()) > 1]
            pool = multi or people
            best = max(pool, key=lambda ent: (counts[collapse(ent.text)], -ent.start_char))
            confidence = PERSON_CONFIDENCE if multi else SINGLE_TOKEN_PERSON_CONFIDENCE
            emit("full_name", normalize_name(best.text), confidence, best.sent.text)

        for ent in doc.ents:
            if ent.label_ not in ("GPE", "LOC"):
                continue
            value = collapse(ent.text).strip(" ,.")
            if not value or any(char.isdigit() for char in value):
                continue
            before = fold(text[ent.sent.start_char : ent.start_char])
            if _BIRTH_CONTEXT.search(before):
                emit("place_of_birth", normalize_name(value), BIRTHPLACE_CONFIDENCE, ent.sent.text)
            elif is_country(value):
                emit("country", normalize_name(value), COUNTRY_CONFIDENCE, ent.sent.text)
            elif ent.label_ == "GPE":
                confidence = (
                    RESIDENCE_CITY_CONFIDENCE
                    if _RESIDENCE_CONTEXT.search(before)
                    else CITY_CONFIDENCE
                )
                emit("city", normalize_name(value), confidence, ent.sent.text)
        return found
