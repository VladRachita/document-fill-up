"""Reading the fields of a Romanian identity card correctly: the CNP, the names, the places and the
issuing office, from the text OCR gives and from card images (fictitious people only).

The cases are the ways real scans go wrong: a CNP that happens to look like a payment card, a
capital I read as a lowercase l, a county code read as a brace, a line cut off by glare, a
machine readable zone damaged or split by a tilted photograph."""

from dataclasses import replace
from datetime import date

import pytest

from docfill.extraction import FieldExtractor
from docfill.extraction.fields import FIELDS
from docfill.extraction.idcard import repair_name, repair_text, review, suspicious
from docfill.models import DocumentType, ExtractedField, ExtractionResult, Page, RawDocument
from docfill.mrz import make_td2
from docfill.samples import Person, ro_id_card_jpeg
from docfill.sanitize import sanitize
from tests.conftest import requires_tesseract

ALBA = Person(
    last_name="TOMA",
    first_name="IOANA-IULIA",
    sex="F",
    birth=date(1992, 12, 5),
    birth_county_code="AB",
    birth_locality="Mun.Sebeș",
    county_code="AB",
    locality="Mun.Alba Iulia",
    street_line="Str.Iuliu Maniu nr.3 bl.12 sc.2 et.4 ap.15",
    series="AX",
    number="334455",
    issued_by="SPCLEP Alba Iulia",
    issued=date(2023, 2, 20),
    expires=date(2033, 12, 5),
    serial="266",
)
# Bucharest prints the sector instead of a county, and the person was born in a sector too.
BUCHAREST = Person(
    last_name="GEORGESCU",
    first_name="MIHAELA-ELENA",
    sex="F",
    birth=date(1990, 2, 17),
    birth_county_code="B",
    birth_line="Mun.București Sec.2",
    domicile_lines=("Mun.București Sec.4", "Str.Rudariilor nr.14 bl.3 sc.1 et.2 ap.7"),
    series="RT",
    number="845219",
    issued_by="SPCLEP Sector 4",
    issued=date(2022, 9, 12),
    expires=date(2032, 2, 17),
    serial="204",
)
# A town ("Or.") whose county code OCR reads as "C}" (the hook of the J).
HUEDIN = Person(
    last_name="POP",
    first_name="LIVIU",
    birth=date(1975, 6, 9),
    birth_county_code="CJ",
    birth_locality="Or.Huedin",
    county_code="CJ",
    locality="Or.Huedin",
    street_line="Str.Horea nr.25 ap.2",
    series="CJ",
    number="290841",
    issued_by="SPCLEP Huedin",
    issued=date(2019, 3, 18),
    expires=date(2029, 6, 9),
    serial="052",
)
# One valid CNP in ten also passes the Luhn check of payment cards.
LUHN = Person(
    birth=date(1946, 10, 2), birth_county_code="CJ", serial="039", series="CJ", number="718264"
)


def card_text(
    person: Person, *, cnp: str | None = None, mrz: list[str] | None = None, **lines
) -> str:
    """The text OCR gives for ``person``'s card: the label above its value. ``cnp=""`` leaves
    the CNP line out, ``mrz=[]`` the machine readable zone; ``lines`` replaces values."""
    shown = {
        "last_name": person.last_name,
        "first_name": person.first_name,
        "birth": person.birth_place_line,
        "domicile": "\n".join(person.domicile_card_lines),
        "issued_by": person.issued_by,
        "validity": f"{person.issued:%d.%m.%y}-{person.expires:%d.%m.%Y}",
        **lines,
    }
    cnp = person.cnp if cnp is None else cnp
    zone = person.mrz() if mrz is None else mrz
    return "\n".join(
        [
            "ROUMANIE ROMÂNIA ROMANIA",
            "CARTE DE IDENTITATE IDENTITY CARD",
            f"SERIA {person.series} NR {person.number}",
            f"CNP {cnp}" if cnp else "",
            "Nume/Nom/Last name",
            shown["last_name"],
            "Prenume/Prenom/First name",
            shown["first_name"],
            "Cetățenie/Nationalite/Nationality",
            "Română / ROU",
            "Sex/Sexe/Sex",
            person.sex,
            "Loc naștere/Lieu de naissance/Place of birth",
            shown["birth"],
            "Domiciliu/Adresse/Address",
            shown["domicile"],
            "Emisă de/Delivree par/Issued by",
            shown["issued_by"],
            "Valabilitate/Validite/Validity",
            shown["validity"],
            *zone,
        ]
    )


