"""The reference documents: the official forms docfill fills, kept exactly as they were given.

Each test fills a reference document from known input (fictitious people) and checks the
expected document: every value in its box, the right boxes ticked, the blocks that must stay
empty left empty. The forms themselves are guarded byte for byte, so they stay the reference.
"""

import hashlib
import re
from datetime import date

import pytest
from pypdf import PdfReader

from docfill.errors import MissingFieldsError
from docfill.export.pdf_form import fill_pdf_form, read_form_values
from docfill.pipeline import DocFill, form_values
from docfill.readers.doc import doc_converter
from docfill.samples import Person, sworn_statement_text
from docfill.templates import bundled_specs_dir, load_spec
from docfill.templates.models import StandardDocument

# The documents given as the reference (official ONRC / ANAF forms and the ONRC model of the
# articles of incorporation). They must never change: a new official version is a new file.
# The administrator's sworn statement has no official form: its reference is the wording of the
# statements filers submit (see test_declaratie_administrator_is_written_like_the_reference).
REFERENCES = {
    "onrc-anexa-2a.pdf": "a468c682e442b5ed886289b0ba5235407d7ae4fa18443ae66dafc430aa99c243",
    "onrc-anexa-4.pdf": "3be459d2f5453c2ad0120410c11819278de1f51c972badad07a960188472f029",
    "anexa-1-inregistrare-fiscala.pdf": (
        "5c5af5528a71d74c03df52e163696b2f86b72cc3bcaa8fb0e295c24732b89a31"
    ),
    "onrc-declaratie-beneficiari-reali.pdf": (
        "d9bb0d338fa387782e7c772e36dbb93c44a20bf4bf698c9f3fabec768af62749"
    ),
    "model-act-constitutiv-sa-sistem-unitar.doc": (
        "971dde85350881fb48eed3585ed97cf393481095b437429e872efc6c5a2b683b"
    ),
}
TODAY_DIGITS = date.today().strftime("%d%m%Y")

PERSON_2 = Person(
    last_name="IONESCU",
    first_name="MARIA",
    sex="F",
    birth=date(1994, 3, 8),
    birth_county_code="SB",
    series="TZ",
    number="604213",
    serial="457",
)

# The applicant (person 1), a second shareholder (person 2), a third board member (person 3).
VALUES = {
    "orc_office": "Cluj",
    "capacity": "Administrator",
    "last_name": "Popescu",
    "first_name": "Ion-Andrei",
    "cnp": Person().cnp,
    "id_type": "CI",
    "id_series": "AX",
    "id_number": "123456",
    "id_issued_by": "SPCLEP Cluj-Napoca",
    "id_issue_date": "22.06.2022",
    "id_expiry_date": "14.11.2032",
    "date_of_birth": "14.11.1987",
    "place_of_birth": "Mun. Sibiu",
    "birth_county": "Sibiu",
    "citizenship": "Română",
    "country": "România",
    "city": "Mun. Cluj-Napoca",
    "street": "Florilor",
    "street_number": "5",
    "building": "A2",
    "entrance": "1",
    "floor": "3",
    "apartment": "10",
    "region": "Cluj",
    "p2_last_name": "Ionescu",
    "p2_first_name": "Maria",
    "p2_cnp": PERSON_2.cnp,
    "p2_id_type": "CI",
    "p2_id_series": "TZ",
    "p2_id_number": "604213",
    "p2_id_issued_by": "SPCLEP Timișoara",
    "p2_id_issue_date": "01.03.2021",
    "p2_date_of_birth": "08.03.1994",
    "p2_place_of_birth": "Mun. Mediaș",
    "p2_birth_county": "Sibiu",
    "p2_citizenship": "Română",
    "p2_country": "România",
    "p2_city": "Com. Săcălaz",
    "p2_street": "Principală",
    "p2_street_number": "12",
    "p2_region": "Timiș",
    "p3_last_name": "Dumitru",
    "p3_first_name": "Andrei",
    "company_name": "Exemplu Invest S.A.",
    "company_city": "Mun. Cluj-Napoca",
    "company_street": "Memorandumului",
    "company_street_number": "28",
    "company_county": "Cluj",
    "caen_activities": "6201 Activități de realizare a software-ului la comandă\n"
    "6202 Activități de consultanță în tehnologia informației",
    "request_registration": "x",
    "share_capital": "90.000 lei",
    "share_count": "900",
    "shares": "450",
    "p2_shares": "450",
    "board_role": "președinte",
    "p2_board_role": "membru",
    "p3_board_role": "membru",
    "general_director": "x",
    "beneficial_owner": "art. 4 alin. (2) lit. a) pct. 1",
    "control_description": "deține direct 50% din acțiuni",
    "p2_beneficial_owner": "lit. a) pct. 1",
    "main_activity_domain": "Activități de servicii în tehnologia informației",
    "control_body": "cenzori",
    "control_members": "1. VASILE ANA, expert contabil\n2. MARIN DAN, expert contabil",
    "micro_tax": "x",
    "micro_tax_start": "01.11.2026",
    "payroll_period": "lunară",
    "estimated_turnover": "250.000 lei",
    "vat_registration": "prin opțiune",
    "vat_period": "trimestrială",
    "contact_last_name": "Popescu",
    "contact_first_name": "Ion",
    "contact_email": "ion@example.ro",
    "contact_phone": "0722123456",
}


