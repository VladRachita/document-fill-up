"""Romanian documents: CNP, MRZ, addresses, identity cards and the ONRC forms.

Only fictitious people are used (see docfill.samples)."""

from datetime import date

import pytest

from docfill.config import Settings
from docfill.mrz import check_digit, make_td2, read_mrz
from docfill.ro import (
    check_cnp,
    cnp_control_digit,
    compose_street_line,
    county_name,
    normalize_date,
    parse_ro_address,
    restore_diacritics,
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
        ("Numele de familie al tatălui", {}),
    ],
)
def test_parse_ro_address(address, expected):
    assert parse_ro_address(address) == expected


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
    assert values4["clasa_caen.0.0"] == "6201"
    assert values4["clasa_caen_desc.0.1"] == "Activități de consultanță în tehnologia informației"


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
