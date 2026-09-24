import io

import pytest
from pypdf import PdfReader

from docfill.errors import MissingFieldsError, TemplateIntegrityError
from docfill.export import fill_pdf_form, render_text_pdf
from docfill.models import ExtractedField, ExtractionResult
from docfill.pipeline import DocFill
from docfill.templates import StandardDocument, StandardDocumentSpec
from docfill.templates.models import compute_checksum
from tests.conftest import make_docx, make_pdf_form


def pdf_text(data: bytes) -> str:
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(data)).pages)


def standard(**overrides) -> StandardDocument:
    spec = StandardDocumentSpec(
        **{
            "name": "declaration",
            "title": "Declaration",
            "body": "# DECLARATION\n\nI, {{ first_name }} {{ last_name | upper }}, live in "
            "{{ city }}.\nRegion: {{ region }}\n\nDate: {{ today }}",
            "optional_fields": ["region"],
            **overrides,
        }
    )
    return StandardDocument(
        name=spec.name,
        title=spec.title,
        kind=spec.kind,
        body=spec.body,
        pdf_data=spec.pdf_data,
        field_map=spec.field_map,
        optional_fields=spec.optional_fields,
        checksum=spec.checksum(),
        version=1,
    )


def extraction(**values: str) -> ExtractionResult:
    result = ExtractionResult()
    for name, value in values.items():
        result.offer(ExtractedField(name=name, value=value, confidence=0.95, source="label"))
    return result


def test_render_text_pdf_escapes_markup_and_supports_unicode(settings):
    data = render_text_pdf("# Title\nȘtefan <b>&amp;</b> Țară", {"title": "T"}, settings)
    text = pdf_text(data)
    assert "Title" in text
    assert "<b>&amp;</b>" in text
    if settings.resolve_font_path():
        assert "Ștefan" in text and "Țară" in text
    assert PdfReader(io.BytesIO(data)).metadata.title == "T"


def test_fill_pdf_form():
    data = fill_pdf_form(make_pdf_form(["txtName", "txtCity"]), {"txtName": "John"})
    fields = PdfReader(io.BytesIO(data)).get_fields()
    assert fields["txtName"]["/V"] == "John"
    assert fields["txtCity"].get("/V") in (None, "")


def test_fill_text_document(settings):
    result = DocFill(settings).fill(
        standard(), extraction(first_name="Ion", last_name="Popescu", city="Cluj-Napoca")
    )
    text = pdf_text(result.pdf)
    assert "I, Ion POPESCU, live in Cluj-Napoca." in text
    assert "Region: ____" in text  # optional field left blank
    assert result.values["first_name"] == "Ion"
    assert result.sources["first_name"] == "label (0.95)"
    assert result.missing == []


def test_missing_required_fields(settings):
    docfill = DocFill(settings)
    with pytest.raises(MissingFieldsError) as error:
        docfill.fill(standard(), extraction(first_name="Ion"))
    assert error.value.missing == ["last_name", "city"]

    result = docfill.fill(standard(), extraction(first_name="Ion"), allow_missing=True)
    assert result.missing == ["last_name", "city"]
    assert "live in ____" in pdf_text(result.pdf)


def test_overrides_and_confidence_threshold(settings):
    weak = extraction(first_name="Ion", last_name="Popescu")
    weak.fields["city"] = ExtractedField(name="city", value="Paris", confidence=0.3, source="ner")
    with pytest.raises(MissingFieldsError):
        DocFill(settings).fill(standard(), weak)
    result = DocFill(settings).fill(standard(), weak, overrides={"city": "Iași"})
    assert result.values["city"] == "Iași"
    assert result.sources["city"] == "manual"


def test_refuses_tampered_standard_document(settings):
    doc = standard()
    doc.body = doc.body + "\nExtra clause"
    with pytest.raises(TemplateIntegrityError):
        DocFill(settings).fill(doc, extraction(first_name="a", last_name="b", city="c"))


def test_fill_pdf_form_standard_document(settings):
    doc = standard(
        kind="pdf_form",
        body=None,
        pdf_data=make_pdf_form(["txtSurname", "txtGiven", "txtCity"]),
        field_map={"txtSurname": "last_name | upper", "txtGiven": "first_name", "txtCity": "city"},
        optional_fields=["city"],
    )
    assert doc.checksum == compute_checksum(
        doc.kind, doc.title, doc.body, doc.pdf_data, doc.field_map, doc.optional_fields
    )
    result = DocFill(settings).fill(doc, extraction(first_name="Ion", last_name="Popescu"))
    fields = PdfReader(io.BytesIO(result.pdf)).get_fields()
    assert fields["txtSurname"]["/V"] == "POPESCU"
    assert fields["txtGiven"]["/V"] == "Ion"
    assert result.missing == []


def test_process_end_to_end_with_several_documents(settings):
    id_doc = make_docx(["Surname: POPESCU", "Given name: Ion"])
    utility_bill = make_docx(["Address: Str. Florilor nr. 5, 400001 Cluj-Napoca, Romania"])
    result, analyses = DocFill(settings).process(
        [("id.docx", id_doc), ("bill.docx", utility_bill)], standard()
    )
    assert len(analyses) == 2
    assert result.values == {
        "first_name": "Ion",
        "last_name": "Popescu",
        "city": "Cluj-Napoca",
        "today": result.values["today"],
    }
    assert "I, Ion POPESCU, live in Cluj-Napoca." in pdf_text(result.pdf)