def document(name: str) -> StandardDocument:
    spec = next(
        spec
        for spec in map(load_spec, sorted(bundled_specs_dir().glob("*.yaml")))
        if spec.name == name
    )
    return StandardDocument(
        name=spec.name,
        title=spec.title,
        kind=spec.kind,
        body=spec.body,
        pdf_data=spec.pdf_data,
        field_map=spec.field_map,
        optional_fields=spec.optional_fields,
        options=spec.options(),
        checksum=spec.checksum(),
        version=1,
    )


@pytest.fixture(scope="module")
def filled(tmp_path_factory):
    """Every reference document filled from VALUES: name -> (PDF, missing fields)."""
    from docfill.config import Settings

    settings = Settings(
        database_url=f"sqlite:///{tmp_path_factory.mktemp('db') / 'test.db'}", spacy_model=""
    )
    docfill = DocFill(settings)
    results = {}
    for name in (
        "onrc-anexa-2a",
        "onrc-anexa-4",
        "cerere-inregistrare-fiscala",
        "onrc-declaratie-beneficiari-reali",
        "act-constitutiv-sa",
    ):
        result = docfill.fill(document(name), None, VALUES)
        results[name] = (result.pdf, result.missing)
    return results


def boxes(values: dict[str, str], *names: str) -> str:
    return "".join(values.get(name, " ") for name in names)


# --------------------------------------------------------------------------- kept as given


@pytest.mark.parametrize(("filename", "sha256"), sorted(REFERENCES.items()))
def test_reference_documents_are_kept_unchanged(filename, sha256):
    data = (bundled_specs_dir() / filename).read_bytes()
    assert hashlib.sha256(data).hexdigest() == sha256


def test_every_reference_form_is_a_standard_document():
    used = {load_spec(path).name for path in bundled_specs_dir().glob("*.yaml")}
    assert {
        "onrc-anexa-2a",
        "onrc-anexa-4",
        "cerere-inregistrare-fiscala",
        "onrc-declaratie-beneficiari-reali",
        "act-constitutiv-sa",
    } <= used


def test_all_reference_documents_are_filled_completely(filled):
    for name, (pdf, missing) in filled.items():
        assert missing == [], name
        assert pdf.startswith(b"%PDF")


# --------------------------------------------------------------------------- Anexa 2a / Anexa 4


def test_anexa_2a(filled):
    values = read_form_values(filled["onrc-anexa-2a"][0])
    assert values["ORC"] == "Cluj"
    assert values["CheckBox1"]  # înmatriculare
    assert (values["nume"], values["prenume"]) == ("POPESCU", "ION-ANDREI")
    assert values["cnp_nif"] == Person().cnp
    assert (values["localitatea"], values["strada"], values["numar"]) == (
        "Mun. Cluj-Napoca",
        "Florilor",
        "5",
    )
    assert (values["act_ident_seria"], values["act_ident_numar"]) == ("AX", "123456")
    assert values["pentru firma"] == "EXEMPLU INVEST S.A."
    assert values["sediu_strada"] == "Memorandumului"
    assert values["pg. 4 text 27"] == "POPESCU ION-ANDREI"


