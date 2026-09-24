import io
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from typer.testing import CliRunner

from docfill.api import create_app
from docfill.cli import app as cli_app
from docfill.models import ExtractedField, ExtractionResult
from docfill.templates.placeholders import preview_lines
from docfill.wizard import (
    build_preview,
    field_rows,
    other_fields,
    safe_filename,
    save_output,
    suggest_filename,
)
from tests.conftest import make_docx, requires_spacy_model
from tests.test_pipeline import standard

TODAY = date.today().isoformat()  # file names
TODAY_RO = date.today().strftime("%d.%m.%Y")  # dates written in documents
TEMPLATE = {
    "name": "hello",
    "title": "Hello",
    "body": "# Hello\n\nHi {{ first_name }} {{ last_name | upper }} from {{ city }}\n---",
    "optional_fields": ["city"],
}


def field(name, value, confidence=0.95, source="label", **extra) -> ExtractedField:
    return ExtractedField(name=name, value=value, confidence=confidence, source=source, **extra)


# --------------------------------------------------------------------------- candidates


def test_extraction_keeps_every_distinct_candidate():
    result = ExtractionResult()
    result.offer(field("city", "Paris", 0.5, "ner"))
    result.offer(field("city", "Lyon", 0.95))
    result.offer(field("city", "PARIS", 0.6, "pattern"))  # same value, another source
    result.offer(field("city", "Paris", 0.55, "ner"))  # same value and source: better one kept
    assert result.fields["city"].value == "Lyon"
    assert [(c.value, c.source, c.confidence) for c in result.candidates["city"]] == [
        ("Lyon", "label", 0.95),
        ("PARIS", "pattern", 0.6),
        ("Paris", "ner", 0.55),
    ]


def test_merge_combines_candidates():
    a, b = ExtractionResult(), ExtractionResult()
    a.offer(field("city", "Paris", 0.5, "ner"))
    b.offer(field("city", "Lyon", 0.95))
    merged = a.merge(b)
    assert merged.fields["city"].value == "Lyon"
    assert {c.value for c in merged.candidates["city"]} == {"Paris", "Lyon"}


# --------------------------------------------------------------------------- preview / rows


def test_preview_lines_mark_filled_and_blank_fields():
    lines = preview_lines("# Title {{ a }}\n\n  Name: {{ a }} {{ b|upper }}\n---", {"a": "x"})
    assert [line["kind"] for line in lines] == ["h1", "blank", "text", "hr"]
    assert lines[0]["segments"] == [
        {"text": "Title "},
        {"text": "x", "field": "a", "filled": True},
    ]
    segments = lines[2]["segments"]
    assert segments[0] == {"text": "  Name: "}
    assert segments[-1]["field"] == "b" and not segments[-1]["filled"]


def test_field_rows_prefill_confident_values_and_offer_alternatives(settings):
    extraction = ExtractionResult()
    extraction.offer(field("first_name", "Ion", evidence="Prenume: Ion", document="id.docx"))
    extraction.offer(field("last_name", "Popescu"))
    extraction.offer(field("city", "Berlin", 0.4, "ner"))  # below min confidence
    extraction.offer(field("country", "Romania"))  # not used by the template
    rows = {row.name: row for row in field_rows(standard(), extraction, settings.min_confidence)}

    assert list(rows) == ["first_name", "last_name", "city", "region", "today"]
    assert rows["first_name"].value == "Ion"
    assert rows["first_name"].found.evidence == "Prenume: Ion"
    assert rows["first_name"].found.document == "id.docx"
    assert rows["city"].value == "" and rows["city"].required
    assert rows["city"].alternatives[0].value == "Berlin"
    assert not rows["region"].required
    assert rows["today"].value == TODAY_RO and rows["today"].found.source == "system"
    assert [f["name"] for f in other_fields(standard(), extraction)] == ["country"]


def test_build_preview_counts_and_missing():
    preview = build_preview(standard(), {"first_name": "Ion", "today": TODAY_RO})
    assert preview["missing"] == ["last_name", "city"]
    assert (preview["filled"], preview["total"]) == (2, 5)
    assert preview["lines"][0] == {"kind": "h1", "segments": [{"text": "DECLARATION"}]}


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Declaration John (final)", "Declaration_John_final.pdf"),
        ("../../etc/passwd", "passwd.pdf"),
        ("C:\\temp\\out.PDF", "out.pdf"),
        ("Ștefan Țară.pdf", "Stefan_Tara.pdf"),
        ("...", "document.pdf"),
    ],
)
def test_safe_filename(name, expected):
    assert safe_filename(name) == expected


