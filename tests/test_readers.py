import pytest

from docfill.errors import DocumentReadError, UnsupportedDocumentError
from docfill.models import DocumentType
from docfill.readers import detect_type, read_bytes, read_document
from tests.conftest import (
    ID_CARD_LINES,
    make_docx,
    make_image,
    make_scanned_pdf,
    make_text_pdf,
    requires_tesseract,
)


def test_detect_type_by_content():
    assert detect_type(make_text_pdf([["hello"]]), "a.pdf") is DocumentType.PDF
    assert detect_type(make_docx(["hello"]), "a.docx") is DocumentType.DOCX
    assert detect_type(make_image(["x"]), "a.png") is DocumentType.IMAGE
    assert detect_type(make_image(["x"], "JPEG"), "a.jpeg") is DocumentType.IMAGE
    # A PNG saved with a .jpg name is still read correctly.
    assert detect_type(make_image(["x"]), "photo.jpg") is DocumentType.IMAGE


@pytest.mark.parametrize(
    ("data", "name"),
    [
        (b"plain text", "notes.txt"),
        (b"random bytes", "a.pdf"),
        (b"\xd0\xcf\x11\xe0legacy", "old.doc"),
        (b"\xd0\xcf\x11\xe0legacy", "old.docx"),
    ],
)
def test_detect_type_rejects_unsupported(data, name):
    with pytest.raises(UnsupportedDocumentError):
        detect_type(data, name)


def test_read_rejects_empty_and_oversized(settings):
    with pytest.raises(DocumentReadError):
        read_bytes(b"", "a.pdf", settings)
    small = settings.model_copy(update={"max_file_size": 10})
    with pytest.raises(DocumentReadError):
        read_bytes(make_text_pdf([["hello"]]), "a.pdf", small)


def test_read_document_missing_file(tmp_path, settings):
    with pytest.raises(DocumentReadError):
        read_document(tmp_path / "missing.pdf", settings)


def test_read_text_pdf(settings):
    data = make_text_pdf([["First name: John", "Last name: Smith"], ["Second page text here"]])
    document = read_bytes(data, "form.pdf", settings)
    assert document.doc_type is DocumentType.PDF
    assert len(document.pages) == 2
    assert "First name: John" in document.pages[0].text
    assert not document.used_ocr


def test_read_docx_paragraphs_and_tables(settings):
    data = make_docx(
        ["Application"],
        table=[["First name", "Ion"], ["Nume", "POPESCU"]],
    )
    text = read_bytes(data, "app.docx", settings).text
    assert "Application" in text
    assert "First name: Ion" in text
    assert "Nume: POPESCU" in text


def test_read_docx_header_table(settings):
    table = [["First name", "Last name", "City"], ["Anna", "Kowalski", "Gdansk"]]
    data = make_docx([], table=table)
    text = read_bytes(data, "people.docx", settings).text
    assert "First name: Anna" in text
    assert "Last name: Kowalski" in text
    assert "City: Gdansk" in text


def test_invalid_pdf_raises(settings):
    with pytest.raises(DocumentReadError):
        read_bytes(b"%PDF-1.4 broken", "broken.pdf", settings)


def test_scanned_pdf_without_ocr_warns(settings, monkeypatch):
    monkeypatch.setattr("docfill.readers.ocr.tesseract_available", lambda *_: False)
    document = read_bytes(make_scanned_pdf(["Surname: SMITH"]), "scan.pdf", settings)
    assert document.warnings and "OCR is unavailable" in document.warnings[0]


@requires_tesseract
def test_read_image_with_ocr(settings, id_card_png):
    document = read_bytes(id_card_png, "id.png", settings)
    assert document.used_ocr
    assert "Surname: SMITH" in document.text
    assert "12 Baker Street" in document.text


@requires_tesseract
def test_read_scanned_pdf_with_ocr(settings):
    document = read_bytes(make_scanned_pdf(ID_CARD_LINES), "scan.pdf", settings)
    assert document.used_ocr
    assert "Given names: John Michael" in document.text
