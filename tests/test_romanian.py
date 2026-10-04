"""Romanian documents: CNP, MRZ, addresses, identity cards and the ONRC forms.

Only fictitious people are used (see docfill.samples)."""

from datetime import date

import pytest

from docfill.config import Settings
from docfill.mrz import check_digit, cnp_from_mrz, make_td2, parse_mrz, read_mrz
from docfill.ro import (
    check_cnp,
    cnp_control_digit,
    compose_street_line,
    county_code,
    county_name,
    doubtful_place,
    normalize_date,
    office_doubt,
    parse_ro_address,
    place_doubt,
    repair_county_codes,
    restore_diacritics,
    spell_locality,
    split_room,
)
from docfill.samples import Person, ro_id_card_jpeg
from docfill.validation import validate_values
from tests.conftest import requires_tesseract

VALID_CNP = "187111432123" + cnp_control_digit("187111432123")


def test_cnp_validation_and_decoding():
    info = check_cnp(VALID_CNP)
    assert info.valid and info.sex == "M" and info.birth_date == date(1987, 11, 14)
    assert info.county == "Sibiu"
    broken = VALID_CNP[:-1] + str((int(VALID_CNP[-1]) + 1) % 10)
    assert not check_cnp(broken).valid
    assert not check_cnp("123").valid
    assert "birth date" in check_cnp("1921332081230").reason or not check_cnp("1921332081230").valid


def test_mrz_check_digits_and_ocr_repair():
    assert check_digit("L898902C3") == "6"  # ICAO 9303 specimen
    lines = make_td2("POPESCU", "ION ANDREI", "AX123456", "ROU", "871114", "M", "321114", "1321230")
    damaged = lines[1].replace("0", "O", 2).replace("123", "I23")
    mrz = read_mrz(f"noise\n{lines[0]}\n{damaged}\n")
    assert mrz.fully_valid
    assert (mrz.surname, mrz.given_names) == ("POPESCU", "ION ANDREI")
    assert mrz.document_number == "AX123456"
    assert mrz.birth_date == date(1987, 11, 14) and mrz.expiry_date == date(2032, 11, 14)


def test_mrz_second_line_is_read_when_the_names_line_is_split():
    # On a tilted photograph OCR splits the first line in two; the second line, which carries
    # the check digits, is still intact (spaces inside it are noise).
    first, second = make_td2(
        "TOMA", "IOANA IULIA", "AX334455", "ROU", "921205", "F", "331205", "2012668"
    )
    noisy = second[:16] + " " + second[16:28] + " " + second[28:]
    mrz = read_mrz(f"IDROUTOMA<<IOAN\n{noisy}\nAIULIA<<<<<<<<<<<<<<<<<")
    assert mrz is not None and mrz.fully_valid
    assert (mrz.document_number, mrz.birth_date, mrz.expiry_date) == (
        "AX334455",
        date(1992, 12, 5),
        date(2033, 12, 5),
    )
    assert mrz.sex == "F" and mrz.issuing_state == "ROU" and mrz.surname == ""


def test_mrz_second_line_with_a_lost_or_an_extra_character():
    _, second = make_td2("POP", "LIVIU", "CJ290841", "ROU", "750609", "M", "290609", "1120521")
    # an extra character (37 long) and a lost "<" filler (35 long)
    for damaged in (second[:1] + "3" + second[1:], second[:8] + second[9:]):
        mrz = read_mrz(f"noise\n{damaged}\n")
        assert mrz is not None and mrz.fully_valid, damaged
        assert mrz.document_number == "CJ290841" and mrz.birth_date == date(1975, 6, 9)


def test_mrz_is_not_invented_from_stray_lines():
    text = "CNP 1871114321239\nSERIA AX NR 123456\n12345678901234567890123456789012345\n"
    assert read_mrz(text) is None


