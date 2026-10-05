"""The company documents of a registration: the act constitutiv (the company, its capital and
activities, every person with their roles), the proofs of the firm name and of the registered
office, and who signs the requests (the administrator) and who files them (a lawyer or a proxy).
Every person and company here is fictitious."""

import pytest
from fastapi.testclient import TestClient

from docfill.api import create_app
from docfill.app import build_app
from docfill.computed import address_line, with_computed
from docfill.doctypes import DocType, DocTypeClassifier
from docfill.export.pdf_form import read_form_values
from docfill.extraction import FieldExtractor
from docfill.extraction.articles import (
    caen_activities,
    company_address,
    company_name,
    extract_articles,
    extract_name_reservation,
    extract_premises,
)
from docfill.extraction.clauses import extract_clauses
from docfill.extraction.derive import complete_values, representation
from docfill.extraction.fields import CONTROL_OPTIONS, REPRESENTATIVE, REPRESENTATIVE_TYPES
from docfill.extraction.patterns import extract_identity
from docfill.models import SanitizedDocument
from docfill.pipeline import DocFill, form_values
from docfill.ro import parse_ro_address
from docfill.samples import Person, ro_id_card_jpeg, sworn_statement_docx, sworn_statement_text
from docfill.templates import TemplateRepository, bundled_specs_dir, load_directory
from docfill.validation import validate_values
from docfill.wizard import field_rows
from tests.conftest import make_docx, requires_tesseract
from tests.test_persons import MURESAN
from tests.test_reference_forms import PERSON_2, document

POPESCU = Person()
# The lawyer filing the request for the company: named nowhere in the act constitutiv.
LAWYER = PERSON_2

POPESCU_ID = (
    f"POPESCU ION-ANDREI, CNP {POPESCU.cnp}, cu domiciliul în Mun. Cluj-Napoca, Str. Florilor "
    "nr. 5, bl. A2, sc. 1, et. 3, ap. 10, jud. Cluj, țara România, cetățenia Română, născut în "
    "Mun. Sibiu, jud. Sibiu, țara România, la data de 14.11.1987, identificat prin CI, seria AX, "
    "nr. 123456, emisă de SPCLEP Cluj-Napoca, la data de 22.06.2022, valabilă până la data de "
    "14.11.2032"
)
MURESAN_ID = (
    f"MUREȘAN VLAD, CNP {MURESAN.cnp}, cu domiciliul în Mun. Alba Iulia, Str. Mihai Viteazul "
    "nr. 12, bl. A2, sc. 1, et. 3, ap. 10, jud. Alba, țara România, cetățenia Română, născut în "
    "Mun. Aiud, jud. Alba, țara România, la data de 21.07.1979, identificat prin CI, seria AX, "
    "nr. 781245, emisă de SPCLEP Alba Iulia, la data de 21.07.2015, valabilă până la data de "
    "21.07.2035"
)

# Two associates; the second one, not the first, administers the company.
ACT_TWO_ASSOCIATES = [
    "ACT CONSTITUTIV",
    "al Societății EXEMPLU SOFT S.R.L.",
    "Asociați:",
    f"{POPESCU_ID};",
    f"{MURESAN_ID}.",
    "CAPITOLUL I",
    "Forma juridică, denumirea, durata, sediul social",
    "Art. 1.2. — Denumirea societății este: EXEMPLU SOFT — societate cu răspundere "
    "limitată/S.R.L, conform dovezii privind disponibilitatea firmei nr. 123456 din 01.09.2026, "
    "eliberată de Oficiul Național al Registrului Comerțului.",
    "Art. 1.3. — Durata de funcționare a societății este nedeterminată.",
    "Art. 1.4. — Sediul societății este : Mun. Cluj-Napoca, Str. Memorandumului nr. 28, et. II, "
    "ap. 4, camera 1, jud. Cluj.",
    "Art. 1.5. — Societatea va putea înființa sedii secundare.",
    "CAPITOLUL II",
    "Obiectul de activitate al societății",
    "Art. 2.1. — Obiectul de activitate al societății este: Realizarea software-ului la comandă",
    "Domeniul principal de activitate corespunde grupei CAEN 620, căruia îi corespunde clasa "
    "CAEN 6201.",
    "— activitatea principală clasa CAEN 6201 și denumirea activității Activități de realizare a "
    "software-ului la comandă principal",
    "— activități secundare:",
    "— clasa CAEN 6202 și denumirea activității Activități de consultanță în tehnologia "
    "informației;",
    "—  clasa CAEN 6209 și denumirea activității Alte activități de servicii privind tehnologia "
    "informației-",
    "CAPITOLUL III",
    "Capitalul social, părțile sociale",
    "Art. 3.1. — La constituire, capitalul social subscris al societății este de : 1.000 lei, "
    "aport în numerar, fiind împărțit într-un număr de 100 de părți sociale, cu o valoare "
    "nominală de 10 lei/parte socială.",
    "Art. 3.2. — Capitalul social este deținut de către asociați astfel:",
    f"POPESCU ION-ANDREI, CNP {POPESCU.cnp}, deține 60 de părți sociale;",
    f"MUREȘAN VLAD, CNP {MURESAN.cnp}, deține o participație de 40% din capitalul social.",
    "CAPITOLUL VI",
    "Administrarea societății",
    "Art. 6.1. — Administrarea societății se face de către:",
    f"{MURESAN_ID}, pe perioadă de 4 ani, cu posibilitatea reînnoirii mandatului.",
    "CAPITOLUL X",
    "Beneficiarii reali ai societății",
    "Art. 10. — În conformitate cu prevederile art. 56 din Legea nr. 129/2019, beneficiarul real "
    "al societății este:",
    f"{POPESCU_ID}, deține o participație de 60% din capitalul social al societății.",
    "Modalitatea în care se exercită controlul asupra societății:",
    "— potrivit prevederilor art. 4 alin. (2) lit. a) pct. 1 din Legea nr. 129/2019 (deținere "
    "directă a unui procent de peste 25% din părțile sociale, respectiv 60%).",
    "CAPITOLUL XII",
    "Data: 10.09.2026",
]