def read(settings, text: str) -> ExtractionResult:
    """What docfill extracts from the text of an identity card (cleaned like a real scan)."""
    page = Page(number=1, text=text, ocr=True)
    raw = RawDocument(source="card.jpg", doc_type=DocumentType.IMAGE, pages=[page])
    return FieldExtractor(settings, use_ner=False).extract(sanitize(raw), "id_card")


def filled(result: ExtractionResult, settings) -> dict[str, str]:
    return result.values(settings.min_confidence)


def held_back(result: ExtractionResult, name: str, settings) -> bool:
    """Is there a value for ``name`` that is shown for review but not filled in?"""
    field = result.fields.get(name)
    return field is not None and field.confidence < settings.min_confidence


# --------------------------------------------------------------------------- the whole card


def test_every_field_of_the_card_is_read(settings):
    values = filled(read(settings, card_text(ALBA)), settings)
    assert {k: values.get(k) for k in EXPECTED_ALBA} == EXPECTED_ALBA


EXPECTED_ALBA = {
    "last_name": "Toma",
    "first_name": "Ioana-Iulia",
    "id_series": "AX",
    "id_number": "334455",
    "cnp": ALBA.cnp,
    "date_of_birth": "05.12.1992",
    "sex": "F",
    "place_of_birth": "Mun. Sebeș",
    "birth_county": "Alba",
    "birth_country": "România",
    "city": "Mun. Alba Iulia",
    "region": "Alba",
    "street": "Iuliu Maniu",
    "street_number": "3",
    "building": "12",
    "entrance": "2",
    "floor": "4",
    "apartment": "15",
    "id_issued_by": "SPCLEP Alba Iulia",
    "id_issue_date": "20.02.2023",
    "id_expiry_date": "05.12.2033",
}


def test_bucharest_keeps_the_sector_of_birth_and_of_the_domicile(settings):
    values = filled(read(settings, card_text(BUCHAREST)), settings)
    assert values["place_of_birth"] == "Mun. București Sector 2"
    assert values["birth_county"] == "București" and values["birth_country"] == "România"
    assert (values["city"], values["region"]) == ("Mun. București", "Sector 4")
    assert (values["street"], values["street_number"]) == ("Rudariilor", "14")
    assert (values["building"], values["entrance"], values["floor"], values["apartment"]) == (
        "3",
        "1",
        "2",
        "7",
    )


def test_a_town_and_a_county_code_misread_as_a_brace(settings):
    text = card_text(
        HUEDIN, birth="Jud.C} Or.Huedin", domicile="Jud.C} Or.Huedin\nStr.Horea nr.25 ap.2"
    )
    values = filled(read(settings, text), settings)
    assert values["place_of_birth"] == "Oraș Huedin" and values["birth_county"] == "Cluj"
    assert (values["city"], values["region"]) == ("Oraș Huedin", "Cluj")
    assert (values["street"], values["street_number"], values["apartment"]) == ("Horea", "25", "2")


# --------------------------------------------------------------------------- the capital I


@pytest.mark.parametrize(
    ("misread", "printed"),
    [
        ("Jud.AB Mun.Alba lulia", "Jud.AB Mun.Alba Iulia"),
        ("Str.luliu Maniu nr.3", "Str.Iuliu Maniu nr.3"),
        ("Str.juliu Maniu nr.3", "Str.Iuliu Maniu nr.3"),
        ("SPCLEP lași", "SPCLEP Iași"),
        ("Jud.IS Mun.lasi", "Jud.IS Mun.Iasi"),
        ("Jud.!S Mun.Iasi", "Jud.IS Mun.Iasi"),
        ("jud.AB mun.Sebes", "Jud.AB Mun.Sebes"),
        ("Jud.C} Or.Huedin", "Jud.CJ Or.Huedin"),
        # words that really start with a lowercase l, or with J, are left alone
        ("Sat Valea lui Mihai nr.3", "Sat Valea lui Mihai nr.3"),
        ("Str.Principală lot 4", "Str.Principală lot 4"),
        ("Str.Jiului nr.2 bl.A2", "Str.Jiului nr.2 bl.A2"),
    ],
)
def test_text_of_a_place_is_repaired_as_the_card_prints_it(misread, printed):
    assert repair_text(misread) == printed