@pytest.mark.parametrize("misread", ["LF", "1F", "IF"])
def test_mrz_series_letters_read_as_look_alikes(misread):
    # Ilfov: the series IF starts with a capital I that OCR reads as l or 1; the check digit
    # of the document number decides which reading is the right one.
    first, second = make_td2(
        "POPESCU", "ION", "IF123456", "ROU", "871114", "M", "321114", "1321230"
    )
    mrz = read_mrz(f"{first}\n{misread}{second[2:]}\n")
    assert mrz.fully_valid and mrz.document_number == "IF123456"


def test_cnp_is_rebuilt_from_the_machine_readable_zone():
    person = Person()
    first, second = person.mrz()
    mrz = read_mrz(f"{first}\n{second}\n")
    assert cnp_from_mrz(mrz) == person.cnp
    # electronic cards keep the 13 digits in the optional data
    td1 = [
        "IDROU" + "AX123456<0" + person.cnp + "<<",
        "8711142M3211142ROU<<<<<<<<<<" + "0",
        "POPESCU<<ION<ANDREI<<<<<<<<<<<<<",
    ]
    electronic = parse_mrz([line[:30].ljust(30, "<") for line in td1])
    assert electronic is not None and cnp_from_mrz(electronic) == person.cnp
    # a damaged optional field gives no CNP, never a wrong one
    broken = second[:28] + "9" + second[29:]
    assert cnp_from_mrz(read_mrz(f"{first}\n{broken}\n")) is None


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        (
            "Jud.AB Mun.Alba Iulia, Str.Mihai Viteazul nr.12 bl.A2 sc.1 et.3 ap.10",
            {
                "county": "Alba",
                "city": "Mun. Alba Iulia",
                "street": "Mihai Viteazul",
                "street_number": "12",
                "building": "A2",
                "entrance": "1",
                "floor": "3",
                "apartment": "10",
                "country": "România",
            },
        ),
        (
            "Mun.București Sec.3 Bd.Unirii nr.1 bl.A ap.5",
            {
                "sector": "Sector 3",
                "city": "Mun. București",
                "street": "Bd. Unirii",
                "street_number": "1",
                "building": "A",
                "apartment": "5",
                "country": "România",
            },
        ),
        (
            "Jud.SB Com.Exemplu Sat Deal nr.7A",
            {
                "county": "Sibiu",
                "city": "Com. Exemplu, Sat Deal",
                "street_number": "7A",
                "country": "România",
            },
        ),
        (
            "Jud.CJ Or.Huedin, Str.Horea nr.25 ap.2",  # towns are written "Or." on cards
            {
                "county": "Cluj",
                "city": "Oraș Huedin",
                "street": "Horea",
                "street_number": "25",
                "apartment": "2",
                "country": "România",
            },
        ),
        (
            "Jud.C} Mun.Dej",  # the hook of the J of the county code, read as a brace
            {"county": "Cluj", "city": "Mun. Dej", "country": "România"},
        ),
        ("Jud.!S Mun.Iasi", {"county": "Iași", "city": "Mun. Iași", "country": "România"}),
        ("Numele de familie al tatălui", {}),
    ],
)
def test_parse_ro_address(address, expected):
    assert parse_ro_address(address) == expected


@pytest.mark.parametrize(
    ("text", "county"),
    [("C}", "Cluj"), ("c)", "Cluj"), ("lS", "Iași"), ("!F", "Ilfov"), ("CJ", "Cluj"), ("XX", None)],
)
def test_county_codes_misread_by_ocr(text, county):
    assert county_name(text) == county
    assert repair_county_codes(f"Jud.{text} Mun.X") == (
        f"Jud.{ {'Cluj': 'CJ', 'Iași': 'IS', 'Ilfov': 'IF'}[county] } Mun.X"
        if county
        else "Jud.XX Mun.X"
    )