# One associate who is also the administrator and the beneficial owner (the layout of the ONRC
# model filers use).
ACT_SOLE_ASSOCIATE = [
    "ACT CONSTITUTIV",
    "al Societății EXEMPLU VERDE S.R.L.",
    "Asociat unic:",
    f"{POPESCU_ID}.",
    "Art. 1.2. — Denumirea societății este: EXEMPLU VERDE S.R.L.— societate cu răspundere "
    "limitată/S.R.L., conform dovezii privind disponibilitatea firmei nr. 654321 din 02.09.2026.",
    "Art. 1.4. — Sediul societății este în Mun. Timișoara, Ale. Teilor nr. 4, bl. 12, "
    "et. VII, ap. 31, camera 1, jud. Timiș.",
    "Art. 3.1. — La constituire, capitalul social subscris al societății este de 500 lei, aport "
    "în numerar, fiind împărțit într-un număr de 50 de părți sociale.",
    "Art. 3.2. — Capitalul social este deținut de către unicul asociat astfel:",
    f"{POPESCU_ID}, deține o participație de 100% din capitalul social al societății.",
    "Art. 6.1. — Administrarea societății se face de către:",
    f"{POPESCU_ID}, pe perioadă de 30 de ani, cu posibilitatea reînnoirii mandatului.",
    "Art. 10. — beneficiarul real al societății este:",
    f"{POPESCU_ID}, deține o participație de 100% din capitalul social al societății.",
    "— potrivit prevederilor art. 4 alin. (2) lit. a) pct. 1 din Legea nr. 129/2019 (deținere "
    "directă a unui procent de peste 25% din părțile sociale, respectiv 100%).",
]

NAME_RESERVATION = [
    "MINISTERUL JUSTIŢIEI",
    "OFICIUL NAŢIONAL AL REGISTRULUI COMERŢULUI",
    "Bucuresti, Bd. Unirii nr. 74, Bl. J3b, tronson II+III, sector 3; E-mail: onrc@onrc.ro",
    "DOVADĂ",
    "privind disponibilitatea şi rezervarea denumirii firmei",
    "Formular nr.17",
    "Nr.: 123456/01.09.2026",
    "În temeiul art.50 alin (6) din Legea nr.265/2022 s-a procedat la verificarea",
    "disponibilităţii denumirii firmei EXEMPLU SOFT S.R.L. solicitată de",
    "Mureșan Vlad, în calitate de administrator.",
    "Rezervarea firmei este valabilă până la data 01.10.2026.",
]

COMODAT = [
    "CONTRACT DE COMODAT DIN 09.09.2026",
    "ART. 1 - PĂRȚILE",
    "VASILE ANA, legitimată cu carte de identitate seria TM nr. 112233, cu",
    "domiciliul în Ale. Teilor nr. 4, Bl. 12, Ap. 31, Mun. Timișoara, județul Timiș, în calitate",
    "de comodant",
    "și",
    "EXEMPLU VERDE SRL, în curs de înființare, reprezentată de către domnul Popescu Ion-Andrei,",
    "în calitate de comodatar.",
    "ART. 2 - OBIECTUL CONTRACTULUI",
    "Cedarea, de către comodant, cu titlu de împrumut gratuit, a dreptului de folosință pentru",
    "imobilul situat în Jud. Timiș, Mun. Timișoara, Ale. Teilor, Nr. 4, Bl. 12, Et. VII, Ap. 31,",
    "Camera 1. Spațiul antemenționat va fi utilizat cu destinația de sediu social fără desfășurare",
    "de activitate.",
]

# The premises described before their address, the firm described before "în curs de
# constituire", two owners: the wording of another comodat filed with the act.
COMODAT_APARTMENT = [
    "CONTRACT DE COMODAT",
    "ÎNTRE PĂRȚILE:",
    "COMODANȚI:",
    "VASILE ION, domiciliat în Jud. Brașov, Mun. Brașov, Str. Exemplului nr. 7, bl. B3, sc. 2,",
    "et. 4, ap. 18, posesor al C.I. seria BV nr. 112233.",
    "VASILE ANA, domiciliată în Jud. Brașov, Mun. Brașov, Str. Exemplului nr. 7, bl. B3, sc. 2,",
    "et. 4, ap. 18, posesoare a C.I. seria BV nr. 445566.",
    "și",
    "COMODATAR:",
    "EXEMPLU VERDE S.R.L., persoană juridică română în curs de constituire, reprezentată de dl.",
    "POPESCU ION-ANDREI, în calitate de Administrator și Asociat Unic, domiciliat în Jud. Cluj,",
    "Mun. Cluj-Napoca, Str. Florilor nr. 5, bl. A2, sc. 1, et. 3, ap. 10.",
    "A intervenit prezentul contract de comodat în următoarele condiții:",
    "Art. 1. OBIECTUL CONTRACTULUI Comodanții dau cu titlu de folosință gratuită comodatarului",
    "imobilul — apartament cu două camere, situat la etajul 4, cu suprafața utilă de 45,10 mp și",
    "suprafața totală de 50,20 mp — situat în Mun. Brașov, Str. Exemplului nr. 7, bl. B3, sc. 2,",
    "et. 4, ap. 18, judeţul Brașov.",
    "Art. 2. DURATA CONTRACTULUI Împrumutul de folosință se face pe o perioadă de 10 ani.",
]