@pytest.mark.parametrize(
    ("misread", "printed"),
    [
        ("lON", "ION"),
        ("ANDREl", "ANDREI"),
        ("IOANA-lULIA", "IOANA-IULIA"),
        ("MARlA", "MARIA"),
        ("1ON", "ION"),
        ("VLAD", "VLAD"),  # a real L is not touched
        ("Paul", "Paul"),
    ],
)
def test_names_are_printed_in_capitals(misread, printed):
    assert repair_name(misread) == printed


def test_misread_capital_i_is_repaired_in_the_whole_card(settings):
    text = card_text(
        ALBA,
        first_name="lOANA-lULIA",
        domicile="Jud.AB Mun.Alba lulia\nStr.luliu Maniu nr.3 bl.12 sc.2 et.4 ap.15",
        issued_by="SPCLEP Alba lulia",
    )
    values = filled(read(settings, text), settings)
    assert values["first_name"] == "Ioana-Iulia"
    assert (values["city"], values["street"]) == ("Mun. Alba Iulia", "Iuliu Maniu")
    assert values["id_issued_by"] == "SPCLEP Alba Iulia"


# --------------------------------------------------------------------------- the CNP


def test_a_cnp_that_looks_like_a_payment_card_is_still_read(settings):
    # 1461002120395 is a valid CNP and also passes the Luhn check used to redact card numbers
    assert LUHN.cnp == "1461002120395"
    result = read(settings, card_text(LUHN, mrz=[]))
    assert filled(result, settings)["cnp"] == LUHN.cnp


def test_the_cnp_is_read_from_the_zone_when_the_printed_one_is_missing(settings):
    result = read(settings, card_text(ALBA, cnp=""))
    assert filled(result, settings)["cnp"] == ALBA.cnp
    assert result.fields["cnp"].source == "mrz"


def test_printed_cnp_and_zone_confirm_each_other(settings):
    cnp = read(settings, card_text(ALBA)).fields["cnp"]
    assert cnp.value == ALBA.cnp and cnp.confidence >= 0.97 and not cnp.issues


def test_a_misread_cnp_is_offered_for_correction_not_filled(settings):
    wrong = ALBA.cnp[:-1] + str((int(ALBA.cnp[-1]) + 1) % 10)
    result = read(settings, card_text(ALBA, cnp=wrong, mrz=[]))
    assert result.fields["cnp"].value == wrong
    assert held_back(result, "cnp", settings)
    assert "CNP invalid" in result.fields["cnp"].issues[0]


def test_the_zone_corrects_a_misread_printed_cnp(settings):
    wrong = ALBA.cnp[:-1] + str((int(ALBA.cnp[-1]) + 1) % 10)
    result = read(settings, card_text(ALBA, cnp=wrong))
    assert filled(result, settings)["cnp"] == ALBA.cnp


def test_digits_read_as_letters_in_the_printed_cnp_are_put_back(settings):
    lookalikes = ALBA.cnp.replace("1", "l").replace("0", "O")
    assert lookalikes != ALBA.cnp
    result = read(settings, card_text(ALBA, cnp=lookalikes, mrz=[]))
    assert filled(result, settings)["cnp"] == ALBA.cnp


def test_random_letters_after_the_label_are_not_a_cnp(settings):
    assert "cnp" not in read(settings, card_text(ALBA, cnp="BOSSZZTSIBOLG", mrz=[])).fields


def test_printed_cnp_and_zone_that_disagree_are_both_held_back(settings):
    other = Person(**{**ALBA.__dict__, "serial": "267"})  # the same birth date, another person
    assert other.cnp != ALBA.cnp and other.cnp[:7] == ALBA.cnp[:7]
    result = read(settings, card_text(ALBA, cnp=other.cnp))
    assert held_back(result, "cnp", settings)
    assert "Diferă de CNP-ul din zona citibilă automat" in result.fields["cnp"].issues[0]