@pytest.mark.parametrize(
    ("place", "birth", "problem"),
    [
        ("Mun. Sibiu", False, None),
        ("Mun. Cluj-Napoca", False, None),
        ("Oraș Huedin", False, None),  # a town that is not on the list is never doubted
        ("Com. Hărman, Sat Podu Oltului", False, None),
        ("Mun. București Sector 2", True, None),
        ("Mun. București", False, None),  # a domicile: its sector is in the county field
        ("Mun. Sib", False, "cut off"),
        ("Mun. Cluj", False, "cut off"),
        ("Mun. Alba", False, "cut off"),
        ("Mun. Set", False, "Not a known municipality"),
        ("Mun. București", True, "sector of Bucharest"),
    ],
)
def test_places_that_cannot_be_what_a_card_prints(place, birth, problem):
    found = doubtful_place(place, birth=birth)
    assert (problem in found) if problem else found is None


@pytest.mark.parametrize(
    ("text", "code"),
    [("CJ", "CJ"), ("Cluj", "CJ"), ("jud. cluj", "CJ"), ("București", "B"), ("Sector 2", "B")],
)
def test_county_code(text, code):
    assert county_code(text) == code
    assert county_code("Atlantida") is None and county_code(None) is None


@pytest.mark.parametrize(
    ("place", "county", "birth", "problem", "suggestions"),
    [
        # a status changes over the years: a town that is a municipality now, and the reverse
        ("Oraș Dej", "CJ", False, None, ()),
        ("Mun. Huedin", "CJ", False, None, ()),
        # the county of the card is the one the name is looked up in
        ("Mun. Cluj-Napoca", "AB", False, "județul Alba (there is one in Cluj)", ()),
        ("Com. Hărman", "BV", False, None, ()),
        ("Com. Hărman", "CJ", False, "județul Cluj (there is one in Brașov)", ()),
        ("Com. Hărman, Sat Podu Oltului", "BV", False, None, ()),
        # a name cut off by glare: the municipalities that begin so, the kind printed first
        ("Mun. Plo", "PH", False, "cut off", ("Mun. Ploiești",)),
        ("Mun. Ca", "SV", False, "cut off", ("Mun. Câmpulung Moldovenesc",)),
        ("Oraș Huedi", "CJ", False, "cut off", ("Oraș Huedin",)),
        ("Mun. Me", "SB", False, "cut off", ("Mun. Mediaș",)),
        ("Com. Car", "DJ", False, "cut off", ("Com. Cârna", "Com. Carpen", "Com. Cârcea")),
        # one letter misread
        ("Oraș Hucdin", "CJ", False, "did you mean Huedin", ("Oraș Huedin",)),
        ("Com. Harmann", "BV", False, "did you mean Hărman", ("Com. Hărman",)),
        ("Com. Hărman, Sat Podu Oltuli", "BV", False, "did you mean Podu Oltului", None),
        # a name the register does not have, that looks like none of its names: left alone
        ("Com. Sărbătoreni", "BV", False, None, ()),
        ("Sat Valea Lungă", None, False, None, ()),
        ("Cluj-Napoca", "CJ", False, None, ()),  # without a prefix it is no card's locality
    ],
)
def test_places_are_checked_in_their_county(place, county, birth, problem, suggestions):
    doubt = place_doubt(place, birth=birth, county=county)
    if problem is None:
        assert doubt is None
        return
    assert doubt is not None and problem in doubt.problem
    if suggestions is not None:
        assert doubt.suggestions == suggestions


def test_a_suggestion_replaces_only_the_doubted_part_of_a_place():
    doubt = place_doubt("Com. Hărman, Sat Podu Oltuli", county="BV")
    assert doubt.suggestions == ("Com. Hărman, Sat Podu Oltului",)


