"""Romanian reference data shipped with docfill: given names, surnames, the words of street
names and the localities of every county (``data/``, see the header of each file).

A card is read by OCR, and OCR loses the diacritics and mixes up look-alike letters. A Romanian
clerk reading the same card knows which names exist: the lists give docfill that knowledge, so
that ``STEFANESCU`` can become ``ȘTEFĂNESCU``, ``JOANA`` can be seen as a misreading of
``IOANA``, and a municipality that does not exist (``Mun. Sib``) is told from one that does.
The lists only ever *add* what a card prints: a word that is not listed is left as it was read.
"""

from __future__ import annotations

import itertools
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import cache
from importlib import resources

ROMANIAN_MARKS = "ăâîșțĂÂÎȘȚ"
_CEDILLA = str.maketrans("ŞşŢţ", "ȘșȚț")  # legacy encodings of the letters ș and ț


def normalize(text: str) -> str:
    """The text in NFC, with the comma-below ș ț: ``ş`` / ``ţ`` (cedilla) are the same letters
    in the encodings of old systems and of some OCR models."""
    return unicodedata.normalize("NFC", text).translate(_CEDILLA)


def _base(char: str) -> str:
    return unicodedata.normalize("NFD", char)[0]


def plain(text: str) -> str:
    """Lower case, without accents, a hyphen or a run of blanks as a single blank: the key a word
    is looked up under (``Ștefănescu`` -> ``stefanescu``, ``Cluj-Napoca`` -> ``cluj napoca``)."""
    decomposed = unicodedata.normalize("NFD", normalize(text))
    stripped = "".join(char for char in decomposed if unicodedata.category(char) != "Mn")
    return re.sub(r"[\s\-]+", " ", stripped.casefold()).strip()


def has_marks(text: str) -> bool:
    """Whether ``text`` has any of the Romanian diacritics (ă â î ș ț)."""
    return any(char in ROMANIAN_MARKS for char in normalize(text))


def with_case_of(original: str, spelling: str) -> str:
    """``spelling`` in the capitals of ``original``: ``IONESCU`` -> ``IONESCU``-style upper case,
    lower case stays lower case, anything else takes the spelling as listed."""
    letters = [char for char in original if char.isalpha()]
    if letters and all(char.isupper() for char in letters):
        return spelling.upper()
    if letters and all(char.islower() for char in letters):
        return spelling.lower()
    return spelling


def restore_marks(original: str, spelling: str) -> str | None:
    """``spelling`` (in the capitals of ``original``) if ``original`` is that word with some of
    its Romanian diacritics missing or misread: ``Stefanescu`` -> ``Ștefănescu``. ``None`` when
    the two are different words, or when it would take away a diacritic ``original`` has."""
    original, spelling = normalize(original), normalize(spelling)
    if len(original) != len(spelling) or plain(original) != plain(spelling):
        return None
    for have, listed in zip(original, spelling, strict=True):
        if have.casefold() == listed.casefold():
            continue
        # only a Romanian diacritic is ever added, and only where the word has none of its own
        if (
            listed not in ROMANIAN_MARKS
            or have in ROMANIAN_MARKS
            or _base(have).casefold() != _base(listed).casefold()
        ):
            return None
    return with_case_of(original, spelling)


# --------------------------------------------------------------------------- word lists


@dataclass(frozen=True)
class Lookup:
    """How a word stands against a list: ``exact`` (listed as it is), ``restorable`` (listed with
    more diacritics: ``spelling`` is the word restored), ``known`` (listed, but with accents that
    are not ours to touch, or in two spellings) or ``unknown``."""

    status: str
    spelling: str | None = None


_UNKNOWN = Lookup("unknown")
_MAX_COMBINATIONS = 4096


class WordList:
    """Words (names) by the section they are listed under, searchable by their accent-free key."""

    def __init__(self, entries: Iterable[tuple[str, str]]):
        self._listed: dict[str, tuple[str, str]] = {}  # lower case -> (spelling, section)
        self._by_plain: dict[str, list[str]] = defaultdict(list)
        for word, section in entries:
            word = normalize(word)
            if word.casefold() not in self._listed:
                self._listed[word.casefold()] = (word, section)
                self._by_plain[plain(word)].append(word)

    def __len__(self) -> int:
        return len(self._listed)

    def words(self) -> list[str]:
        """Every listed word, as it is spelled in the list."""
        return [spelling for spelling, _ in self._listed.values()]

    def section(self, word: str) -> str | None:
        """The section ``word`` is listed in (as it is written, in any capitals)."""
        listed = self._listed.get(normalize(word).casefold())
        return listed[1] if listed else None

    def lookup(self, word: str) -> Lookup:
        word = normalize(word)
        if listed := self._listed.get(word.casefold()):
            return Lookup("exact", listed[0])
        forms = self._by_plain.get(plain(word))
        if not forms:
            return _UNKNOWN
        if len(forms) == 1 and (restored := restore_marks(word, forms[0])):
            return Lookup("restorable", restored)
        return Lookup("known")

    def known(self, word: str) -> bool:
        return self.lookup(word).status != "unknown"

    def with_letter(self, word: str, letter: str) -> list[str]:
        """The listed words that are ``word`` with ``letter`` put back (a thin letter, an I,
        that OCR lost: ``Onut`` -> ``Ionuț``)."""
        base = plain(word)
        found: dict[str, None] = {}
        for index in range(len(base) + 1):
            for spelling in self._by_plain.get(base[:index] + letter + base[index:], []):
                found[spelling] = None
        return list(found)

    def completions(self, word: str) -> list[str]:
        """The listed words that begin with ``word`` and are longer (a name cut off by glare)."""
        base = plain(word)
        return [
            spelling
            for key, forms in self._by_plain.items()
            if key.startswith(base) and key != base
            for spelling in forms
        ]

    def neighbours(self, word: str, swaps: Mapping[str, str], max_swaps: int = 2) -> list[str]:
        """The listed words ``word`` becomes when up to ``max_swaps`` of its letters are replaced
        by the letters OCR takes them for (``swaps``: letter -> look-alikes)."""
        base = plain(word)
        options = [[char, *swaps.get(char, "")] for char in base]
        total = 1
        for choices in options:
            total *= len(choices)
        if total == 1 or total > _MAX_COMBINATIONS:
            return []
        found: dict[str, None] = {}
        for combination in itertools.product(*options):
            changed = sum(a != b for a, b in zip(combination, base, strict=True))
            candidate = "".join(combination)
            if 0 < changed <= max_swaps and candidate in self._by_plain:
                found.update(dict.fromkeys(self._by_plain[candidate]))
        return list(found)