# --------------------------------------------------------------------------- the zone


def test_the_zone_is_found_when_a_tilted_photo_splits_its_first_line(settings):
    first, second = ALBA.mrz()
    text = card_text(ALBA, cnp="", mrz=[first[:15], second, first[15:]])
    result = read(settings, text)
    assert result.fields["cnp"].source == "mrz" and result.fields["cnp"].value == ALBA.cnp
    assert filled(result, settings)["id_expiry_date"] == "05.12.2033"


def test_a_short_name_does_not_make_the_zone_look_like_noise(settings):
    # "POP<<LIVIU" followed by many "<": the line was once dropped as "OCR noise"
    first, second = HUEDIN.mrz()
    assert first.count("<") > 20
    result = read(settings, card_text(HUEDIN, cnp="", mrz=[first, second]))
    assert result.fields["cnp"].source == "mrz"
    assert result.fields["id_series"].source == "mrz"


# --------------------------------------------------------------------------- names and the zone


def zone_with_names(person: Person, names: str) -> list[str]:
    first, second = person.mrz()
    return [("IDROU" + names).ljust(36, "<"), second]


def test_a_name_cut_off_by_glare_is_finished_from_the_zone(settings):
    result = read(settings, card_text(ALBA, first_name="IOANA-IUL"))
    name = result.fields["first_name"]
    assert name.value == "Ioana-Iulia" and name.source == "derived"
    assert any("Trunchiat pe scanare" in issue for issue in name.issues)


def test_a_name_finished_by_the_zone_keeps_no_note_about_being_unknown(settings):
    # "ANDRE" is no first name and a list could finish it ("ANDREI"): the zone does, for sure
    person = replace(ALBA, first_name="ION-ANDREI")
    name = read(settings, card_text(person, first_name="ION-ANDRE")).fields["first_name"]
    assert name.value == "Ion-Andrei" and name.confidence >= 0.8
    assert not any("Nu este un nume românesc cunoscut" in issue for issue in name.issues)


def test_without_the_zone_a_name_that_may_be_cut_off_is_offered_not_filled(settings):
    person = replace(ALBA, first_name="ION-ANDREI")
    result = read(settings, card_text(person, first_name="ION-ANDRE", mrz=[]))
    assert held_back(result, "first_name", settings)
    assert "ați vrut să scrieți Ion-Andrei" in result.fields["first_name"].issues[0]
    assert "Ion-Andrei" in {c.value for c in result.candidates["first_name"]}


def test_a_first_name_that_is_no_romanian_name_is_settled_by_the_zone(settings):
    # "IOANA" read as "JOANA": the zone has IOANA, and "Joana" is not a Romanian first name
    result = read(settings, card_text(ALBA, first_name="JOANA-IULIA"))
    name = result.fields["first_name"]
    assert name.value == "Ioana-Iulia" and name.confidence >= 0.9
    assert not name.issues and name.original is None


def test_two_readings_that_are_both_romanian_names_are_left_to_a_person(settings):
    # "IULIA" read as "JULIA": both are names a card prints, so the list cannot tell which
    zone = zone_with_names(ALBA, "TOMA<<IULIA")
    result = read(settings, card_text(ALBA, first_name="JULIA", mrz=zone))
    assert held_back(result, "first_name", settings)
    assert "Diferă de zona citibilă automat" in result.fields["first_name"].issues[0]
    assert [c.confidence < 0.5 for c in result.candidates["first_name"]] == [True, True]


def test_a_misread_zone_does_not_overrule_a_printed_romanian_name(settings):
    # the zone reads "IOQANA IULIA": not a name, while the printed one is
    zone = zone_with_names(ALBA, "TOMA<<IOQANA<IULIA")
    name = read(settings, card_text(ALBA, mrz=zone)).fields["first_name"]
    assert name.value == "Ioana-Iulia" and 0.5 <= name.confidence <= 0.8
    assert "nu este un nume românesc" in name.issues[0]


