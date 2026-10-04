"""Names checked against the lists of Romanian names: what is restored, what is guessed, and how
a difference with the machine readable zone is settled."""

import pytest

from docfill.names import FAMILY, GIVEN, fix_name, settle


@pytest.mark.parametrize(
    ("read", "kind", "value"),
    [
        ("STEFANESCU", FAMILY, "ȘTEFĂNESCU"),
        ("Stefanescu", FAMILY, "Ștefănescu"),
        ("Stefănescu", FAMILY, "Ștefănescu"),  # the diacritics that were read are kept
        ("Ștefan", GIVEN, "Ștefan"),
        ("Stefan", GIVEN, "Ștefan"),
        ("ŞTEFAN", GIVEN, "ȘTEFAN"),  # cedilla -> comma-below
        ("LACRAMIOARA-MADALINA", GIVEN, "LĂCRĂMIOARA-MĂDĂLINA"),
        ("Ana Maria", GIVEN, "Ana Maria"),
        ("Mărin", FAMILY, "Mărin"),  # never takes a diacritic away
        ("AGNES", GIVEN, "AGNES"),  # a Hungarian name: its accent is not ours to add
        ("Kovacs", FAMILY, "Kovacs"),
        ("MARIOARA", GIVEN, "MARIOARA"),  # listed with and without ă: no guess
        ("Hrițcu", FAMILY, "Hrițcu"),  # not listed: left as read
    ],
)
def test_the_diacritics_come_from_the_lists(read, kind, value):
    fixed = fix_name(read, kind)
    assert fixed.value == value
    assert fixed.guess is None and fixed.suggestion is None


@pytest.mark.parametrize(
    ("read", "kind", "value"),
    [
        ("I0AN", GIVEN, "IOAN"),
        ("Cr1stian", GIVEN, "Cristian"),
        ("CR1STIAN", GIVEN, "CRISTIAN"),
        ("Ion-Andre1", GIVEN, "Ion-Andrei"),
        ("P0PESCU", FAMILY, "POPESCU"),
        ("D1NU", FAMILY, "DINU"),
    ],
)
def test_digits_in_a_name_are_the_letters_they_look_like(read, kind, value):
    fixed = fix_name(read, kind)
    assert fixed.value == value and not fixed.uncertain_digits


def test_digits_that_do_not_make_a_listed_name_are_flagged():
    fixed = fix_name("Xy1ophone", GIVEN)
    assert fixed.value == "Xyiophone" and fixed.uncertain_digits


@pytest.mark.parametrize(
    ("read", "kind", "guess"),
    [
        ("JOANA", GIVEN, "IOANA"),
        ("Joana-Iulia", GIVEN, "Ioana-Iulia"),
        ("JONEL", GIVEN, "IONEL"),
        ("JONESCU", FAMILY, "IONESCU"),
        ("Qana", GIVEN, "Oana"),
    ],
)
def test_a_name_that_is_one_look_alike_letter_from_a_romanian_name_is_guessed(read, kind, guess):
    fixed = fix_name(read, kind)
    assert fixed.guess == guess and fixed.value == read


@pytest.mark.parametrize(
    ("read", "kind"),
    [
        ("Julia", GIVEN),  # a name of its own, whatever Iulia is
        ("Jacob", FAMILY),
        ("Jipa", FAMILY),
        ("Xylophone", GIVEN),
        ("Ana", GIVEN),
        ("I.", GIVEN),
    ],
)
def test_a_name_that_is_listed_or_has_no_neighbour_is_never_guessed(read, kind):
    fixed = fix_name(read, kind)
    assert fixed.guess is None and fixed.suggestion is None


@pytest.mark.parametrize(
    ("read", "kind", "suggestion"),
    [
        ("Onut-Marian", GIVEN, "Ionuț-Marian"),  # the thin letter was lost
        ("DUMITRES", FAMILY, "DUMITRESCU"),  # cut off by glare
    ],
)
def test_a_lost_letter_or_a_name_cut_off_is_only_suggested(read, kind, suggestion):
    fixed = fix_name(read, kind)
    assert fixed.suggestion == suggestion and fixed.guess is None and fixed.value == read


# --------------------------------------------------------------------------- the zone


@pytest.mark.parametrize(
    ("printed", "zone", "kind", "value", "from_zone"),
    [
        ("JOANA-IULIA", "IOANA IULIA", GIVEN, "IOANA-IULIA", True),  # the printed one is no name
        ("IOANA-IULIA", "IQANA IULIA", GIVEN, "IOANA-IULIA", False),  # the zone's is no name
        ("Joana", "Ioana", GIVEN, "Ioana", True),
        ("Jonescu", "Ionescu", FAMILY, "Ionescu", True),
        ("Ioana-Iulia", "Ioana Iulia", GIVEN, "Ioana-Iulia", False),  # the same, but for the hyphen
    ],
)
def test_the_lists_settle_a_difference_with_the_zone(printed, zone, kind, value, from_zone):
    settled = settle(printed, zone, kind)
    assert settled is not None
    assert (settled.value, settled.from_zone) == (value, from_zone)


@pytest.mark.parametrize(
    ("printed", "zone", "kind"),
    [
        ("JULIA", "IULIA", GIVEN),  # both are names a card prints
        ("XYLOPHONE", "XYLOPHONF", GIVEN),  # neither is
        ("IOANA-IULIA", "IOANA", GIVEN),  # not the same number of words
    ],
)
def test_the_lists_do_not_decide_what_they_cannot_tell(printed, zone, kind):
    assert settle(printed, zone, kind) is None
