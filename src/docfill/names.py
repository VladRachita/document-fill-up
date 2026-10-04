"""The names of people on a Romanian identity card, checked against the lists of Romanian names.

OCR loses the diacritics of a name (``STEFANESCU``), reads digits in place of letters
(``I0AN``) and takes look-alike letters for one another (``JOANA`` for ``IOANA``). What can be
decided from the letters alone is decided here: the diacritics are restored from the list, a
digit becomes the letter it is. What is a guess (``JOANA`` -> ``IOANA``) is returned as a
guess, for the caller to apply, show or hold back.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass

from docfill.lexicon import WordList, given_names, normalize, plain, surnames, with_case_of

GIVEN, FAMILY = "given", "family"

# Letters OCR takes for one another in the capitals of a card (I and J in a tilted or blurred
# scan, O and Q); the capital I read as l, 1 or a bar is repaired in ``extraction.idcard``.
LOOKALIKES = {"i": "j", "j": "i", "o": "q", "q": "o"}
# What a digit inside a name can only be: the letter of the same shape.
_DIGITS = {
    "0": "o", "1": "il", "2": "z", "3": "e", "4": "a", "5": "s", "6": "g", "7": "t", "8": "b",
}  # fmt: skip
_TOKEN = re.compile(r"[^\W_]+")
_MIN_GUESSED = 3  # an initial or a short word has too many look-alikes to be guessed


def words_of(kind: str) -> WordList:
    return given_names() if kind == GIVEN else surnames()


def _recase(original: str, text: str) -> str:
    """``text`` in the capitals of ``original``: ``CR1STIAN`` -> ``CRISTIAN``, ``Cr1stian`` ->
    ``Cristian``."""
    letters = [char for char in original if char.isalpha()]
    if letters and all(char.isupper() for char in letters):
        return text.upper()
    return text[:1].upper() + text[1:].lower() if original[:1].isupper() else text.lower()


def read_digits(token: str, words: WordList) -> tuple[str, bool]:
    """``I0AN`` -> ``IOAN``: the letters a name's digits can be. Returns the token and whether
    it is a listed name (several readings: the one that is listed; none: the first)."""
    if not any(char.isdigit() for char in token) or not any(char.isalpha() for char in token):
        return token, words.known(token)
    choices = [_DIGITS.get(char, char) if char.isdigit() else char for char in token.casefold()]
    choices = [list(choice) if len(choice) > 1 else [choice] for choice in choices]
    combinations = itertools.islice(itertools.product(*choices), 64)
    readings = ["".join(combination) for combination in combinations]
    for reading in readings:
        if words.known(reading):
            return _recase(token, reading), True
    return _recase(token, readings[0]), False


@dataclass(frozen=True)
class NameFix:
    value: str  # the name with its diacritics restored and its digits read as letters
    uncertain_digits: bool = False  # digits were replaced and the result is not a listed name
    restored: bool = False  # diacritics were restored from the list
    guess: str | None = None  # the name if look-alike letters were swapped for a listed name
    suggestion: str | None = None  # a listed name it may be (a letter lost, a name cut off)


def _one(names: list[str]) -> str | None:
    return names[0] if len({plain(name) for name in names}) == 1 else None


def fix_name(value: str, kind: str) -> NameFix:
    """The name as a card prints it, the Romanian name it is most likely a misreading of (look-alike
    letters swapped: ``JOANA`` -> ``IOANA``) and the one it may be (a thin letter lost:
    ``ONUT`` -> ``IONUȚ``; a name cut off by glare: ``DUMITRES`` -> ``DUMITRESCU``)."""
    words = words_of(kind)
    pieces = re.split(r"([\s\-]+)", normalize(value))
    last = max((i for i, piece in enumerate(pieces) if _TOKEN.fullmatch(piece)), default=-1)
    fixed, guessed, suggested = [], [], []
    uncertain = restored = False
    for index, piece in enumerate(pieces):
        token = piece
        guess = suggestion = piece
        if _TOKEN.fullmatch(piece):
            token, listed = read_digits(piece, words)
            uncertain |= token != piece and not listed
            found = words.lookup(token)
            if found.status == "restorable" and found.spelling:
                restored |= token != found.spelling
                token = found.spelling
            guess = suggestion = token
            if found.status == "unknown" and len(plain(token)) >= _MIN_GUESSED:
                if swapped := _one(words.neighbours(token, LOOKALIKES)):
                    guess = suggestion = with_case_of(token, swapped)
                else:
                    near = words.with_letter(token, "i") if len(plain(token)) >= 4 else []
                    if not near and index == last and len(plain(token)) >= 4:
                        near = words.completions(token)
                    if finished := _one(near):
                        suggestion = with_case_of(token, finished)
        fixed.append(token)
        guessed.append(guess)
        suggested.append(suggestion)
    joined = "".join(fixed)
    return NameFix(
        joined,
        uncertain,
        restored,
        guess="".join(guessed) if guessed != fixed else None,
        suggestion="".join(suggested) if suggested != guessed else None,
    )


# --------------------------------------------------------------------------- the zone


def _tokens(value: str) -> list[str]:
    return [token for token in re.split(r"[\s\-]+", value.strip()) if token]


def _same(a: str, b: str) -> bool:
    """The same letters, whatever OCR makes of the diacritics and of I / l / 1 and O / 0."""
    table = str.maketrans("01256lq", "oizsgio")
    return plain(a).translate(table) == plain(b).translate(table)


@dataclass(frozen=True)
class Settled:
    value: str  # the printed name, with the words the zone settles taken from it
    from_zone: bool  # the zone's spelling replaced a word of the printed name


def settle(printed: str, zone: str, kind: str) -> Settled | None:
    """Decide a difference between a printed name and the one in the machine readable zone
    from the list of names: a word that is not a Romanian name is a misreading, the other
    reading is right. ``None`` when the list cannot tell (both or neither word is listed)."""
    words = words_of(kind)
    printed_tokens, zone_tokens = _tokens(printed), _tokens(zone)
    if not printed_tokens or len(printed_tokens) != len(zone_tokens):
        return None
    chosen, from_zone = [], False
    for have, other in zip(printed_tokens, zone_tokens, strict=True):
        if _same(have, other):
            chosen.append(have)
            continue
        have_listed = words.known(read_digits(have, words)[0])
        other_word, other_listed = read_digits(other, words)
        other_listed = other_listed and words.known(other_word)
        if have_listed == other_listed:
            return None
        if other_listed:
            found = words.lookup(other_word)
            chosen.append(with_case_of(have, found.spelling or other_word))
            from_zone = True
        else:
            chosen.append(have)
    # keep the separators of the printed name
    result, tokens = [], iter(chosen)
    for piece in re.split(r"([\s\-]+)", printed.strip()):
        result.append(next(tokens) if _TOKEN.fullmatch(piece) else piece)
    return Settled("".join(result), from_zone)
