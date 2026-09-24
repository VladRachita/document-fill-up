import base64
import io
import json

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from docfill.api import create_app
from tests.conftest import make_docx, make_pdf_form

TEMPLATE = {
    "name": "hello",
    "title": "Hello",
    "body": "Hello {{ first_name }} {{ last_name | upper }} from {{ city }}",
    "optional_fields": ["city"],
}


@pytest.fixture
def client(settings):
    return TestClient(create_app(settings))


@pytest.fixture
def docx_file():
    return (
        "id.docx",
        make_docx(["Surname: Smith", "First name: John"]),
        "application/octet-stream",
    )


def test_health_and_fields(client):
    assert client.get("/health").json()["status"] == "ok"
    names = {field["name"] for field in client.get("/fields").json()}
    assert {"first_name", "last_name", "city", "today"} <= names


def test_template_crud(client):
    created = client.post("/templates", json=TEMPLATE)
    assert created.status_code == 201
    assert created.json()["required_fields"] == ["first_name", "last_name"]
    assert client.post("/templates", json=TEMPLATE).json()["changed"] is False
    assert [t["name"] for t in client.get("/templates").json()] == ["hello"]
    assert client.get("/templates/hello").json()["body"] == TEMPLATE["body"]
    assert client.delete("/templates/hello").status_code == 204
    assert client.get("/templates/hello").status_code == 404


def test_template_validation_errors(client):
    assert client.post("/templates", json={**TEMPLATE, "name": "Bad Name"}).status_code == 422
    assert client.post("/templates", json={**TEMPLATE, "body": "{{ x"}).status_code == 422
    bad_pdf = {**TEMPLATE, "kind": "pdf_form", "body": None, "pdf_base64": "@@"}
    assert client.post("/templates", json=bad_pdf).status_code == 422


def test_pdf_form_template(client, docx_file):
    payload = {
        "name": "form",
        "title": "Form",
        "kind": "pdf_form",
        "pdf_base64": base64.b64encode(make_pdf_form(["txtSurname"])).decode(),
        "field_map": {"txtSurname": "last_name|upper"},
    }
    assert client.post("/templates", json=payload).status_code == 201
    response = client.post("/fill", data={"template": "form"}, files={"files": docx_file})
    assert response.status_code == 200
    assert PdfReader(io.BytesIO(response.content)).get_fields()["txtSurname"]["/V"] == "SMITH"


def test_extract(client, docx_file):
    response = client.post("/extract", files={"files": docx_file})
    assert response.status_code == 200
    body = response.json()
    assert body["fields"]["first_name"]["value"] == "John"
    assert body["documents"][0]["type"] == "docx"


def test_fill_returns_pdf(client, docx_file):
    client.post("/templates", json=TEMPLATE)
    response = client.post(
        "/fill",
        data={"template": "hello", "values": json.dumps({"city": "Paris"})},
        files={"files": docx_file},
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    text = PdfReader(io.BytesIO(response.content)).pages[0].extract_text()
    assert "Hello John SMITH from Paris" in text


def test_fill_errors(client, docx_file):
    client.post("/templates", json=TEMPLATE)
    missing = client.post("/fill", data={"template": "hello"})
    assert missing.status_code == 422
    assert missing.json()["missing"] == ["first_name", "last_name"]

    allowed = client.post("/fill", data={"template": "hello", "allow_missing": "true"})
    assert allowed.status_code == 200
    assert allowed.headers["x-docfill-missing"] == "first_name,last_name"

    assert client.post("/fill", data={"template": "nope"}).status_code == 404
    assert client.post("/fill", data={"template": "hello", "values": "[1]"}).status_code == 422
    unsupported = client.post(
        "/fill", data={"template": "hello"}, files={"files": ("a.txt", b"hi", "text/plain")}
    )
    assert unsupported.status_code == 415


def test_upload_size_limit(settings, docx_file):
    client = TestClient(create_app(settings.model_copy(update={"max_file_size": 100})))
    assert client.post("/extract", files={"files": docx_file}).status_code == 413