def test_without_the_zone_a_misread_first_name_is_corrected_with_a_note(settings):
    result = read(settings, card_text(ALBA, first_name="JOANA-IULIA", mrz=[]))
    name = result.fields["first_name"]
    assert name.value == "Ioana-Iulia" and 0.5 <= name.confidence < 0.9
    assert name.issues[0].startswith("Citit ca „Joana-Iulia”") and name.original == "Joana-Iulia"
    # what was read stays one click away
    assert "Joana-Iulia" in {c.value for c in result.candidates["first_name"]}


def test_a_zone_that_has_the_unusual_name_too_keeps_it(settings):
    # the person really is called Joana: the zone says so as well
    person = replace(ALBA, first_name="JOANA")
    name = read(settings, card_text(person)).fields["first_name"]
    assert name.value == "Joana" and name.confidence >= 0.9 and not name.issues


def test_a_surname_that_may_be_misread_is_offered_not_filled(settings):
    person = replace(ALBA, last_name="JONESCU")
    result = read(settings, card_text(person, mrz=[]))
    assert held_back(result, "last_name", settings)
    assert "Ionescu" in result.fields["last_name"].issues[0]
    assert "Ionescu" in {c.value for c in result.candidates["last_name"]}


def test_a_surname_that_is_not_listed_is_left_alone(settings):
    # a rare surname the lists do not know is not an error
    person = replace(ALBA, last_name="HRIȚCU")
    assert filled(read(settings, card_text(person, mrz=[])), settings)["last_name"] == "Hrițcu"


def test_names_get_their_diacritics_back_from_the_lists(settings):
    person = replace(ALBA, last_name="STEFANESCU", first_name="LACRAMIOARA-MADALINA")
    values = filled(read(settings, card_text(person, mrz=[])), settings)
    assert values["last_name"] == "Ștefănescu"
    assert values["first_name"] == "Lăcrămioara-Mădălina"


def test_a_name_in_the_zone_is_finished_with_its_diacritics(settings):
    person = replace(ALBA, first_name="CĂTĂLINA-ANDREEA")
    result = read(settings, card_text(person, first_name="CĂTĂLINA-ANDR"))
    assert result.fields["first_name"].value == "Cătălina-Andreea"


def test_hungarian_names_are_left_as_they_were_read(settings):
    # the lists know them, and know that their accents are not ours to add
    person = replace(ALBA, last_name="KOVACS", first_name="ZOLTAN")
    values = filled(read(settings, card_text(person, mrz=[])), settings)
    assert values["last_name"] == "Kovacs" and values["first_name"] == "Zoltan"


@pytest.mark.parametrize(
    "zone_names",
    [
        "TOMA<<IOANA<IULIA",  # as printed
        "TOMA<<IOANA<IUL1A",  # a digit read in place of a letter
        "TOMA<<IOANA<IULlA",  # a lowercase l read in place of the capital I
        "TOMA<<IOANA<IULIACK<<",  # "<" fillers read as letters
    ],
)
def test_noise_in_the_zone_does_not_discredit_a_good_printed_name(settings, zone_names):
    result = read(settings, card_text(ALBA, mrz=zone_with_names(ALBA, zone_names)))
    name = result.fields["first_name"]
    assert name.value == "Ioana-Iulia" and name.confidence >= 0.9 and not name.issues


def test_names_that_only_the_zone_has_get_their_diacritics_from_the_lists(settings):
    person = replace(ALBA, last_name="ȘTEFĂNESCU", first_name="CĂTĂLINA-ANDREEA")
    result = read(settings, card_text(person, last_name="", first_name=""))
    assert result.fields["last_name"].value == "Ștefănescu"
    assert result.fields["first_name"].value == "Cătălina Andreea"  # the zone has no hyphen
    assert result.fields["last_name"].source == "mrz"


def test_a_first_name_that_does_not_fit_the_sex_is_flagged(settings):
    # IOANA-IULIA on a card of a man: the name or the CNP was misread, or cut ("IOAN")
    person = replace(ALBA, sex="M")
    name = read(settings, card_text(person)).fields["first_name"]
    assert any("este de obicei prenume de femeie" in issue for issue in name.issues)
    assert not any(
        "de obicei" in i for i in read(settings, card_text(ALBA)).fields["first_name"].issues
    )


