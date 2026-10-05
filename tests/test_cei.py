"""The electronic identity card (CEI) as the "RO CEI Reader" application exports it: one label
per line, the series and the number together ("AX1234567"), the issuing authority over three
lines, cedilla letters, words some PDF readers glue together. Every person here is
fictitious."""

import io
from datetime import date

from fastapi.testclient import TestClient
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from reportlab.pdfbase.pdfmetrics import stringWidth

from docfill.api import create_app
from docfill.doctypes import DocTypeClassifier
from docfill.extraction import FieldExtractor, cei, idcard
from docfill.models import ExtractedField, SanitizedDocument
from docfill.readers.pdf import _spaced
from docfill.samples import Person, cei_export_lines, cei_export_pdf
from tests.test_act_constitutiv_srl import COMPANY, _context, act_text

IONESCU = Person(
    last_name="IONESCU",
    first_name="MARIA-ELENA",
    sex="F",
    birth=date(2001, 3, 9),
    birth_county_code="BV",
    birth_locality="Mun.Brașov",
    county_code="BV",
    locality="Mun.Brașov",
    street_line="Str.Exemplului nr.7 Bl.B3 sc.2 et.4 ap.18",
    series="BV",
    number="1234567",
    issued_by="SPCLEP Brașov",
    issued=date(2026, 7, 6),
    expires=date(2036, 3, 9),
)
IONESCU_CLAUSE = (
    f"IONESCU MARIA-ELENA, CNP {IONESCU.cnp}, cu domiciliul în Mun. Brașov, Str. Exemplului "
    "nr. 7, bl. B3, sc. 2, et. 4, ap. 18, jud. Brașov, țara România, cetățenia Română, "
    "născută în Mun. Brașov, jud. Brașov, țara România, la data de 09.03.2001, identificată "
    "prin CEI, seria BV, nr. 1234567, emisă de SPCLEP Brașov, la data de 06.07.2026, valabilă "
    "până la data de 09.03.2036"
)


def read(text: str) -> dict[str, str]:
    return {found.name: found.value for found in cei.extract(text, "cei.pdf")}


def test_every_label_of_the_export_is_read():
    text = "\n".join(cei_export_lines(IONESCU))
    assert cei.is_export(text)
    assert read(text) == {
        "last_name": "Ionescu",
        "first_name": "Maria-Elena",
        "citizenship": "Română",
        "sex": "F",
        "cnp": IONESCU.cnp,
        "date_of_birth": "09.03.2001",
        "place_of_birth": "Jud.BV Mun.Brașov",
        "id_series": "BV",
        "id_number": "1234567",
        "id_type": "CEI",
        "id_issue_date": "06.07.2026",
        "id_expiry_date": "09.03.2036",
        "id_issued_by": "SPCLEP Brașov",
        "full_address": "Jud.BV Mun.Braşov Str.Exemplului nr.7 Bl.B3 sc.2 et.4 ap.18",
    }


def test_words_glued_together_are_separated():
    text = "\n".join(cei_export_lines(IONESCU))
    text = text.replace("Jud.BV Mun.", "Jud.BVMun.").replace(" nr.7 Bl.B3 sc.2", "nr.7Bl.B3sc.2")
    values = read(text)
    assert values["place_of_birth"] == "Jud.BV Mun.Brașov"
    assert values["full_address"].startswith("Jud.BV Mun.Braşov Str.Exemplului nr.7 Bl.B3 sc.2")


def test_the_export_without_its_footer_is_still_recognised():
    lines = cei_export_lines(IONESCU)
    assert cei.is_export("\n".join(lines[:-3]))
    assert not cei.is_export("Nume de familie: IONESCU\nPrenume: MARIA\nCNP: 123")


def test_the_export_is_an_identity_card():
    prediction = DocTypeClassifier().predict("\n".join(cei_export_lines(IONESCU)))
    assert (prediction.doc_type, prediction.method) == ("id_card", "title")


def test_cedilla_letters_become_romanian_letters():
    """The application prints "Braşov" with a cedilla ş: forms are written with the ș."""
    document = SanitizedDocument(source="cei.pdf", text="\n".join(cei_export_lines(IONESCU)))
    result = FieldExtractor(use_ner=False).extract(document, "id_card")
    values = {name: found.value for name, found in result.fields.items()}
    assert values["full_address"] == "Jud.BV Mun.Brașov Str.Exemplului nr.7 Bl.B3 sc.2 et.4 ap.18"
    assert (values["city"], values["region"]) == ("Mun. Brașov", "Brașov")
    assert (values["place_of_birth"], values["birth_county"]) == ("Mun. Brașov", "Brașov")
    assert (values["id_type"], values["id_series"], values["id_number"]) == ("CEI", "BV", "1234567")
    name = ExtractedField(name="last_name", value="Ştefănescu", confidence=0.9, source="label")
    assert idcard.review(name)[0].value == "Ștefănescu"


def _pdf_one_block_per_word(lines: list[list[str]]) -> bytes:
    """A PDF that places every word on its own, without a blank between them, as the export
    of the application does."""
    writer = PdfWriter()
    page = writer.add_blank_page(595, 842)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    fonts = DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): fonts})
    operations, y = [], 760
    for words in lines:
        x = 40.0
        for word in words:
            operations.append(f"BT /F1 12 Tf {x:.2f} {y} Td ({word}) Tj ET")
            x += stringWidth(word, "Helvetica", 12) + 3.5
        y -= 20
    content = DecodedStreamObject()
    content.set_data(" ".join(operations).encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(content)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_the_reader_that_keeps_the_blanks_between_words_is_used():
    data = _pdf_one_block_per_word([["Domiciliu:", "Jud.BV", "Mun.Brasov", "Str.Exemplului"]])
    spaced = "Domiciliu: Jud.BV Mun.Brasov Str.Exemplului"
    assert _spaced(["Domiciliu:Jud.BVMun.BrasovStr.Exemplului"], data) == [spaced]
    # text read with its blanks, or other text altogether, is kept as it is
    assert _spaced(["Domiciliu: Jud.BV Mun.Brasov\nStr.Exemplului"], data) == [
        "Domiciliu: Jud.BV Mun.Brasov\nStr.Exemplului"
    ]
    assert _spaced(["Something else entirely"], data) == ["Something else entirely"]


def test_the_act_names_the_electronic_identity_card(settings, tmp_path):
    _context(settings)
    settings = settings.model_copy(update={"output_dir": tmp_path / "out"})
    export = ("cei.pdf", cei_export_pdf(IONESCU), "application/pdf")
    templates = ["act-constitutiv-srl"]
    with TestClient(create_app(settings)) as client:
        body = client.post(
            "/wizard/analyze", files=[("associate", export)], data={"templates": templates}
        ).json()
        assert [(d["doc_type"], d["person"]) for d in body["documents"]] == [("id_card", 1)]
        rows = {row["name"]: row["value"] for row in body["rows"]}
        values = {name: value for name, value in rows.items() if value}
        values.update(COMPANY)
        exported = client.post(
            "/wizard/export",
            json={"templates": templates, "values": values, "allow_missing": True},
        ).json()
        text = act_text(client.get(exported["files"][0]["url"]).content)
    assert f"Asociat unic: {IONESCU_CLAUSE}." in text
