"""Reading the fields of a Romanian identity card correctly: the CNP, the names, the places and the
issuing office, from the text OCR gives and from card images (fictitious people only).

The cases are the ways real scans go wrong: a CNP that happens to look like a payment card, a
capital I read as a lowercase l, a county code read as a brace, a line cut off by glare, a
machine readable zone damaged or split by a tilted photograph."""

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
    assert "Invalid CNP" in result.fields["cnp"].issues[0]


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
    assert "Differs from the CNP in the machine readable zone" in result.fields["cnp"].issues[0]


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
    assert any("Cut off on the scan" in issue for issue in name.issues)


def test_a_name_that_disagrees_with_the_zone_is_not_filled(settings):
    # "IOANA" read as "JOANA": two readings that disagree are a person's decision
    result = read(settings, card_text(ALBA, first_name="JOANA-IULIA"))
    assert held_back(result, "first_name", settings)
    assert "Differs from the machine readable zone" in result.fields["first_name"].issues[0]
    assert [c.confidence < 0.5 for c in result.candidates["first_name"]] == [True, True]


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
    reviewed = review(ExtractedField(name=name, value=junk, confidence=0.92, source="label"))
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
        ("Jud.SB Mun.Sib", "Looks cut off"),
        ("Jud.CJ Mun.Cluj", "Looks cut off"),
        ("Jud.AB Mun.Set", "Not a known municipality"),
        ("Mun.București", "sector of Bucharest"),
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