def text_of(paragraphs: list[str]) -> str:
    return "\n".join(paragraphs)


def values_of(fields) -> dict[str, str]:
    return {field.name: field.value for field in fields}


# --------------------------------------------------------------------------- the act constitutiv


def test_act_constitutiv_company():
    found = values_of(extract_articles(text_of(ACT_TWO_ASSOCIATES)))
    # "EXEMPLU SOFT — societate cu răspundere limitată/S.R.L": the legal form is added
    assert found["company_name"] == "EXEMPLU SOFT S.R.L."
    assert found["company_address"] == (
        "Mun. Cluj-Napoca, Str. Memorandumului nr. 28, et. II, ap. 4, camera 1, jud. Cluj"
    )
    assert found["share_capital"] == "1.000 lei"
    assert found["share_count"] == "100"
    assert found["name_reservation_number"] == "123456"
    assert found["name_reservation_date"] == "01.09.2026"
    assert "company_duration" not in found  # nedeterminată: the form's empty value
    # the main activity first, the name as the act writes it ("principal" and dashes dropped)
    assert found["caen_activities"].splitlines() == [
        "6201 Activități de realizare a software-ului la comandă",
        "6202 Activități de consultanță în tehnologia informației",
        "6209 Alte activități de servicii privind tehnologia informației",
    ]


def test_act_constitutiv_persons_and_their_roles():
    found = values_of(extract_articles(text_of(ACT_TWO_ASSOCIATES)))
    # the administrator signs the requests: person 1, though the act names them second
    assert (found["last_name"], found["first_name"], found["cnp"]) == (
        "Mureșan",
        "Vlad",
        MURESAN.cnp,
    )
    assert found["city"] == "Mun. Alba Iulia" and found["id_number"] == "781245"
    assert found["board_role"] == "administrator unic"
    assert found["capacity"] == "asociat și administrator"
    assert found["shares"] == "40"  # 40% of 100 părți sociale
    assert found["board_term_years"] == "4"
    assert "beneficial_owner" not in found
    # the other associate, also the beneficial owner
    assert (found["p2_last_name"], found["p2_cnp"]) == ("Popescu", POPESCU.cnp)
    assert found["p2_shares"] == "60"
    assert "p2_board_role" not in found
    assert found["p2_beneficial_owner"] == CONTROL_OPTIONS[0]
    assert found["p2_control_description"].startswith("deținere directă")
    assert found["associates"].splitlines() == [
        f"MUREȘAN VLAD | {MURESAN.cnp} | 40 părți sociale (40%)",
        f"POPESCU ION-ANDREI | {POPESCU.cnp} | 60 părți sociale (60%)",
    ]
    assert not any(name.startswith("p3_") for name in found)


def test_sole_associate_administrator_and_beneficial_owner():
    found = values_of(extract_articles(text_of(ACT_SOLE_ASSOCIATE)))
    assert found["company_name"] == "EXEMPLU VERDE S.R.L."
    assert found["capacity"] == "asociat unic și administrator"
    assert (found["last_name"], found["first_name"]) == ("Popescu", "Ion-Andrei")
    assert found["shares"] == "50"
    assert found["board_role"] == "administrator unic"
    assert found["beneficial_owner"] == CONTROL_OPTIONS[0]
    assert found["associates"] == f"POPESCU ION-ANDREI | {POPESCU.cnp} | 50 părți sociale (100%)"
    assert found["board_term_years"] == "30"
    assert not any(name.startswith("p2_") for name in found)  # one person, named four times


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Denumirea societății este: ALFA PLUS — societate cu răspundere limitată/S.R.L", "ALFA "
         "PLUS S.R.L."),
        ("Denumirea societății este: „ALFA PLUS” — societate pe acțiuni/S.A.", "ALFA PLUS S.A."),
        ("Denumirea societății este: ALFA PLUS SRL, conform dovezii nr. 1", "ALFA PLUS SRL"),
        ("ACT CONSTITUTIV\nal Societății BETA 2 CUP S.R.L.\nAsociat unic:", "BETA 2 CUP S.R.L."),
    ],
)  # fmt: skip
def test_company_name_written_in_several_ways(line, expected):
    assert company_name(line + "\n")[0] == expected


@pytest.mark.parametrize(
    "line",
    [
        "Sediul societății este : Mun. Cluj-Napoca, Str. Florilor nr. 5, jud. Cluj.",
        "Sediul societății este în Mun. Cluj-Napoca, Str. Florilor nr. 5, jud. Cluj.",
        "Sediul social al societății este stabilit în Mun. Cluj-Napoca, Str. Florilor nr. 5, jud. "
        "Cluj.",
        "Sediul social: Mun. Cluj-Napoca, Str. Florilor nr. 5, jud. Cluj.",
    ],
)
def test_registered_office_written_in_several_ways(line):
    following = "\nArt. 1.5. — Societatea va putea înființa sedii secundare."
    assert company_address(line + following)[0] == (
        "Mun. Cluj-Napoca, Str. Florilor nr. 5, jud. Cluj"
    )


