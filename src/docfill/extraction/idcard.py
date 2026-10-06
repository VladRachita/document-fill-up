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

from docfill import names
from docfill.extraction.fields import FIELDS, FieldSpec
from docfill.extraction.text_utils import fold
from docfill.lexicon import normalize
from docfill.models import ExtractedField
from docfill.ro import office_doubt, repair_county_codes

SUSPECT_CONFIDENCE = 0.45  # under the default fill threshold: shown, never filled in
GUESS_CONFIDENCE = 0.75  # a first name corrected to a Romanian one: filled in, with a note
ALTERNATIVE_CONFIDENCE = 0.4  # a reading offered next to the value, to be picked by a person
CORRECTED = "Citit ca"  # the note of a name that was corrected: the zone can overrule it
UNKNOWN_NAME = "Nu este un nume românesc cunoscut"  # a name that may be misread or cut off
NAME_NOTES = (CORRECTED, UNKNOWN_NAME)  # what the machine readable zone can settle

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
    """The repair that fits the field: names, then places / addresses / the issuing office. The
    cedilla letters of old encodings (``ş``, ``ţ``) become the Romanian ``ș``, ``ț``."""
    if spec.kind == "name":
        return repair_name(normalize(value))
    if spec.kind in ("place", "address") or spec.name.endswith("id_issued_by"):
        return repair_text(normalize(value))
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
        return "conține simboluri care nu fac parte din text: comparați cu cartea de identitate"
    if spec.kind != "address" and "/" in value:
        return "pare o etichetă tipărită, nu o valoare: comparați cu cartea de identitate"
    words = re.findall(rf"[{_UPPER}{_LOWER}]{{3,}}", value)
    if any(get_close_matches(fold(word), _LABEL_WORDS, n=1, cutoff=0.8) for word in words):
        return "pare o etichetă tipărită, nu o valoare: comparați cu cartea de identitate"
    letters = sum(char.isalpha() for char in value)
    if letters < 3 or letters < 0.4 * len(value.replace(" ", "")):
        return "are prea puține litere pentru a fi citit sigur: comparați cu cartea de identitate"
    return None


# Where a value does not come from the text of the card (or is already checked elsewhere).
_NOT_PRINTED = {"mrz", "derived", "manual", "form", "default", "memory"}


def _name_kind(spec: FieldSpec) -> str | None:
    if spec.kind != "name":
        return None
    if spec.name.endswith("first_name"):
        return names.GIVEN
    return names.FAMILY if spec.name.endswith("last_name") else None


def _review_name(candidate: ExtractedField, kind: str) -> tuple[dict, list[ExtractedField]]:
    """The changes to a first or last name that the lists of Romanian names decide: diacritics
    restored, digits read as letters. A name that is not Romanian and is a look-alike letter
    away from a name that is (``JOANA``, ``IOANA``) is corrected when it is a first name (with a
    note, the reading kept next to it). Anything less sure (a surname, a lost letter, a name cut
    off) is offered, not filled in."""
    fix = names.fix_name(candidate.value, kind)
    changes: dict = {"value": fix.value} if fix.value != candidate.value else {}
    issues, confidence, extra = list(candidate.issues), candidate.confidence, []
    if fix.uncertain_digits:
        confidence = min(confidence, GUESS_CONFIDENCE)
        issues.append("Cifrele citite în nume au fost înlocuite cu litere: verificați numele")
    suggestion = fix.suggestion or (fix.guess if kind == names.FAMILY else None)
    if fix.guess and kind == names.GIVEN:
        confidence = min(confidence, GUESS_CONFIDENCE)
        issues.append(
            f"{CORRECTED} „{fix.value}”, corectat în prenumele românesc {fix.guess}: "
            "comparați cu cartea de identitate"
        )
        changes.update(value=fix.guess, original=fix.value)
        reading = {"value": fix.value, "confidence": ALTERNATIVE_CONFIDENCE, "original": None}
        extra.append(candidate.model_copy(update=reading))
    elif suggestion:
        confidence = min(confidence, SUSPECT_CONFIDENCE)
        what = "prenume" if kind == names.GIVEN else "nume de familie"
        issues.append(f"{UNKNOWN_NAME} ({what}): ați vrut să scrieți {suggestion}? Verificați")
        offer = {"value": suggestion, "confidence": SUSPECT_CONFIDENCE - 0.01, "source": "derived"}
        extra.append(candidate.model_copy(update={**offer, "issues": []}))
    if issues != candidate.issues:
        changes.update(issues=issues, confidence=confidence)
    return changes, extra


def _propose(
    candidate: ExtractedField, problem: str, suggestions: tuple[str, ...]
) -> tuple[dict, list[ExtractedField]]:
    """A value that cannot be what the field holds, shown for a person: the only name that fits
    becomes the value (what was read stays next to it), several are offered next to it. Never
    filled in."""
    issues = [*candidate.issues, problem]
    if len(suggestions) == 1:
        reading = {"confidence": ALTERNATIVE_CONFIDENCE - 0.1, "issues": issues}
        changes = {
            "value": suggestions[0],
            "confidence": SUSPECT_CONFIDENCE - 0.01,
            "source": "derived",
            "issues": [problem],
        }
        return changes, [candidate.model_copy(update=reading)]
    offered = {"confidence": SUSPECT_CONFIDENCE - 0.01, "source": "derived", "issues": []}
    extra = [candidate.model_copy(update={**offered, "value": value}) for value in suggestions]
    return {"confidence": min(candidate.confidence, SUSPECT_CONFIDENCE), "issues": issues}, extra


def review(candidate: ExtractedField) -> list[ExtractedField]:
    """A value read from the text of an identity card: repaired the way the card prints it and,
    if it still cannot be what the field holds, lowered under the fill threshold. Returns the
    value followed by the other readings of it that a person may pick."""
    spec = FIELDS.get(candidate.name)
    if spec is None or candidate.source in _NOT_PRINTED:
        return [candidate]
    value = repair(spec, candidate.value)
    changes: dict = {"value": value} if value != candidate.value else {}
    extra: list[ExtractedField] = []
    if kind := _name_kind(spec):
        more, extra = _review_name(candidate.model_copy(update=changes), kind)
        changes.update(more)
        value = changes.get("value", value)
    elif spec.name.endswith("id_issued_by") and (doubt := office_doubt(value)):
        more, extra = _propose(
            candidate.model_copy(update=changes), doubt.problem, doubt.suggestions
        )
        changes.update(more)
        value = changes.get("value", value)
    problem = suspicious(spec, value)
    if problem and problem not in changes.get("issues", candidate.issues):
        changes["confidence"] = min(
            changes.get("confidence", candidate.confidence), SUSPECT_CONFIDENCE
        )
        changes["issues"] = [*changes.get("issues", candidate.issues), problem]
    return [candidate.model_copy(update=changes) if changes else candidate, *extra]