# ------------------------------------------------------------------- places and the register


def test_a_commune_gets_its_diacritics_from_the_register(settings):
    person = replace(ALBA, county_code="TM", locality="Com.Sacalaz")
    values = filled(read(settings, card_text(person, mrz=[])), settings)
    assert values["city"] == "Com. Săcălaz" and values["region"] == "Timiș"


def test_a_street_gets_its_diacritics_from_the_lists(settings):
    person = replace(ALBA, street_line="Str.Libertatii nr.5")
    street = filled(read(settings, card_text(person, mrz=[])), settings)["street"]
    assert street == "Libertății"


def test_a_place_in_the_wrong_county_is_shown_not_filled(settings):
    # Cluj-Napoca is in județul Cluj: the code or the name was misread
    person = replace(ALBA, county_code="AB", locality="Mun.Cluj-Napoca")
    result = read(settings, card_text(person, mrz=[]))
    assert held_back(result, "city", settings)
    assert "județul Alba (există una cu acest nume în: Cluj)" in result.fields["city"].issues[-1]


def test_a_town_cut_off_by_glare_is_proposed_not_filled(settings):
    result = read(settings, card_text(HUEDIN, birth="Jud.CJ Or.Huedi", mrz=[]))
    place = result.fields["place_of_birth"]
    assert place.value == "Oraș Huedin" and held_back(result, "place_of_birth", settings)
    assert "Oraș Huedi" in {c.value for c in result.candidates["place_of_birth"]}  # as read


def test_several_places_that_fit_are_all_offered(settings):
    # "Com. Car" in Dolj: Cârna, Carpen, Cârcea: no way to tell which, so none is the value
    person = replace(ALBA, birth_county_code="DJ", birth_locality="Com.Car")
    result = read(settings, card_text(person, mrz=[]))
    assert result.fields["place_of_birth"].value == "Com. Car"
    offered = {c.value for c in result.candidates["place_of_birth"]}
    assert {"Com. Cârna", "Com. Carpen", "Com. Cârcea"} <= offered


def test_the_place_of_an_issuing_office_is_checked_against_the_register(settings):
    result = read(settings, card_text(ALBA, issued_by="SPCLEP Alba Iulla", mrz=[]))
    assert result.fields["id_issued_by"].value == "SPCLEP Alba Iulia"
    assert held_back(result, "id_issued_by", settings)
    assert filled(read(settings, card_text(ALBA, mrz=[])), settings)["id_issued_by"] == (
        "SPCLEP Alba Iulia"
    )


def test_a_date_with_a_comma_does_not_end_up_in_the_issuing_office(settings):
    result = read(settings, card_text(ALBA, issued_by="SPCLEP Alba Iulia 20,02.23-", mrz=[]))
    assert filled(result, settings)["id_issued_by"] == "SPCLEP Alba Iulia"


# --------------------------------------------------------------------------- never fill junk


@pytest.mark.parametrize(
    ("name", "junk"),
    [
        ("place_of_birth", "Yeccceccecec<e<<<<<<<*"),  # a piece of the zone
        ("place_of_birth", "naja; a N"),
        ("id_issued_by", "Malabilitate/Validite/Validity"),  # the neighbouring label
        ("id_issued_by", "SPCLEP ~~ {}"),
    ],
)
def test_text_that_is_not_what_the_field_holds_is_flagged(name, junk):
    assert suspicious(FIELDS[name], junk)
    reviewed = review(ExtractedField(name=name, value=junk, confidence=0.92, source="label"))[0]
    assert reviewed.confidence < 0.5 and reviewed.issues


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("place_of_birth", "Jud.AB Mun.Alba Iulia"),
        ("place_of_birth", "Mun. București Sector 2"),
        ("id_issued_by", "SPCLEP Sector 4"),
        ("id_issued_by", "I.N.E.P."),
        ("full_address", "Mun. București Sec. 4, Str. Rudariilor nr. 14, bl. 3/B, ap. 7"),
        ("last_name", "Pop"),
    ],
)
def test_text_a_card_really_prints_is_not_flagged(name, value):
    assert suspicious(FIELDS[name], value) is None