def test_registered_office_parts_keep_the_room_and_the_floor():
    address = "Mun. Timișoara, Ale. Teilor nr. 4, bl. 12, et. VII, ap. 31, camera 1, jud. Timiș"
    parts = parse_ro_address(address)
    assert parts["street"] == "Ale. Teilor"
    assert (parts["floor"], parts["apartment"], parts["room"]) == ("VII", "31", "camera 1")
    derived = complete_values({"company_address": address})
    assert derived["company_street"][0] == "Ale. Teilor, camera 1"  # no box for the room
    assert derived["company_apartment"][0] == "31"
    assert derived["company_county"][0] == "Timiș"
    assert derived["orc_office"][0] == "Timiș"


def test_caen_lines_outside_the_act_wording():
    text = "Obiectul principal de activitate:\n4711 - Comerț cu amănuntul\nCAPITOLUL III\n"
    assert caen_activities(text) == ["4711 Comerț cu amănuntul"]
    assert caen_activities("Art. 2. Codul civil, art. 2146-2157.\n") == []


# --------------------------------------------------------------------------- other documents


def test_name_reservation_proof():
    found = values_of(extract_name_reservation(text_of(NAME_RESERVATION)))
    # not the trade register's own address or e-mail, nor the person who asked
    assert found == {
        "company_name": "EXEMPLU SOFT S.R.L.",
        "name_reservation_number": "123456",
        "name_reservation_date": "01.09.2026",
    }


def test_proof_of_registered_office():
    found = values_of(extract_premises(text_of(COMODAT)))
    assert found == {
        "company_address": "Jud. Timiș, Mun. Timișoara, Ale. Teilor, Nr. 4, Bl. 12, Et. VII, "
        "Ap. 31, Camera 1",
        "company_name": "EXEMPLU VERDE SRL",
    }


@pytest.mark.parametrize(
    ("premises", "confidence"),
    [
        # how OCR may read "imobilul situat în" and the floor "VII" on a scan
        ("imobllul sltuat Tn Jud. Timiș, Mun. Timișoara, Ale. Teilor, Nr. 4, Bl. 12, Et. Vil,",
         0.85),
        ("imobilul situat, la adresa: Jud. Timiș, Mun. Timișoara, Ale. Teilor, Nr. 4, Bl. 12, "
         "Et. Vll,", 0.85),
        # "situat" misread beyond recognition: the first address after the premises
        ("imobilul sifuat ín Jud. Timiș, Mun. Timișoara, Ale. Teilor, Nr. 4, Bl. 12, Et. VII,",
         0.7),
        ("imobilul din Jud. Timiș, Mun. Timișoara, Ale. Teilor, Nr. 4, Bl. 12, Et. VII,", 0.7),
    ],
)  # fmt: skip
def test_proof_of_registered_office_as_ocr_reads_it(premises, confidence):
    text = text_of(COMODAT).replace(
        "imobilul situat în Jud. Timiș, Mun. Timișoara, Ale. Teilor, Nr. 4, Bl. 12, Et. VII,",
        premises,
    )
    (found,) = [field for field in extract_premises(text) if field.name == "company_address"]
    assert found.confidence == confidence
    derived = complete_values({"company_address": found.value})
    office = {name: value for name, (value, _) in derived.items()}
    assert address_line(office, "company_", county="county") == (
        "Mun. Timișoara, Ale. Teilor nr. 4, bl. 12, et. VII, ap. 31, camera 1, jud. Timiș"
    )


@pytest.mark.parametrize(
    "scanned",
    [
        False,
        # the text a phone scanner app lays over its scan: "~" for ș, "judeftil" for județul
        True,
    ],
)
def test_the_premises_described_before_their_address(scanned):
    text = text_of(COMODAT_APARTMENT)
    if scanned:
        article = text.index("Art. 1.")
        text = text[:article] + (
            text[article:]
            .replace("Brașov", "Bra~ov")
            .replace("situat în", "situat in")
            .replace("judeţul", "judeftil")
        )
    found = values_of(extract_premises(text))
    # not the floor ("situat la etajul 4"), nor an owner's or the representative's domicile
    assert found["company_name"] == "EXEMPLU VERDE S.R.L."
    derived = complete_values({"company_address": found["company_address"]})
    office = {name: value for name, (value, _) in derived.items()}
    assert address_line(office, "company_", county="county") == (
        "Mun. Brașov, Str. Exemplului nr. 7, bl. B3, sc. 2, et. 4, ap. 18, jud. Brașov"
    )


def test_no_premises_no_office():
    assert extract_premises("Spațiul va fi utilizat în vederea stabilirii sediului.") == []


ACTIVITIES = "6201 Activități de realizare a software-ului la comandă\n6202 Consultanță IT"


