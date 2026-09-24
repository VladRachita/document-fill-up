import json

import pytest
from pypdf import PdfReader
from typer.testing import CliRunner

from docfill.cli import app
from tests.conftest import make_docx

runner = CliRunner()


@pytest.fixture
def db(tmp_path):
    return ["--db", f"sqlite:///{tmp_path / 'cli.db'}"]


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "id.docx"
    path.write_bytes(
        make_docx(
            [
                "Surname: POPESCU",
                "Given name: Ion",
                "Address: Str. Florilor nr. 5, 400001 Cluj-Napoca, jud. Cluj, Romania",
            ]
        )
    )
    return path


def run(*args: str):
    result = runner.invoke(app, list(args))
    return result


def test_seed_list_show(db):
    assert run(*db, "templates", "seed").exit_code == 0
    listed = run(*db, "templates", "list")
    assert "residence-declaration" in listed.output
    shown = run(*db, "templates", "show", "residence-declaration")
    assert shown.exit_code == 0
    assert "{{ first_name }}" in shown.output
    assert run(*db, "templates", "show", "nope").exit_code == 1


def test_add_and_remove(db, tmp_path):
    spec = tmp_path / "t.yaml"
    spec.write_text("name: mini\ntitle: Mini\nbody: 'Hi {{ first_name }}'\n")
    added = run(*db, "templates", "add", str(spec))
    assert added.exit_code == 0 and "saved as v1" in added.output
    assert "unchanged" in run(*db, "templates", "add", str(spec)).output
    assert run(*db, "templates", "remove", "mini").exit_code == 0


def test_extract_json(db, source):
    result = run(*db, "extract", str(source), "--json", "--no-ner")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["fields"]["last_name"]["value"] == "Popescu"
    assert payload["documents"][0]["type"] == "docx"


def test_fill(db, source, tmp_path):
    run(*db, "templates", "seed")
    output = tmp_path / "out" / "declaration.pdf"
    result = run(
        *db, "fill", str(source), "-t", "residence-declaration", "-o", str(output), "--no-ner"
    )
    assert result.exit_code == 0, result.output
    text = PdfReader(output).pages[0].extract_text()
    assert "Ion POPESCU" in text
    assert "Cluj-Napoca" in text


def test_fill_reports_missing_fields_and_accepts_overrides(db, tmp_path):
    run(*db, "templates", "seed")
    source = tmp_path / "name.docx"
    source.write_bytes(make_docx(["Name: John Smith"]))
    output = tmp_path / "o.pdf"
    failed = run(
        *db, "fill", str(source), "-t", "residence-declaration", "-o", str(output), "--no-ner"
    )
    assert failed.exit_code == 1
    assert "street_address" in failed.output

    filled = run(
        *db,
        "fill",
        str(source),
        "-t",
        "residence-declaration",
        "-o",
        str(output),
        "--no-ner",
        "--set",
        "street_address=1 Main Road",
        "--set",
        "postal_code=10001",
        "--set",
        "city=New York",
        "--set",
        "country=USA",
    )
    assert filled.exit_code == 0, filled.output
    assert "1 Main Road" in PdfReader(output).pages[0].extract_text()


def test_fill_rejects_bad_set_syntax(db):
    assert run(*db, "fill", "-t", "x", "--set", "novalue").exit_code == 1


@pytest.mark.parametrize(
    ("given", "written"),
    [("result.docx", "result.docx.pdf"), ("result", "result.pdf"), ("result.PDF", "result.PDF")],
)
def test_fill_output_is_always_pdf(db, source, tmp_path, given, written):
    run(*db, "templates", "seed")
    result = run(
        *db,
        "fill",
        str(source),
        "-t",
        "residence-declaration",
        "-o",
        str(tmp_path / given),
        "--no-ner",
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / written).read_bytes().startswith(b"%PDF-")
    if written != given:
        assert not (tmp_path / given).exists()