def test_garbage_in_place_of_birth_is_not_filled(settings):
    text = card_text(HUEDIN, birth="Yeccceccecec<e<<<<<<<*", mrz=[])
    assert held_back(read(settings, text), "place_of_birth", settings)


def test_a_place_without_a_readable_locality_is_not_filled(settings):
    result = read(settings, card_text(ALBA, birth="Jud.AB fă"))
    assert held_back(result, "place_of_birth", settings)
    assert result.fields["birth_county"].value == "Alba"  # the county itself was readable


def test_the_generic_parser_does_not_take_a_street_for_the_locality(settings):
    result = read(settings, card_text(HUEDIN, domicile="Jud.?? ??\nStr.Horea nr.25 ap.2"))
    assert "city" not in result.fields or result.fields["city"].value != "Str.Horea nr.25 ap.2"


# --------------------------------------------------------------------------- cut off places


@pytest.mark.parametrize(
    ("birth", "problem"),
    [
        ("Jud.SB Mun.Sib", "Pare trunchiat"),
        ("Jud.CJ Mun.Cluj", "Pare trunchiat"),
        ("Jud.AB Mun.Set", "ați vrut să scrieți Sebeș"),  # glare took the end, a letter was misread
        ("Mun.București", "sectorul municipiului București"),
    ],
)
def test_a_place_cut_off_or_unknown_is_shown_not_filled(settings, birth, problem):
    result = read(settings, card_text(ALBA, birth=birth))
    assert held_back(result, "place_of_birth", settings)
    assert problem in result.fields["place_of_birth"].issues[-1]


def test_a_known_municipality_is_filled(settings):
    assert (
        filled(read(settings, card_text(ALBA, birth="Jud.SB Mun.Sibiu")), settings)[
            "place_of_birth"
        ]
        == "Mun. Sibiu"
    )


# --------------------------------------------------------------------------- the sample cards


def test_sample_cards_can_print_any_layout_and_county():
    assert Person(birth_county_code="TM").cnp[7:9] == "35"
    assert BUCHAREST.cnp[7:9] == "40"
    assert BUCHAREST.birth_place_line == "Mun.București Sec.2"
    assert Person().birth_place_line == "Jud.SB Mun.Sibiu"
    assert Person().domicile_card_lines[0] == "Jud.CJ Mun.Cluj-Napoca"
    lines = make_td2("X", "Y", "AX123456", "ROU", "871114", "M", "321114")
    assert len(lines[0]) == len(lines[1]) == 36


# --------------------------------------------------------------------------- end to end (OCR)


@requires_tesseract
@pytest.mark.parametrize(
    ("person", "expected"),
    [
        (
            ALBA,  # "Iulia" is read "lulia": the locality used to be cut to "Mun. Alba"
            EXPECTED_ALBA,
        ),
        (
            BUCHAREST,
            {
                "last_name": "Georgescu",
                "first_name": "Mihaela-Elena",
                "cnp": BUCHAREST.cnp,
                "place_of_birth": "Mun. București Sector 2",
                "birth_county": "București",
                "city": "Mun. București",
                "region": "Sector 4",
                "street": "Rudariilor",
                "id_issued_by": "SPCLEP Sector 4",
            },
        ),
        (
            HUEDIN,  # a short name, a town, a county code OCR cannot read, a Luhn-like CNP
            {
                "last_name": "Pop",
                "first_name": "Liviu",
                "cnp": HUEDIN.cnp,
                "date_of_birth": "09.06.1975",
                "place_of_birth": "Oraș Huedin",
                "birth_county": "Cluj",
                "city": "Oraș Huedin",
                "region": "Cluj",
            },
        ),
    ],
    ids=["alba-iulia", "bucharest", "town"],
)
def test_card_images_are_read_end_to_end(settings, person, expected):
    from docfill.pipeline import DocFill

    analysis = DocFill(settings).analyze_bytes(ro_id_card_jpeg(person), "ci.jpg")
    assert analysis.doc_type.doc_type == "id_card"
    values = analysis.extraction.values(settings.min_confidence)
    assert {k: values.get(k) for k in expected} == expected