def test_anexa_4(filled):
    values = read_form_values(filled["onrc-anexa-4"][0])
    assert (values["SubNume"], values["SubCNP"]) == ("POPESCU", Person().cnp)
    assert values["InmFirma"] == "EXEMPLU INVEST S.A."
    assert (values["clasa_caen.0.0"], values["clasa_caen.0.1"]) == ("6201", "6202")
    assert values["clasa_caen_desc.0.1"] == "Activități de consultanță în tehnologia informației"


# --------------------------------------------------------------------------- Anexa 1 (fiscal)


def test_anexa_1_cerere_inregistrare_fiscala(filled):
    values = read_form_values(filled["cerere-inregistrare-fiscala"][0])
    assert values["BBox1"] == "x" and "BBox3" not in values  # persoană juridică
    assert values["BBox5_bg25012023"] == "x"  # 2. micro-enterprise tax
    assert boxes(values, *map(str, range(21, 29))) == "01112026"
    assert "BBox4" not in values  # no profit tax
    assert values["BBox6"] == "x" and values["BBo11"] == "x"  # payroll, monthly
    assert values["BBox16"] == "x"  # 4. VAT
    assert boxes(values, *map(str, range(61, 69))) == "  250000"  # right-aligned digits
    assert values["BBox24"] == "x" and "BBox21" not in values  # 4.3 by option
    assert values["BBox20"] == "x" and "BBox23" not in values  # quarterly
    assert (values["3"], values["4"]) == ("POPESCU ION-ANDREI", "Administrator")
    assert boxes(values, *map(str, range(5, 13))) == TODAY_DIGITS
    assert "1" not in values and "2" not in values  # number / date given by the office


# --------------------------------------------------------------------------- beneficial owners


def test_declaratie_beneficiari_reali(filled):
    values = read_form_values(filled["onrc-declaratie-beneficiari-reali"][0])
    assert values["Box1"] == "x" and "Box2" not in values  # înmatriculare
    assert (values["3_2"], values["3"], values["6"]) == ("POPESCU", "ION-ANDREI", Person().cnp)
    assert values["9"] == "EXEMPLU INVEST S.A."
    # beneficial owner 1 = person 1, control by lit. a) pct. 1
    assert (values["13"], values["20"], values["18"]) == (
        "POPESCU",
        Person().cnp,
        "Mun. Cluj-Napoca",
    )
    assert values["Box11"] == "x" and values["Box5"] == "x"
    assert values["24"] == "deține direct 50% din acțiuni"
    # beneficial owner 2 = person 2 (the option is matched from "lit. a) pct. 1")
    assert (values["33"], values["31"], values["32_2"]) == ("IONESCU", "MARIA", PERSON_2.cnp)
    assert values["Box9_06012023_0"] == "x" and values["Box8"] == "x"
    # person 3 is named but is not a beneficial owner: block 3 stays empty
    assert not {"46", "50", "Box10"} & set(values)
    assert not any(values.get(f"Box9_06012023_{i}") for i in range(6, 12))
    # filed by the legal representative (no proxy): derived
    assert values["07Box1111"] == "x" and "Box121" not in values
    assert (values["79"], values["74"]) == ("POPESCU ION-ANDREI", date.today().strftime("%d.%m.%Y"))