def _read_sections(filename: str) -> Iterable[tuple[str, str]]:
    text = (resources.files("docfill") / "data" / filename).read_text(encoding="utf8")
    section = ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        for word in line.split():
            yield word, section


@cache
def given_names() -> WordList:
    """First names; the section is the sex they are given to: ``M``, ``F`` or ``U`` (both)."""
    return WordList(_read_sections("ro_given_names.txt"))


@cache
def surnames() -> WordList:
    """Family names; the section is the community: ``RO``, ``HU``, ``DE`` or ``XX`` (others)."""
    return WordList(_read_sections("ro_surnames.txt"))


@cache
def street_words() -> WordList:
    """Words with diacritics that are common in street names (``Libertății``)."""
    return WordList(_read_sections("ro_street_words.txt"))


# --------------------------------------------------------------------------- localities

KINDS = {"M": "Mun.", "O": "Oraș", "C": "Com.", "S": "Sat"}


@dataclass(frozen=True)
class Locality:
    kind: str  # M municipality, O town, C commune, S village (locality of a commune)
    name: str
    county: str  # the code a card prints: CJ, AB, B


def _distance(a: str, b: str, limit: int) -> int:
    """Edit distance of two short strings, ``limit + 1`` as soon as it is certain to exceed it."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (char_a != char_b))
            )
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


class Gazetteer:
    """The localities of Romania by county, each with its kind (municipality, town, commune,
    village): what a card can print after ``Jud.`` and ``Mun.`` / ``Oraș`` / ``Com.`` / ``Sat``."""

    def __init__(self, entries: Iterable[Locality]):
        self._by_county: dict[str, dict[str, list[Locality]]] = defaultdict(
            lambda: defaultdict(list)
        )
        self._by_plain: dict[str, list[Locality]] = defaultdict(list)
        for locality in entries:
            key = plain(locality.name)
            self._by_county[locality.county][key].append(locality)
            self._by_plain[key].append(locality)

    @property
    def counties(self) -> set[str]:
        return set(self._by_county)

    def entries(self, kinds: str = "MOCS") -> Iterable[Locality]:
        for pool in self._by_county.values():
            for found in pool.values():
                yield from (item for item in found if item.kind in kinds)

    def find(self, name: str, county: str | None = None, kinds: str = "MOCS") -> list[Locality]:
        """The localities called ``name`` (accents, hyphens and capitals do not matter), in
        ``county`` or anywhere in the country."""
        pool = self._by_county.get(county, {}) if county else self._by_plain
        return [found for found in pool.get(plain(name), []) if found.kind in kinds]

    def _names(self, county: str | None, kinds: str) -> Iterable[tuple[str, Locality]]:
        pools = [self._by_county.get(county, {})] if county else [self._by_plain]
        for pool in pools:
            for key, found in pool.items():
                for locality in found:
                    if locality.kind in kinds:
                        yield key, locality

    def completions(
        self, partial: str, county: str | None, kinds: str = "MOCS", limit: int = 3
    ) -> list[Locality]:
        """The localities whose name begins with ``partial`` (a name cut off by glare or a fold).
        Two letters are enough within a county, which has few names; three in the country."""
        key = plain(partial)
        if len(key) < (2 if county else 3):
            return []
        found = {
            (locality.kind, locality.name): locality
            for name, locality in self._names(county, kinds)
            if name.startswith(key) and name != key
        }
        return sorted(found.values(), key=lambda item: (len(item.name), item.name))[:limit]

    def similar(
        self, name: str, county: str | None, kinds: str = "MOCS", limit: int = 3
    ) -> list[Locality]:
        """The localities spelled almost like ``name``: one letter off (two for long names), or,
        within a county, whose beginning is (a name cut off by glare, with a letter misread)."""
        key = plain(name)
        scored = []
        for other, locality in self._names(county, kinds):
            if len(key) >= 5:
                allowed = 1 if len(key) < 9 else 2
                if (distance := _distance(key, other, allowed)) <= allowed:
                    scored.append((distance, locality.name, locality.kind, locality))
                    continue
            if (
                county
                and len(key) >= 3
                and len(other) > len(key)
                and _distance(key, other[: len(key)], 1) <= 1
            ):
                scored.append((2, locality.name, locality.kind, locality))
        scored.sort(key=lambda item: item[:3])
        return [item[3] for item in scored][:limit]


@cache
def gazetteer() -> Gazetteer:
    text = (resources.files("docfill") / "data" / "ro_localities.txt").read_text("utf8")
    county, entries = "", []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            county = line[1:-1]
        else:
            kind, _, name = line.partition(" ")
            entries.append(Locality(kind, name, county))
    return Gazetteer(entries)
