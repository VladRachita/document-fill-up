"""The Romanian reference data (names, localities) and the lookups on top of it."""

import re
import unicodedata
from collections import defaultdict
from pathlib import Path

import pytest

from docfill import lexicon
from docfill.lexicon import (
    gazetteer,
    given_names,
    normalize,
    plain,
    restore_marks,
    street_words,
    surnames,
    with_case_of,
)

DATA = Path(lexicon.__file__).parent / "data"
NAME_FILES = ["ro_given_names.txt", "ro_surnames.txt", "ro_street_words.txt"]


def sections(filename: str) -> dict[str, list[str]]:
    found: dict[str, list[str]] = defaultdict(list)
    section = ""
    for line in (DATA / filename).read_text(encoding="utf8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("["):
            section = line[1:-1]
        else:
            found[section].extend(line.split())
    return found


# --------------------------------------------------------------------------- the files


@pytest.mark.parametrize("filename", NAME_FILES)
def test_the_name_lists_are_clean(filename):
    seen: set[str] = set()
    for section, words in sections(filename).items():
        assert words == sorted(words, key=lambda w: (plain(w), w)), f"[{section}] is not sorted"
        for word in words:
            assert unicodedata.normalize("NFC", word) == word, f"{word}: not NFC"
            assert not set(word) & set("ŞşŢţ"), f"{word}: cedilla instead of comma-below"
            assert re.fullmatch(r"[^\W\d_]+", word), f"{word}: not a single word of letters"
            assert word[0].isupper(), f"{word}: names start with a capital"
            assert word not in seen, f"{word}: listed twice"
            seen.add(word)


@pytest.mark.parametrize("words", [given_names, surnames, street_words])
def test_every_listed_word_is_found_as_listed(words):
    lookup = words()
    assert len(lookup) > 150
    for word in lookup.words():
        assert lookup.lookup(word).status == "exact"
        assert lookup.lookup(word.upper()).status == "exact"  # the card prints capitals


@pytest.mark.parametrize("words", [given_names, surnames, street_words])
def test_a_word_with_diacritics_comes_back_from_its_plain_spelling(words):
    lookup, forms = words(), defaultdict(set)
    for word in lookup.words():
        forms[plain(word)].add(word)
    checked = 0
    for key, spellings in forms.items():
        (word,) = spellings if len(spellings) == 1 else (None,)
        if word and lookup.lookup(key.title()).status == "restorable":
            assert lookup.lookup(key.title()).spelling == word
            checked += 1
    assert checked > 100


def test_the_name_lists_have_the_names_a_card_prints():
    given, family = given_names(), surnames()
    for name in ("Ioana", "Ioan", "Ștefan", "Mădălina", "Cătălin", "Lăcrămioara", "Zoltán"):
        assert given.lookup(name).status == "exact", name
    for name in ("Popescu", "Ionescu", "Ștefănescu", "Mureșan", "Țurcanu", "Kovács", "Iohannis"):
        assert family.lookup(name).status == "exact", name
    assert given.section("Ion") == "M" and given.section("Ioana") == "F"
    assert given.section("Alex") == "U" and given.section("Joana") is None


def test_joana_is_not_a_romanian_first_name():
    assert not given_names().known("Joana") and not given_names().known("Jonel")
    assert given_names().known("Julia") and given_names().known("Iulia")  # both are names


# --------------------------------------------------------------------------- looking words up


def test_normalize_and_plain():
    assert normalize("Ştefan Ţurcanu") == "Ștefan Țurcanu"  # cedilla -> comma-below
    assert plain("ȘTEFĂNESCU") == "stefanescu"
    assert plain("Cluj-Napoca") == plain("CLUJ NAPOCA") == "cluj napoca"


@pytest.mark.parametrize(
    ("read", "listed", "restored"),
    [
        ("Stefanescu", "Ștefănescu", "Ștefănescu"),
        ("STEFANESCU", "Ștefănescu", "ȘTEFĂNESCU"),
        ("Ștefanescu", "Ștefănescu", "Ștefănescu"),  # some of the diacritics were read
        ("Stefänescu", "Ștefănescu", "Ștefănescu"),  # another diacritic, misread
        ("Marin", "Mărin", "Mărin"),
        ("Mărin", "Marin", None),  # a diacritic that was read is never taken away
        ("Mărin", "Mârin", None),  # nor replaced by another one
        ("AGNES", "Ágnes", None),  # only Romanian diacritics are ever added
        ("Popescu", "Ionescu", None),
    ],
)
def test_restore_marks(read, listed, restored):
    assert restore_marks(read, listed) == restored


def test_the_capitals_of_the_reading_are_kept():
    assert with_case_of("IONESCU", "Ionescu") == "IONESCU"
    assert with_case_of("ionescu", "Ionescu") == "ionescu"
    assert with_case_of("Ionescu", "Ionescu") == "Ionescu"


def test_lookups_say_what_they_know():
    given = given_names()
    assert given.lookup("Ioana").status == "exact"
    assert given.lookup("Stefan") == lexicon.Lookup("restorable", "Ștefan")
    assert given.lookup("Madalina") == lexicon.Lookup("restorable", "Mădălina")
    assert given.lookup("Zoltan").status == "exact"  # listed without its accent too
    assert given.lookup("AGNES").status == "known"  # listed as Ágnes: left as it was read
    assert given.lookup("MARIOARA").status == "exact"  # two spellings are listed: no guess
    assert given.lookup("Xylophone").status == "unknown"


def test_look_alike_letters_find_the_name_they_stand_for():
    given = given_names()
    swaps = {"i": "j", "j": "i", "o": "q", "q": "o"}
    assert given.neighbours("Joana", swaps) == ["Ioana"]
    assert given.neighbours("Qana", swaps) == ["Oana"]
    assert given.neighbours("Xylophone", swaps) == []
    assert given.with_letter("Onut", "i") == ["Ionuț"]  # the thin letter was lost
    assert surnames().completions("Dumitres") == ["Dumitrescu"]


# --------------------------------------------------------------------------- the localities


def test_the_register_has_every_county_and_kind_of_locality():
    register = gazetteer()
    assert len(register.counties) == 42 and "B" in register.counties
    municipalities = list(register.entries("M"))
    towns, communes = list(register.entries("O")), list(register.entries("C"))
    assert 100 <= len(municipalities) <= 106 and 210 <= len(towns) <= 222
    assert 2800 <= len(communes) <= 2900 and len(list(register.entries("S"))) > 12000


@pytest.mark.parametrize(
    ("name", "county", "kind", "spelling"),
    [
        ("Cluj-Napoca", "CJ", "M", "Cluj-Napoca"),
        ("Huedin", "CJ", "O", "Huedin"),
        ("Hărman", "BV", "C", "Hărman"),
        ("Podu Oltului", "BV", "S", "Podu Oltului"),
        ("Săcălaz", "TM", "C", "Săcălaz"),
        ("Roșiori de Vede", "TR", "M", "Roșiori de Vede"),
        ("București", "B", "M", "București"),
        ("Piatra-Neamț", "NT", "M", "Piatra-Neamț"),
    ],
)
def test_localities_are_found_with_their_kind(name, county, kind, spelling):
    found = gazetteer().find(name, county, kind)
    assert [item.name for item in found] == [spelling]


def test_accents_hyphens_and_capitals_do_not_matter_when_looking_up():
    register = gazetteer()
    for spelling in ("Harman", "HĂRMAN", "hărman"):
        assert register.find(spelling, "BV", "C")
    assert register.find("Piatra Neamt", "NT", "M") == register.find("Piatra-Neamț", "NT", "M")
    assert not register.find("Hărman", "CJ")  # there is none in that county
    assert {item.county for item in register.find("Ștefănești")} == {"AG", "BT", "CL", "GJ", "VL"}


def test_names_cut_off_or_misread_are_matched_to_the_register():
    register = gazetteer()
    assert [item.name for item in register.completions("Huedi", "CJ", "O")] == ["Huedin"]
    assert [item.name for item in register.completions("Sib", None, "M")] == ["Sibiu"]
    assert [item.name for item in register.completions("Me", "SB", "MO")] == ["Mediaș"]
    assert not register.completions("Me", None, "MO")  # two letters are enough in a county only
    assert [item.name for item in register.similar("Hucdin", "CJ", "O")] == ["Huedin"]
    assert [item.name for item in register.similar("Drobeta-Tumu Severin", None, "MOC")] == [
        "Drobeta-Turnu Severin"
    ]
    # glare took the end and a letter was misread: "Set" for "Seb(eș)"
    assert [item.name for item in register.similar("Set", "AB", "MO")] == ["Sebeș"]
    assert not register.similar("Zzzzzzz", "AB", "MOC")