@pytest.mark.parametrize(
    ("activities_at_office", "at_office", "at_third_parties"),
    [
        # by default at third parties: the main activity first, each class once
        ("", [], ["6201", "6202", "4321"]),
        (
            "x",
            ["6201", "6202"],
            ["4321", "6202"],
        ),  # carried out at the office; a class may be at both
    ],
)
def test_anexa_4_places_the_activities_at_third_parties_unless_at_the_office(
    activities_at_office, at_office, at_third_parties
):
    values = {
        "caen_activities": ACTIVITIES,
        "caen_third_party": "4321 Lucrări de instalații electrice\n6202 Consultanță IT",
        "activities_at_office": activities_at_office,
    }
    filled = form_values(document("onrc-anexa-4"), with_computed(values))
    assert [filled[f"clasa_caen.0.{i}"] for i in range(len(at_office))] == at_office
    assert f"clasa_caen.0.{len(at_office)}" not in filled
    rows = [filled[f"clasa_caen.1.{i}"] for i in range(len(at_third_parties))]
    assert rows == at_third_parties
    assert f"clasa_caen.1.{len(at_third_parties)}" not in filled


def test_a_filled_anexa_4_is_read_back_as_the_activities(settings):
    template = document("onrc-anexa-4")
    docfill = DocFill(settings, templates=lambda: [template])
    for at_office, field in (("x", "caen_activities"), ("", "caen_third_party")):
        values = {"caen_activities": ACTIVITIES, "activities_at_office": at_office}
        pdf = docfill.fill(template, None, values, allow_missing=True).pdf
        analysis = docfill.analyze_bytes(pdf, "anexa4.pdf")
        assert analysis.extraction.fields[field].value.splitlines()[0].startswith("6201")


def test_a_party_to_a_contract_is_not_the_applicant(settings):
    # read as an unknown document, the owner's clause gives no capacity of the applicant
    assert "capacity" not in values_of(extract_clauses(text_of(COMODAT)))
    # read as the proof of the registered office: nothing about a person at all
    extracted = FieldExtractor(settings, use_ner=False).extract(
        SanitizedDocument(source="comodat.pdf", text=text_of(COMODAT)), "dovada_sediu"
    )
    assert {"capacity", "last_name", "cnp", "id_number", "city"}.isdisjoint(extracted.fields)
    assert extracted.fields["company_city"].value == "Mun. Timișoara"


# --------------------------------------------------------------------------- document types


def test_document_types_by_their_title():
    known = [
        DocType(name, name, "", ("seed text of at least eight words for the type",))
        for name in ("act_constitutiv", "dovada_denumire", "dovada_sediu")
    ]
    classifier = DocTypeClassifier(extra_types=lambda: known)
    for paragraphs, expected in (
        (ACT_TWO_ASSOCIATES, "act_constitutiv"),
        (NAME_RESERVATION, "dovada_denumire"),
        (COMODAT, "dovada_sediu"),
    ):
        prediction = classifier.predict(text_of(paragraphs))
        assert (prediction.doc_type, prediction.method) == (expected, "title")
    # a mark of the scan before the title; a title OCR lost, with both parties named
    assert classifier.by_title("| " + text_of(COMODAT)) == "dovada_sediu"
    untitled = text_of(COMODAT[1:]).replace("CONTRACT DE COMODAT", "")
    assert classifier.by_title("C0NTRAGT DE C0MQDAT\n" + untitled) == "dovada_sediu"
    # the title as a scanner app reads it, the parties as they are named in the plural
    glued = text_of(COMODAT_APARTMENT).replace("CONTRACT DE COMODAT", "CONTRACTDECOMODAT", 1)
    assert classifier.by_title(glued) == "dovada_sediu"
    # not a list of documents that names a contract
    assert classifier.by_title("Opis\n3. Contract de comodat nr. 1\n4. Dovada") is None
    # a type the classifier was not taught is never named by its title
    assert DocTypeClassifier().by_title(text_of(ACT_TWO_ASSOCIATES)) is None


# --------------------------------------------------------------------------- persons and filing


@pytest.fixture
def context(settings):
    context = build_app(settings, use_ner=False)
    context.knowledge.seed()
    with context.sessions() as session:
        repository = TemplateRepository(session)
        for spec in load_directory(bundled_specs_dir()):
            repository.save(spec)
    return context


