"""Repairs and plausibility checks for the text printed on a Romanian identity card.

The card prints names in capitals and places, streets and the issuing office in capitalised
words. OCR confuses the capital I with a lowercase l, a 1 or a bar (``lON``, ``Alba lulia``,
``luliu Maniu``), and nothing on the card is written that way: such a word is repaired. A value
that is still not text a card can hold (symbols, a neighbouring label read as the value, only
fragments) is flagged, so it is proposed to a person instead of being written into a form.
"""

from __future__ import annotations

import re
from difflib import get_close_matches

from docfill.extraction.fields import FIELDS, FieldSpec
from docfill.extraction.text_utils import fold
from docfill.models import ExtractedField
from docfill.ro import repair_county_codes

SUSPECT_CONFIDENCE = 0.45  # under the default fill threshold: shown, never filled in

_UPPER = "A-ZĂÂÎȘȚŞŢ"
_LOWER = "a-zăâîșțşţ"
_CONFUSED_I = "l1|!"

# A name is printed in capitals only: an l, a 1 or a bar inside a word of capitals is a capital I.
_NAME_I = re.compile(
    rf"(?<=[{_UPPER}])[{_CONFUSED_I}](?![{_LOWER}])|(?<![{_UPPER}{_LOWER}])[{_CONFUSED_I}](?=[{_UPPER}])"
)
# A place, street or office is capitalised: a word that starts with a lowercase l (or a 1, a
# bar) followed by letters is a capital I ("lulia", "lași", "luliu"), except real small words.
_WORD_I = re.compile(rf"(?<![{_UPPER}{_LOWER}\d])[{_CONFUSED_I}][{_LOWER}][\w{_LOWER}-]*")
_I_WORD = re.compile(rf"(?<![{_UPPER}{_LOWER}\d])[jJ][{_LOWER}][\w{_LOWER}]*")
_SMALL_WORDS = {"la", "lui", "le", "lot", "loc", "langa"}
# Frequent places, streets and first names that start with a capital I; OCR also reads that I
# as a j (``julia``). Only these words are repaired, so a real "Jiului" is left alone.
_I_WORDS = {
    "iasi", "iasomiei", "iancu", "iacob", "ilie", "ileana", "ilfov", "independentei", "industriei",
    "ioan", "ioana", "ion", "iosif", "irina", "izvorului", "iuliu", "iulia", "ineu", "ianca",
}  # fmt: skip
# The prefixes a card prints with a capital letter, which OCR sometimes lowercases ("jud.AB").
_LOWERED_PREFIX = re.compile(r"\b(?:jud|mun|com|sat|str|sec|bd)(?=[.\s])")


def repair_name(text: str) -> str:
    return _NAME_I.sub("I", text)


def repair_text(text: str) -> str:
    """The text of a place, street or office as the card prints it: county codes whose letters
    OCR misread (``C}`` for ``CJ``), prefixes OCR lowercased (``jud.AB``) and the capital I read
    as a lowercase l, 1, bar or j (``lulia``, ``juliu``)."""

    def capital_i(match: re.Match[str]) -> str:
        word = match.group()
        return word if fold(word) in _SMALL_WORDS else "I" + word[1:]

    def i_word(match: re.Match[str]) -> str:
        word = match.group()
        return "I" + word[1:] if "i" + fold(word)[1:] in _I_WORDS else word

    text = _LOWERED_PREFIX.sub(lambda match: match.group().capitalize(), repair_county_codes(text))
    return _I_WORD.sub(i_word, _WORD_I.sub(capital_i, text))


def repair(spec: FieldSpec, value: str) -> str:
    """The repair that fits the field: names, then places / addresses / the issuing office."""
    if spec.kind == "name":
        return repair_name(value)
    if spec.kind in ("place", "address") or spec.name.endswith("id_issued_by"):
        return repair_text(value)
    return value


# --------------------------------------------------------------------------- plausibility

# Characters that are never part of a name, a place, a street or an office on a card; they come
# from the guilloche background, the machine readable zone or a stamp read as text.
_SYMBOLS = re.compile(r"[<>{}\[\]|\\~^*_=;@#$%&?!]")
# The printed labels of the card, as OCR can misspell them ("Malabilitate" for "Valabilitate").
_LABEL_WORDS = (
    "valabilitate",
    "validite",
    "validity",
    "domiciliu",
    "adresse",
    "address",
    "emisa",
    "delivree",
    "issued",
    "cetatenie",
    "nationalite",
    "nationality",
    "prenume",
    "prenom",
    "naissance",
    "identitate",
    "identity",
    "nastere",
)


def suspicious(spec: FieldSpec, value: str) -> str | None:
    """Why ``value`` cannot be what ``spec`` holds on an identity card, or ``None``."""
    if spec.kind not in ("name", "place", "address") and not spec.name.endswith("id_issued_by"):
        return None
    if _SYMBOLS.search(value):
        return "contains symbols that are not part of the text: check it against the card"
    if spec.kind != "address" and "/" in value:
        return "reads like a printed label, not a value: check it against the card"
    words = re.findall(rf"[{_UPPER}{_LOWER}]{{3,}}", value)
    if any(get_close_matches(fold(word), _LABEL_WORDS, n=1, cutoff=0.8) for word in words):
        return "reads like a printed label, not a value: check it against the card"
    letters = sum(char.isalpha() for char in value)
    if letters < 3 or letters < 0.4 * len(value.replace(" ", "")):
        return "has too few letters to be read reliably: check it against the card"
    return None


# Where a value does not come from the text of the card (or is already checked elsewhere).
_NOT_PRINTED = {"mrz", "derived", "manual", "form", "default", "memory"}


def review(candidate: ExtractedField) -> ExtractedField:
    """A value read from the text of an identity card: repaired the way the card prints it and,
    if it still cannot be what the field holds, lowered under the fill threshold."""
    spec = FIELDS.get(candidate.name)
    if spec is None or candidate.source in _NOT_PRINTED:
        return candidate
    value = repair(spec, candidate.value)
    changes: dict = {"value": value} if value != candidate.value else {}
    problem = suspicious(spec, value)
    if problem and problem not in candidate.issues:
        changes["confidence"] = min(candidate.confidence, SUSPECT_CONFIDENCE)
        changes["issues"] = [*candidate.issues, problem]
    return candidate.model_copy(update=changes) if changes else candidate