def test_email_and_phone_share_one_field_but_get_their_own_box():
    template = document("onrc-declaratie-beneficiari-reali")
    pdf_values = form_values(template, VALUES)
    assert (pdf_values["71#1"], pdf_values["71#2"]) == ("ion@example.ro", "0722123456")
    pdf = fill_pdf_form(template.pdf_data, pdf_values, font_path=None)  # Helvetica: plain text
    drawn = []
    for page in PdfReader(__import__("io").BytesIO(pdf)).pages:
        for annotation in page.get("/Annots") or []:
            annotation = annotation.get_object()
            parent = annotation.get("/Parent")
            if parent is not None and parent.get_object().get("/T") == "71":
                drawn.append(annotation["/AP"]["/N"].get_object().get_data())
    assert len(drawn) == 2
    assert b"ion@example.ro" in drawn[0] and b"0722123456" not in drawn[0]
    assert b"0722123456" in drawn[1] and b"ion@example.ro" not in drawn[1]


# --------------------------------------------------------------------------- act constitutiv SA


def act_text(pdf: bytes) -> str:
    return " ".join(
        " ".join(
            page.extract_text() for page in PdfReader(__import__("io").BytesIO(pdf)).pages
        ).split()
    )


def test_act_constitutiv_sa(filled):
    text = act_text(filled["act-constitutiv-sa"][0])
    assert "ACTUL CONSTITUTIV AL SOCIETĂŢII EXEMPLU INVEST S.A." in text
    # founders: the persons holding shares (person 3 holds none)
    assert (
        "D-nul/d-na POPESCU ION-ANDREI, cetăţenie română, născut(ă) la data de 14.11.1987" in text
    )
    assert "D-nul/d-na IONESCU MARIA" in text
    assert "D-nul/d-na DUMITRU ANDREI" not in text
    assert "Sediul social este în Mun. Cluj-Napoca, Str. Memorandumului nr. 28, jud. Cluj." in text
    assert "grupa CAEN 620" in text and "clasa CAEN 6201 - Activități de realizare" in text
    assert "clasa CAEN 6202 - Activități de consultanță" in text
    assert "capitalul social subscris şi vărsat al societăţii este de 90.000 lei" in text
    assert "900 acţiuni, cu o valoare nominală de 100 lei/acţiune" in text
    assert "deţine un număr de 450 acţiuni" in text and "în valoare totală de 45.000 lei" in text
    assert "reprezentând 50% din capitalul social" in text
    assert "Acţiunile sunt numerotate de la 1 la 900." in text
    assert "sunt nominative şi indivizibile" in text
    # a board of directors (derived from the board roles), not a sole administrator
    assert "este numit consiliul de administraţie, cu următoarea componenţă" in text
    assert "administrator unic al societăţii cu puteri depline" not in text
    assert "în calitate de președinte" in text and "în calitate de membru" in text
    assert "este numit director general: - d-nul/d-na POPESCU ION-ANDREI" in text
    assert "Controlul gestiunii societăţii este asigurat de către cenzorii: 1. VASILE ANA" in text
    assert "auditor financiar: " not in text
    assert "Durata de funcţionare a societăţii este nedeterminată." in text


def test_act_constitutiv_sa_sole_administrator_and_auditor(tmp_path):
    from docfill.config import Settings

    settings = Settings(database_url=f"sqlite:///{tmp_path / 't.db'}", spacy_model="")
    values = {
        **VALUES,
        "board_role": "administrator unic",
        "p2_board_role": "",
        "p3_board_role": "",
        "general_director": "",
        "control_body": "auditor financiar",
        "control_members": "EXPERT AUDIT S.R.L., CUI 12345678",
        "company_duration": "10",
    }
    result = DocFill(settings).fill(document("act-constitutiv-sa"), None, values)
    text = act_text(result.pdf)
    assert "administrată de către un administrator unic" in text
    assert "este numit administrator unic al societăţii cu puteri depline" in text
    assert "consiliul de administraţie, cu următoarea componenţă" not in text
    assert "director general:" not in text
    assert "numit auditor financiar: EXPERT AUDIT S.R.L." in text
    assert "pe o perioadă de 10 ani" in text