def test_the_act_names_the_persons_and_a_stranger_files_the_request(context):
    docfill = context.docfill
    act = docfill.analyze_bytes(make_docx(ACT_TWO_ASSOCIATES), "act.docx")
    lawyer = docfill.analyze_bytes(
        sworn_statement_docx(LAWYER), "avocat.docx", "declaratie_administrator"
    )
    administrator = docfill.analyze_bytes(
        sworn_statement_docx(MURESAN), "muresan.docx", "declaratie_administrator"
    )
    name_proof = docfill.analyze_bytes(make_docx(NAME_RESERVATION), "dovada.docx")
    combined = docfill.combine([lawyer, act, administrator, name_proof])
    assert act.doc_type.doc_type == "act_constitutiv"
    assert name_proof.doc_type.doc_type == "dovada_denumire"
    # the act numbers its persons; the administrator's statement has their CNP: person 1; the
    # lawyer is named nowhere in the act: the representative filing the request
    assert [a.person for a in (lawyer, act, administrator, name_proof)] == [
        REPRESENTATIVE,
        None,
        1,
        None,
    ]
    values = {name: field.value for name, field in combined.fields.items()}
    assert (values["last_name"], values["cnp"]) == ("Mureșan", MURESAN.cnp)
    assert values["p2_last_name"] == "Popescu"
    assert (values["filer_last_name"], values["filer_cnp"]) == ("Ionescu", LAWYER.cnp)
    assert (values["filer_id_series"], values["filer_id_number"]) == ("TZ", "604213")
    assert (values["contact_last_name"], values["contact_first_name"]) == ("Ionescu", "Maria")
    assert values["contact_city"] == "Mun. Cluj-Napoca"
    assert values["company_name"] == "EXEMPLU SOFT S.R.L."  # not the lawyer's statement's
    assert values["capacity"] == "asociat și administrator"
    assert not any(name.startswith("p3_") for name in values)

    overrides = {"representative_type": REPRESENTATIVE_TYPES[0]}
    request = read_form_values(
        docfill.fill(document("onrc-anexa-2a"), combined, overrides, allow_missing=True).pdf
    )
    assert (request["nume"], request["prenume"]) == ("MUREȘAN", "VLAD")
    assert request["calitate"] == "asociat și administrator"
    assert (request["prin"], request["conform"]) == ("avocat", "împuternicirii avocațiale")
    assert request["pentru firma"] == "EXEMPLU SOFT S.R.L."
    assert (request["sediu_strada"], request["sediu_etaj"]) == (
        "Memorandumului, camera 1",
        "II",
    )
    assert request["ORC"] == "Cluj"
    assert request["pg. 4 text 27"] == "MUREȘAN VLAD"
    assert (request["pg. 4 text 31"], request["pg. 4 text 36"]) == ("Ionescu", LAWYER.cnp)
    assert (request["pg. 4 text 37"], request["pg. 4 text 38"]) == (
        "avocat",
        "împuternicirii avocațiale",
    )
    assert request["pg. 3 text 61"] == "Ionescu"  # the contact person

    declaration = read_form_values(
        docfill.fill(document("onrc-anexa-4"), combined, allow_missing=True).pdf
    )
    assert (declaration["SubNume"], declaration["SubCNP"]) == ("MUREȘAN", MURESAN.cnp)
    assert declaration["SubCalitate"] == "asociat și administrator"
    assert declaration["InmFirma"] == "EXEMPLU SOFT S.R.L."
    assert declaration["InmLocalitatea"] == "Mun. Cluj-Napoca"
    # the activities of the act, at third parties (3.2): nothing at the registered office (3.1)
    assert [declaration[f"clasa_caen.1.{i}"] for i in range(3)] == ["6201", "6202", "6209"]
    assert (
        declaration["clasa_caen_desc.1.0"] == "Activități de realizare a software-ului la comandă"
    )
    assert "clasa_caen.0.0" not in declaration


def test_a_card_with_a_misread_cnp_still_goes_to_its_person(context):
    docfill = context.docfill
    act = docfill.analyze_bytes(make_docx(ACT_TWO_ASSOCIATES), "act.docx")
    misread = text_of(sworn_statement_text(MURESAN)).replace(MURESAN.cnp, "1790721010880")
    statement = docfill.analyze_bytes(
        make_docx(misread.split("\n")), "m.docx", "declaratie_administrator"
    )
    lawyer = docfill.analyze_bytes(
        sworn_statement_docx(LAWYER), "l.docx", "declaratie_administrator"
    )
    docfill.combine([act, statement, lawyer])
    assert (statement.person, lawyer.person) == (1, REPRESENTATIVE)


def test_the_beneficial_owner_declaration_is_filed_by_the_lawyer(context):
    # the act alone says nobody else files it; merged with the lawyer's card, the lawyer does
    docfill = context.docfill
    act = docfill.analyze_bytes(make_docx(ACT_SOLE_ASSOCIATE), "act.docx")
    assert act.extraction.fields["bo_filed_by"].value == "reprezentantul legal"
    lawyer = docfill.analyze_bytes(
        sworn_statement_docx(LAWYER), "avocat.docx", "declaratie_administrator"
    )
    combined = docfill.combine([act, lawyer])
    assert combined.fields["bo_filed_by"].value == "împuternicit"
    assert docfill.combine([act]).fields["bo_filed_by"].value == "reprezentantul legal"


def test_without_an_act_the_first_identity_card_is_the_applicant(context):
    docfill = context.docfill
    analyses = [
        docfill.analyze_bytes(sworn_statement_docx(person), f"{n}.docx", "declaratie_administrator")
        for n, person in (("popescu", POPESCU), ("ionescu", LAWYER))
    ]
    docfill.combine(analyses)
    assert [a.person for a in analyses] == [1, 2]


def test_anexa_4_from_the_act_alone_lists_the_activities_at_third_parties(context):
    docfill = context.docfill
    combined = docfill.combine([docfill.analyze_bytes(make_docx(ACT_TWO_ASSOCIATES), "act.docx")])
    template = document("onrc-anexa-4")
    declaration = read_form_values(docfill.fill(template, combined, allow_missing=True).pdf)
    assert "clasa_caen.0.0" not in declaration  # 3.1. Sediu social/profesional
    assert [declaration[f"clasa_caen.1.{i}"] for i in range(3)] == ["6201", "6202", "6209"]
    # ticked in the review: carried out at the registered office
    ticked = docfill.fill(template, combined, {"activities_at_office": "x"}, allow_missing=True)
    declaration = read_form_values(ticked.pdf)
    assert declaration["clasa_caen.0.0"] == "6201" and "clasa_caen.1.0" not in declaration
    rows = {row.name: row for row in field_rows(template, combined, 0.5)}
    assert rows["activities_at_office"].value == "" and rows["activities_at_office"].kind == (
        "checkbox"
    )