@pytest.mark.parametrize(
    ("office", "problem", "suggestions"),
    [
        ("SPCLEP Cluj-Napoca", None, ()),
        ("SPCLEP Alba Iulia", None, ()),
        ("SPCLEP Sector 4", None, ()),  # not a place of the register
        ("SPCLEP Săcălaz", None, ()),
        ("SPCLEP Drobeta-Tumu Severin", "did you mean Drobeta-Turnu Severin", None),
        ("SPCLEP Cluj", "cut off", ("SPCLEP Cluj-Napoca",)),
        ("SPCLEP Xyzzyq", None, ()),
        ("I.N.E.P.", None, ()),
    ],
)
def test_the_place_in_an_issuing_office_is_checked(office, problem, suggestions):
    doubt = office_doubt(office)
    if problem is None:
        assert doubt is None
        return
    assert doubt is not None and problem in doubt.problem
    if suggestions is not None:
        assert doubt.suggestions == suggestions


@pytest.mark.parametrize(
    ("kind", "name", "county", "spelled"),
    [
        ("Com.", "Harman", "BV", "Hărman"),
        ("Com.", "HARMAN", "BV", "HĂRMAN"),
        ("Mun.", "Cluj Napoca", "CJ", "Cluj-Napoca"),
        ("Sat", "Podu Oltului", "BV", "Podu Oltului"),
        ("Oraș", "Stefanesti", "AG", "Ștefănești"),
        ("Com.", "Sacalaz", None, "Săcălaz"),  # one such commune in the country
        ("Com.", "Stefanesti", None, "Ștefănești"),  # in five counties, spelled the same
        ("Sat", "Silea", None, "Silea"),  # Silea or Șilea, by county: left as read
        ("Sat", "Silea", "AB", "Șilea"),
        ("Com.", "Zzzz", "BV", "Zzzz"),
        ("Com.", "Hărman", "CJ", "Hărman"),
    ],
)
def test_localities_are_spelled_as_the_register_does(kind, name, county, spelled):
    assert spell_locality(kind, name, county) == spelled


def test_an_address_gets_its_diacritics_from_the_register_and_the_lists():
    parts = parse_ro_address("Jud.BV Com.Harman Sat Podu Oltului Str.Libertatii nr.5")
    assert parts["city"] == "Com. Hărman, Sat Podu Oltului" and parts["street"] == "Libertății"
    parts = parse_ro_address("Jud.IS Mun.Iasi Str.Stefan cel Mare nr.8")
    assert parts["city"] == "Mun. Iași" and parts["street"] == "Ștefan cel Mare"
    # a Bucharest address has no county: its sector says where it is
    assert parse_ro_address("Mun.Bucuresti Sec.4 Str.Rudariilor nr.14")["city"] == "Mun. București"


def test_restore_diacritics_knows_names_and_street_words():
    assert restore_diacritics("Str. Stefan cel Mare") == "Str. Ștefan cel Mare"
    assert restore_diacritics("Aleea Padurii") == "Aleea Pădurii"
    assert restore_diacritics("Bd. Libertatii") == "Bd. Libertății"
    assert restore_diacritics("Str. Mihai Eminescu") == "Str. Mihai Eminescu"  # nothing to add
    assert restore_diacritics("Str. Kossuth Lajos") == "Str. Kossuth Lajos"  # not ours to accent
    assert restore_diacritics("Com. Sacalaz") == "Com. Săcălaz"  # a commune of the register


def test_address_helpers():
    assert (
        compose_street_line({"street": "Florilor", "street_number": "5", "apartment": "2"})
        == "Str. Florilor nr. 5, ap. 2"
    )
    assert split_room("Mihai Eminescu camera 2") == ("Mihai Eminescu", "camera 2")
    assert county_name("SB") == "Sibiu" and county_name("jud. iasi") == "Iași"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("14.11.1987", "14.11.1987"),
        ("14/11/87", "14.11.1987"),
        ("1987-11-14", "14.11.1987"),
        ("14 noiembrie 1987", "14.11.1987"),
        ("31.02.2020", None),
    ],
)
def test_dates(text, expected):
    assert normalize_date(text) == expected