def test_suggest_filename():
    values = {"first_name": "Ion Andrei", "last_name": "Popescu"}
    assert suggest_filename(standard(), values) == f"declaration_Popescu_Ion_Andrei_{TODAY}.pdf"


def test_save_output_never_overwrites(tmp_path):
    first = save_output(tmp_path / "out", "doc", b"1")
    second = save_output(tmp_path / "out", "doc.pdf", b"2")
    assert (first.name, second.name) == ("doc.pdf", "doc-2.pdf")
    assert first.read_bytes() == b"1" and second.read_bytes() == b"2"


# --------------------------------------------------------------------------- web API


@pytest.fixture
def client(settings, tmp_path):
    output_settings = settings.model_copy(update={"output_dir": tmp_path / "output"})
    client = TestClient(create_app(output_settings))
    assert client.post("/templates", json=TEMPLATE).status_code == 201
    return client


def test_wizard_page_is_served(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/wizard"
    page = client.get("/wizard")
    assert page.status_code == 200
    assert "docfill wizard" in page.text


def test_wizard_analyze_shows_text_and_rows(client):
    docx = make_docx(["Surname: SMITH", "First name: John", "Card: 4111 1111 1111 1111"])
    response = client.post(
        "/wizard/analyze", data={"template": "hello"}, files={"files": ("id.docx", docx)}
    )
    assert response.status_code == 200
    body = response.json()
    document = body["documents"][0]
    assert "4111 1111 1111 1111" in document["raw_text"]
    assert "[REDACTED CARD]" in document["text"]
    rows = {row["name"]: row for row in body["rows"]}
    assert rows["first_name"]["value"] == "John"
    assert rows["first_name"]["found"]["source"] == "label"
    assert rows["city"]["required"] is False


def test_wizard_reextract_follows_text_corrections(client):
    payload = {"template": "hello", "documents": [{"source": "id", "text": "Surname: SMYTH"}]}
    rows = {r["name"]: r for r in client.post("/wizard/reextract", json=payload).json()["rows"]}
    assert rows["last_name"]["value"] == "Smyth"
    payload["documents"][0]["text"] = "Surname: SMITH\nFirst name: John"
    rows = {r["name"]: r for r in client.post("/wizard/reextract", json=payload).json()["rows"]}
    assert (rows["last_name"]["value"], rows["first_name"]["value"]) == ("Smith", "John")
    # No documents at all: every field is to be typed by hand.
    empty = client.post("/wizard/reextract", json={"template": "hello", "documents": []})
    assert all(row["value"] == "" for row in empty.json()["rows"])


def test_wizard_preview(client):
    body = client.post(
        "/wizard/preview", json={"template": "hello", "values": {"first_name": "Ion"}}
    ).json()
    preview = body["previews"][0]
    assert preview["missing"] == ["last_name"]
    assert body["suggested_filename"] == f"hello_Ion_{TODAY}"
    line = preview["lines"][2]["segments"]
    assert {"text": "Ion", "field": "first_name", "filled": True} in line


def test_wizard_preview_reports_issues_and_derivations(client):
    body = client.post(
        "/wizard/preview",
        json={
            "template": "hello",
            "values": {"cnp": "1871114321239", "date_of_birth": "01.01.1990"},
        },
    ).json()
    assert body["issues"]["date_of_birth"] == ["Differs from the date of birth in the CNP"]
    assert body["derived"]["sex"]["value"] == "M"
    bad = client.post(
        "/wizard/preview", json={"template": "hello", "values": {"cnp": "1871114321230"}}
    )
    assert "Invalid CNP" in bad.json()["issues"]["cnp"][0]


def test_wizard_export_saves_under_new_name_and_learns(client, tmp_path):
    rows = client.post(
        "/wizard/reextract",
        json={
            "template": "hello",
            "documents": [
                {"source": "id", "text": "Surname: SMITH\nFirst name: Jon", "doc_type": "other"}
            ],
        },
    ).json()["rows"]
    payload = {
        "template": "hello",
        "values": {"first_name": "John", "last_name": "Smith", "city": "Iași"},
        "filename": "../John Smith final",
        "rows": rows,
        "documents": [
            {
                "source": "id",
                "text": "Surname: SMITH\nFirst name: Jon",
                "doc_type": "other",
                "detected_type": "other",
            }
        ],
    }
    response = client.post("/wizard/export", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    saved = body["files"][0]
    assert saved["filename"] == "John_Smith_final.pdf"
    assert (tmp_path / "output" / "John_Smith_final.pdf").is_file()
    download = client.get(saved["url"])
    assert download.headers["content-type"] == "application/pdf"
    text = PdfReader(io.BytesIO(download.content)).pages[0].extract_text()
    assert "Hi John SMITH from Iași" in text
    # the review was recorded: last name accepted, first name corrected, city added
    assert body["learned"]["outcomes"] == {"accepted": 1, "corrected": 1, "added": 1}
    stats = client.get("/learning/stats").json()
    assert stats["reviews"] == 1 and stats["examples"] == {"other": 1}
    again = client.post("/wizard/export", json=payload).json()
    assert again["files"][0]["filename"] == "John_Smith_final-2.pdf"


def test_wizard_export_several_documents(client):
    client.post(
        "/templates",
        json={**TEMPLATE, "name": "bye", "title": "Bye", "body": "Bye {{ first_name }}"},
    )
    body = client.post(
        "/wizard/export",
        json={
            "templates": ["hello", "bye"],
            "values": {"first_name": "Ion", "last_name": "Popescu"},
            "filename": "dosar",
        },
    ).json()
    assert [f["filename"] for f in body["files"]] == ["dosar_hello.pdf", "dosar_bye.pdf"]


def test_wizard_download_rejects_other_paths(client):
    assert client.get("/wizard/files/..%2F..%2Fetc%2Fpasswd").status_code == 404
    assert client.get("/wizard/files/missing.pdf").status_code == 404


def test_wizard_export_requires_missing_fields_or_permission(client):
    payload = {"template": "hello", "values": {"first_name": "Ion"}}
    refused = client.post("/wizard/export", json=payload)
    assert refused.status_code == 422 and refused.json()["missing"] == ["last_name"]
    allowed = client.post("/wizard/export", json={**payload, "allow_missing": True})
    assert allowed.status_code == 200
    assert allowed.json()["files"][0]["missing"] == ["last_name"]


def test_wizard_unknown_template(client):
    assert client.post("/wizard/preview", json={"template": "nope"}).status_code == 404


# --------------------------------------------------------------------------- terminal wizard


def test_terminal_wizard(tmp_path):
    db = ["--db", f"sqlite:///{tmp_path / 'w.db'}"]
    spec = tmp_path / "hello.yaml"
    spec.write_text(json.dumps(TEMPLATE))  # JSON is valid YAML
    runner = CliRunner()
    assert runner.invoke(cli_app, [*db, "templates", "add", str(spec)]).exit_code == 0
    source = tmp_path / "id.docx"
    source.write_bytes(make_docx(["Surname: SMITH", "First name: Jon"]))

    answers = "\n".join(
        [
            "n",  # show the text read? no
            "1",  # change field 1 (first name)...
            "John",  # ...correcting the OCR/typing mistake
            "3",  # change field 3 (city, optional)...
            "Cluj",  # ...providing it
            "",  # no more changes
            "",  # everything correct? yes
            "John Smith hello",  # new file name
        ]
    )
    result = runner.invoke(
        cli_app,
        [
            *db,
            "wizard",
            str(source),
            "-t",
            "hello",
            "--output-dir",
            str(tmp_path / "out"),
            "--no-ner",
        ],
        input=answers + "\n",
    )
    assert result.exit_code == 0, result.output
    saved = tmp_path / "out" / "John_Smith_hello.pdf"
    assert "Hi John SMITH from Cluj" in PdfReader(saved).pages[0].extract_text()
    learned = result.output.split("Learned from this review:")[1].splitlines()[0]
    assert all(part in learned for part in ("1 accepted", "1 corrected", "1 added"))


@requires_spacy_model
def test_ner_ignores_entities_spanning_lines(ner_settings):
    from docfill.extraction import FieldExtractor
    from docfill.models import SanitizedDocument

    text = "Given names: John Michael\nPlace of birth: Manchester"
    result = FieldExtractor(ner_settings).extract(SanitizedDocument(source="t", text=text))
    last_names = {candidate.value for candidate in result.candidates.get("last_name", [])}
    assert "Place" not in last_names