@requires_tesseract
def test_identity_card_of_the_lawyer(context):
    docfill = context.docfill
    act = docfill.analyze_bytes(make_docx(ACT_SOLE_ASSOCIATE), "act.docx")
    card = docfill.analyze_bytes(ro_id_card_jpeg(LAWYER), "ci-avocat.jpg")
    combined = docfill.combine([card, act])
    assert card.person == REPRESENTATIVE
    assert combined.fields["last_name"].value == "Popescu"
    assert combined.fields["filer_cnp"].value == LAWYER.cnp
    assert combined.fields["filer_id_number"].value == LAWYER.number
    assert "p2_cnp" not in combined.fields


def test_extract_texts_keeps_the_numbering_of_the_act(context):
    lawyer = text_of(sworn_statement_text(LAWYER))
    result = context.docfill.extract_texts(
        [
            ("act.docx", text_of(ACT_TWO_ASSOCIATES), "act_constitutiv", 3),  # person ignored
            ("avocat.docx", lawyer, "declaratie_administrator", REPRESENTATIVE),
        ]
    )
    assert result.fields["last_name"].value == "Mureșan"
    assert result.fields["p2_last_name"].value == "Popescu"
    assert result.fields["filer_last_name"].value == "Ionescu"
    assert "p3_last_name" not in result.fields


def test_wizard_shows_whose_document_and_asks_for_the_representative(settings, context, tmp_path):
    settings = settings.model_copy(update={"output_dir": tmp_path / "out"})
    with TestClient(create_app(settings)) as client:
        kinds = {t["name"]: t["persons"] for t in client.get("/doctypes").json()}
        assert kinds["act_constitutiv"] == "named" and kinds["dovada_sediu"] == "none"
        assert kinds["id_card"] == "one"
        files = [
            ("files", ("act.docx", make_docx(ACT_SOLE_ASSOCIATE), "application/octet-stream")),
            ("files", ("avocat.docx", sworn_statement_docx(LAWYER), "application/octet-stream")),
        ]
        templates = ["onrc-anexa-2a", "onrc-anexa-4"]
        body = client.post("/wizard/analyze", files=files, data={"templates": templates}).json()
        documents = body["documents"]
        # the act names its persons; a statement of nobody it names: the representative
        assert [d["person"] for d in documents] == [None, REPRESENTATIVE]
        payload = [
            {"source": d["source"], "text": d["text"], "doc_type": d["doc_type"], "person": p}
            for d, p in zip(documents, (1, REPRESENTATIVE), strict=True)
        ]
        again = client.post(
            "/wizard/reextract", json={"templates": templates, "documents": payload}
        ).json()
        rows = {row["name"]: row for row in again["rows"]}
        assert rows["last_name"]["value"] == "Popescu"
        assert rows["filer_last_name"]["value"] == "Ionescu"
        assert rows["representative_type"]["choices"] == list(REPRESENTATIVE_TYPES)
        assert rows["representative_type"]["value"] == ""  # asked each time
        values = {name: row["value"] for name, row in rows.items() if row["value"]}
        preview = client.post("/wizard/preview", json={"templates": templates, "values": values})
        assert "representative_type" in preview.json()["issues"]


# --------------------------------------------------------------------------- lawyer or proxy


@pytest.mark.parametrize(
    ("kind", "who", "basis"),
    [
        (REPRESENTATIVE_TYPES[0], "avocat", "împuternicirii avocațiale"),
        (REPRESENTATIVE_TYPES[1], "împuternicit", "procurii speciale autentice"),
        (REPRESENTATIVE_TYPES[2], "împuternicit", "procurii generale autentice"),
    ],
)
def test_representative_writes_prin_and_conform(kind, who, basis):
    assert representation(kind) == {
        "represented_by": who,
        "representation_basis": basis,
        "filer_capacity": who,
        "filer_basis": basis,
    }
    derived = complete_values({"representative_type": kind, "filer_last_name": "Ionescu"})
    assert derived["represented_by"][0] == who and derived["filer_basis"][0] == basis


def test_lawyer_or_proxy_chosen_without_anybody_filing(settings):
    # "lawyer" chosen but no representative read (only the act uploaded): no crash, and the
    # beneficial owner declaration is filed by the legal representative
    values = {
        "representative_type": REPRESENTATIVE_TYPES[0],
        "last_name": "Popescu",
        "beneficial_owner": CONTROL_OPTIONS[0],
    }
    derived = complete_values(values)
    assert derived["represented_by"][0] == "avocat"
    assert derived["bo_filed_by"][0] == "reprezentantul legal"
    docfill = DocFill(settings)
    for name in ("onrc-anexa-4", "onrc-anexa-2a", "onrc-declaratie-beneficiari-reali"):
        assert docfill.fill(document(name), None, values, allow_missing=True).pdf


def test_lawyer_or_proxy_is_asked_each_time():
    request = document("onrc-anexa-2a")
    remembered = set(request.remember)
    assert {"represented_by", "representation_basis", "filer_capacity", "filer_basis"}.isdisjoint(
        remembered
    )
    assert "representative_type" not in remembered
    assert "representative_type" in request.input_fields()
    assert "representative_type" not in request.field_names()  # no box prints it
    rows = {row.name: row for row in field_rows(request, None, 0.5, memory={})}
    assert rows["representative_type"].value == ""
    issue = validate_values({"filer_last_name": "Ionescu"})["representative_type"]
    assert "lawyer" in issue[0]
    assert "representative_type" not in validate_values(
        {"filer_last_name": "Ionescu", "representative_type": REPRESENTATIVE_TYPES[1]}
    )
    assert "representative_type" not in validate_values({"last_name": "Popescu"})