def test_act_constitutiv_copies_the_model_verbatim():
    """Every paragraph of the template without blanks is the model's own text."""
    if doc_converter() is None:
        pytest.skip("antiword or LibreOffice is needed to read the .doc model")
    from docfill.readers import read_bytes

    model_path = bundled_specs_dir() / "model-act-constitutiv-sa-sistem-unitar.doc"
    model = " ".join(read_bytes(model_path.read_bytes(), model_path.name).text.split())
    body = document("act-constitutiv-sa").body or ""
    verbatim = [
        line
        for line in body.splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "---", "[[")) and "{{" not in line
    ]
    assert len(verbatim) > 80
    assert [line for line in verbatim if " ".join(line.split()) not in model] == []
    # every article of the model is in the template
    articles = set(re.findall(r"Art\.\s?(\d+\.\d+)", model))
    assert articles <= set(re.findall(r"Art\.\s?(\d+\.\d+)", body))


# --------------------------------------------------------------------------- sworn statement


def statement_values(person: Person, prefix: str = "") -> dict[str, str]:
    """What docfill knows about ``person`` after reading their identity card."""
    from docfill.extraction.clauses import clause_fields

    read = clause_fields("\n".join(sworn_statement_text(person)[1:2]))
    read.update(last_name=person.last_name, first_name=person.first_name)
    return {prefix + name: value for name, value in read.items() if name != "capacity"}


def test_declaratie_administrator_is_written_like_the_reference(tmp_path):
    """The statement docfill writes is, paragraph by paragraph, the one filers submit."""
    from docfill.config import Settings
    from docfill.templates.placeholders import render_body

    person = PERSON_2  # a woman: "născută", "identificată", "numită"
    values = {
        **statement_values(person),
        "last_name": person.last_name,
        "first_name": person.first_name,
        "company_name": "EXEMPLU INVEST S.A.",
        "today": "25.09.2026",
    }
    template = document("declaratie-administrator")
    settings = Settings(database_url=f"sqlite:///{tmp_path / 't.db'}", spacy_model="")
    [(person_number, result)] = DocFill(settings).fill_each(template, None, values)
    assert person_number == 1 and result.missing == []
    from docfill.computed import with_computed

    rendered = render_body(template.body or "", with_computed({**template.defaults, **values}))
    written = [line.lstrip("# ").strip() for line in rendered.splitlines() if line.strip()]
    assert written == sworn_statement_text(person, signed=date(2026, 9, 25))
    assert "născută în" in written[1] and "identificată prin CI" in written[1]


def test_one_statement_for_each_administrator(tmp_path):
    from docfill.config import Settings

    settings = Settings(database_url=f"sqlite:///{tmp_path / 't.db'}", spacy_model="")
    values = {
        **statement_values(Person()),
        **statement_values(PERSON_2, "p2_"),
        "p3_last_name": "Dumitru",
        "company_name": "EXEMPLU INVEST S.A.",
        "board_role": "președinte",
        "p2_board_role": "membru",
    }
    copies = DocFill(settings).fill_each(document("declaratie-administrator"), None, values)
    assert [person for person, _ in copies] == [1, 2]  # person 3 is not an administrator
    texts = [act_text(result.pdf) for _, result in copies]
    assert texts[0].startswith("DECLARAȚIE PE PROPRIE RĂSPUNDERE POPESCU ION-ANDREI, CNP")
    assert "IONESCU MARIA, CNP " + PERSON_2.cnp in texts[1]
    assert "în calitate de administrator numit al societății EXEMPLU INVEST S.A." in texts[1]
    # an administrator whose identity is incomplete: the missing fields are named for them
    values["p3_board_role"] = "membru"
    with pytest.raises(MissingFieldsError) as missing:
        DocFill(settings).fill_each(document("declaratie-administrator"), None, values)
    assert {"p3_cnp", "p3_id_number"} <= set(missing.value.missing)


def test_statement_preview_lists_what_each_administrator_misses():
    from docfill.wizard import build_preview

    values = {**statement_values(Person()), "company_name": "X S.A.", "today": "01.10.2026"}
    values.update({"board_role": "președinte", "p2_board_role": "membru", "p2_last_name": "Ion"})
    preview = build_preview(document("declaratie-administrator"), values)
    assert preview["persons"] == [1, 2]
    assert "p2_cnp" in preview["missing"] and "cnp" not in preview["missing"]