def test_restore_diacritics_only_for_known_places():
    assert restore_diacritics("Mun. Fagaras") == "Mun. Făgăraș"
    assert restore_diacritics("SPCLEP TARGU MURES") == "SPCLEP TÂRGU MUREȘ"
    assert restore_diacritics("Focşani") == "Focșani"  # cedilla ş -> comma-below ș
    assert restore_diacritics("Strada Necunoscuta") == "Strada Necunoscuta"


def test_validate_values_cross_checks():
    issues = validate_values(
        {
            "cnp": VALID_CNP,
            "date_of_birth": "01.01.1990",
            "sex": "F",
            "billing_iban": "RO00 BANK 0000",
            "id_expiry_date": "01.01.2000",
        }
    )
    assert issues["date_of_birth"] == ["Differs from the date of birth in the CNP"]
    assert issues["sex"] == ["Differs from the sex encoded in the CNP"]
    assert "IBAN" in issues["billing_iban"][0]
    assert issues["id_expiry_date"] == ["The identity card has expired"]


@requires_tesseract
def test_identity_card_end_to_end(settings):
    from docfill.pipeline import DocFill

    person = Person()
    analysis = DocFill(settings).analyze_bytes(ro_id_card_jpeg(person), "ci.jpg")
    assert analysis.doc_type.doc_type == "id_card"
    values = analysis.extraction.values(settings.min_confidence)
    expected = {
        "last_name": "Popescu",
        "first_name": "Ion-Andrei",
        "cnp": person.cnp,
        "sex": "M",
        "date_of_birth": "14.11.1987",
        "citizenship": "Română",
        "id_type": "CI",
        "id_series": "AX",
        "id_number": "123456",
        "id_issued_by": "SPCLEP Cluj-Napoca",
        "id_issue_date": "22.06.2022",
        "id_expiry_date": "14.11.2032",
        "place_of_birth": "Mun. Sibiu",
        "birth_county": "Sibiu",
        "birth_country": "România",
        "city": "Mun. Cluj-Napoca",
        "region": "Cluj",
        "country": "România",
        "street": "Florilor",
        "street_number": "5",
        "building": "A2",
        "entrance": "1",
        "floor": "3",
        "apartment": "10",
    }
    assert {k: values.get(k) for k in expected} == expected
    # the printed names are confirmed by the machine readable zone
    assert analysis.extraction.fields["last_name"].confidence >= 0.97


# --------------------------------------------------------------------------- ONRC forms

ONRC_VALUES = {
    "last_name": "Ștefănescu",
    "first_name": "Ana-Maria",
    "cnp": "295010112345" + cnp_control_digit("295010112345"),
    "city": "Mun. Cluj-Napoca",
    "street": "Florilor",
    "street_number": "5",
    "region": "Cluj",
    "country": "România",
    "citizenship": "Română",
    "place_of_birth": "Mun. Sibiu",
    "birth_county": "Sibiu",
    "birth_country": "România",
    "date_of_birth": "01.01.1995",
    "id_type": "CI",
    "id_series": "CJ",
    "id_number": "654321",
    "id_issued_by": "SPCLEP Cluj-Napoca",
    "id_issue_date": "01.02.2020",
    "id_expiry_date": "01.01.2030",
    "capacity": "asociat unic și administrator",
    "company_name": "Exemplu Soft S.R.L.",
    "company_city": "Mun. Cluj-Napoca",
    "company_street": "Memorandumului",
    "company_street_number": "10",
    "company_county": "Cluj",
    "communication_method": "mijloace electronice",
    "caen_activities": "6201 Activități de realizare a soft-ului la comandă\n"
    "6202 Activități de consultanță în tehnologia informației",
}


@pytest.fixture
def onrc(settings):
    from docfill.app import build_app
    from docfill.templates import bundled_specs_dir, load_directory

    app = build_app(settings, use_ner=False)
    with app.sessions() as session:
        repo = app.repository(session)
        for spec in load_directory(bundled_specs_dir()):
            repo.save(spec)
    return app


def _template(app, name):
    with app.sessions() as session:
        return app.repository(session).get(name)