# --------------------------------------------------------------------------- identity cards


def test_identity_card_number_after_a_misread_nr():
    found = values_of(
        extract_identity("CARTE DE IDENTITATE seria XB na 512834 \n", None, "id_card")
    )
    assert (found["id_series"], found["id_number"]) == ("XB", "512834")


def test_specks_after_a_locality_are_dropped():
    assert parse_ro_address("Jud.BV Mun.Brașov Sr")["city"] == "Mun. Brașov"
    assert parse_ro_address("Jud.CJ Mun.Cluj Napoca")["city"] == "Mun. Cluj-Napoca"


def test_wizard_takes_the_representative_apart(settings, context, tmp_path):
    settings = settings.model_copy(update={"output_dir": tmp_path / "out"})
    lawyer = ("avocat.docx", sworn_statement_docx(LAWYER), "application/octet-stream")
    templates = ["onrc-anexa-2a"]
    with TestClient(create_app(settings)) as client:
        # with an act constitutiv, the lawyer's card given apart, lawyer chosen in step 1
        body = client.post(
            "/wizard/analyze",
            files=[
                ("files", ("act.docx", make_docx(ACT_SOLE_ASSOCIATE), "application/octet-stream")),
                ("representative", lawyer),
            ],
            data={"templates": templates, "representative_type": REPRESENTATIVE_TYPES[0]},
        ).json()
        assert [d["person"] for d in body["documents"]] == [None, REPRESENTATIVE]
        rows = {row["name"]: row["value"] for row in body["rows"]}
        assert rows["last_name"] == "Popescu" and rows["filer_last_name"] == "Ionescu"
        assert rows["representative_type"] == REPRESENTATIVE_TYPES[0]
        assert (rows["represented_by"], rows["filer_basis"]) == (
            "avocat",
            "împuternicirii avocațiale",
        )
        # without an act constitutiv (a change, a closing): the slot still says who files
        statement = ("popescu.docx", sworn_statement_docx(POPESCU), "application/octet-stream")
        body = client.post(
            "/wizard/analyze",
            files=[("files", statement), ("representative", lawyer)],
            data={"templates": templates},
        ).json()
        assert [d["person"] for d in body["documents"]] == [1, REPRESENTATIVE]
        rows = {row["name"]: row["value"] for row in body["rows"]}
        assert (rows["last_name"], rows["filer_last_name"]) == ("Popescu", "Ionescu")
        assert rows["contact_first_name"] == "Maria" and not rows.get("p2_last_name")
        assert rows["representative_type"] == ""  # not chosen: asked in the review
        wrong = client.post(
            "/wizard/analyze",
            files=[("representative", lawyer)],
            data={"templates": templates, "representative_type": "notar"},
        )
        assert wrong.status_code == 422


def test_the_page_offers_the_same_representative_types():
    from docfill.web import wizard_page

    page = wizard_page()
    assert all(f'"{kind}"' in page for kind in REPRESENTATIVE_TYPES)
    assert 'id="rep-input"' in page


# --------------------------------------------------------------------------- the operation


@pytest.mark.parametrize(
    ("procedure", "ticked", "box", "other_box"),
    [
        ("srl.infiintare", "request_registration", "Box1", "Box2"),  # înmatriculare
        ("srl.modificare", "request_mentions", "Box2", "Box1"),  # modificare
        (None, None, None, None),  # no operation chosen: nothing ticked
    ],
)
def test_the_operation_ticks_its_request_on_the_beneficial_owner_declaration(
    settings, context, tmp_path, procedure, ticked, box, other_box
):
    settings = settings.model_copy(update={"output_dir": tmp_path / "out"})
    templates = ["onrc-declaratie-beneficiari-reali"]
    act = ("act.docx", make_docx(ACT_SOLE_ASSOCIATE), "application/octet-stream")
    data = {"templates": templates, **({"procedure": procedure} if procedure else {})}
    with TestClient(create_app(settings)) as client:
        body = client.post("/wizard/analyze", files=[("files", act)], data=data).json()
        rows = {row["name"]: row for row in body["rows"]}
        by_hand = client.post(
            "/wizard/reextract",
            json={"templates": templates, "procedure": procedure, "documents": []},
        ).json()
        manual_rows = {row["name"]: row["value"] for row in by_hand["rows"]}
        for request in ("request_registration", "request_mentions"):
            expected = "x" if request == ticked else ""
            assert rows[request]["value"] == expected and manual_rows[request] == expected
        if ticked:
            assert rows[ticked]["found"]["source"] == "default"  # can be unticked in the review
        values = {name: row["value"] for name, row in rows.items() if row["value"]}
        exported = client.post(
            "/wizard/export",
            json={
                "templates": templates,
                "procedure": procedure,
                "values": values,
                "allow_missing": True,
                "legal_acknowledged": True,
            },
        ).json()
        form = read_form_values((tmp_path / "out" / exported["files"][0]["filename"]).read_bytes())
        if box:
            assert box in form and other_box not in form
        else:
            assert "Box1" not in form and "Box2" not in form
        # a document without the box is left alone (nothing "other" to show either)
        alone = client.post(
            "/wizard/analyze", files=[("files", act)], data={**data, "templates": ["onrc-anexa-4"]}
        ).json()
        assert "request_registration" not in {row["name"] for row in alone["rows"]}
        assert "request_registration" not in {field["name"] for field in alone["other_fields"]}