def test_onrc_forms_are_filled_with_correct_fields(onrc):
    from docfill.export.pdf_form import read_form_values

    anexa2a = _template(onrc, "onrc-anexa-2a")
    result = onrc.docfill.fill(anexa2a, None, ONRC_VALUES, allow_missing=True)
    values = read_form_values(result.pdf)
    assert values["nume"] == "ȘTEFĂNESCU" and values["prenume"] == "ANA-MARIA"
    assert values["cnp_nif"] == ONRC_VALUES["cnp"]
    assert values["localitate nastere"] == "Mun. Sibiu" and values["sector"] == "Sibiu"
    assert values["pentru firma"] == "EXEMPLU SOFT S.R.L."
    assert values["ORC"] == "Cluj"  # derived from the company county
    assert values["CheckBox1"] == "x" and values["CheckBox7_bg"] == "x"  # defaults: înmatriculare
    assert values["CheckBox90_2"] == "/v3"  # option button "mijloace electronice"
    assert values["pg. 4 text 27"] == "ȘTEFĂNESCU ANA-MARIA"  # composed field

    anexa4 = _template(onrc, "onrc-anexa-4")
    values4 = read_form_values(onrc.docfill.fill(anexa4, None, ONRC_VALUES, True).pdf)
    assert values4["SubNume"] == "ȘTEFĂNESCU" and values4["InmFirma"] == "EXEMPLU SOFT S.R.L."
    assert values4["clasa_caen.1.0"] == "6201"  # 3.2: at third parties
    assert values4["clasa_caen_desc.1.1"] == "Activități de consultanță în tehnologia informației"


def test_filled_anexa_2a_is_recognised_and_fills_anexa_4(onrc):
    from docfill.export.pdf_form import read_form_values

    anexa2a, anexa4 = _template(onrc, "onrc-anexa-2a"), _template(onrc, "onrc-anexa-4")
    filled = onrc.docfill.fill(anexa2a, None, ONRC_VALUES, allow_missing=True).pdf
    analysis = onrc.docfill.analyze_bytes(filled, "anexa2a.pdf")
    assert analysis.doc_type.doc_type == "onrc_anexa_2a"
    assert analysis.doc_type.method == "form fields"
    result = onrc.docfill.fill(anexa4, analysis.extraction, allow_missing=True)
    values4 = read_form_values(result.pdf)
    assert values4["SubNume"] == "ȘTEFĂNESCU"
    assert values4["SubCNP"] == ONRC_VALUES["cnp"]
    assert values4["InmStrada"] == "Memorandumului"
    assert values4["SubNData"] == "01.01.1995"


def test_blank_form_removes_every_value(onrc):
    from docfill.export.pdf_form import blank_form, read_form_values

    anexa4 = _template(onrc, "onrc-anexa-4")
    filled = onrc.docfill.fill(anexa4, None, ONRC_VALUES, allow_missing=True).pdf
    blank = blank_form(filled)
    assert read_form_values(blank) == {}
    assert "ȘTEFĂNESCU".encode("utf-16-be") not in blank and b"654321" not in blank


def test_inspect_form_finds_labels(onrc):
    from docfill.export.pdf_form import inspect_form

    fields = {f.name: f for f in inspect_form(_template(onrc, "onrc-anexa-4").pdf_data)}
    assert fields["SubCNP"].label.startswith("CNP")
    assert fields["SubCNP"].kind == "text" and fields["SubCNP"].page == 1


def test_document_type_classifier():
    from docfill.doctypes import DocTypeClassifier

    classifier = DocTypeClassifier()
    assert (
        classifier.predict(
            "CERTIFICAT DE NASTERE Numele de familie Prenumele PARINTII "
            "Numele de familie al tatalui"
        ).doc_type
        == "birth_certificate"
    )
    assert (
        classifier.predict("FACTURA fiscala Furnizor Cumparator Total de plata TVA").doc_type
        == "other"
    )


def test_settings_default_languages():
    assert Settings().ocr_languages == "auto"
