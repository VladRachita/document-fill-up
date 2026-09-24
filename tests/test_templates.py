import pytest
from pydantic import ValidationError
from sqlalchemy import update

from docfill.errors import TemplateError, TemplateIntegrityError, TemplateNotFoundError
from docfill.templates import (
    StandardDocument,
    StandardDocumentSpec,
    TemplateRepository,
    bundled_specs_dir,
    load_directory,
    load_spec,
    make_session_factory,
    render_body,
    validate_body,
)
from docfill.templates.placeholders import clean_fill_value
from tests.conftest import make_pdf_form


@pytest.fixture
def repo(settings):
    session = make_session_factory(settings.database_url)()
    yield TemplateRepository(session)
    session.close()


def text_spec(**overrides) -> StandardDocumentSpec:
    data = {"name": "greeting", "title": "Greeting", "body": "Hello {{ first_name }}!"}
    data.update(overrides)
    return StandardDocumentSpec(**data)


def test_validate_body_lists_fields_in_order():
    body = "{{ last_name | upper }}, {{first_name}} - {{ last_name }}"
    assert validate_body(body) == ["last_name", "first_name"]


@pytest.mark.parametrize("body", ["Hi {{ name", "Hi {{ name | shout }}", "Hi {{ 1abc }}"])
def test_validate_body_rejects_bad_placeholders(body):
    with pytest.raises(TemplateError):
        validate_body(body)


def test_render_body_is_verbatim_except_placeholders():
    body = "# Title\n  Name: {{ first_name }} {{ last_name|upper }}\nCity: {{ city }} <&>"
    rendered = render_body(body, {"first_name": "Ion", "last_name": "Popescu"}, blank="___")
    assert rendered == "# Title\n  Name: Ion POPESCU\nCity: ___ <&>"


def test_values_are_not_reinterpreted_as_placeholders():
    assert render_body("{{ a }}", {"a": "{{ b }}", "b": "x"}) == "{{ b }}"


def test_clean_fill_value():
    assert clean_fill_value("  John\n\tSmith\x00 ") == "John Smith"
    assert len(clean_fill_value("x" * 1000)) == 300


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": "Bad Name"},
        {"body": ""},
        {"body": "Hi {{ oops"},
        {"field_map": {"a": "b"}},
        {"kind": "pdf_form"},
    ],
)
def test_spec_validation(overrides):
    with pytest.raises(ValidationError):
        text_spec(**overrides)


def test_pdf_form_spec_defaults_to_identity_mapping():
    spec = StandardDocumentSpec(
        name="form", title="Form", kind="pdf_form", pdf_data=make_pdf_form(["first_name", "city"])
    )
    assert spec.field_map == {"first_name": "first_name", "city": "city"}


def test_pdf_form_spec_rejects_unknown_pdf_fields():
    with pytest.raises(ValidationError):
        StandardDocumentSpec(
            name="form",
            title="Form",
            kind="pdf_form",
            pdf_data=make_pdf_form(["txtName"]),
            field_map={"txtOther": "first_name"},
        )


def test_repository_versions_and_noop(repo):
    doc, changed = repo.save(text_spec())
    assert changed and doc.version == 1
    _, changed = repo.save(text_spec())
    assert not changed
    doc, changed = repo.save(text_spec(body="Hi {{ first_name }} {{ last_name }}"))
    assert changed and doc.version == 2
    assert repo.get("greeting").required_fields() == ["first_name", "last_name"]
    assert [d.name for d in repo.list()] == ["greeting"]


def test_repository_delete_and_not_found(repo):
    repo.save(text_spec())
    repo.delete("greeting")
    with pytest.raises(TemplateNotFoundError):
        repo.get("greeting")


def test_optional_fields(repo):
    doc, _ = repo.save(text_spec(body="{{ first_name }} {{ region }}", optional_fields=["region"]))
    assert doc.field_names() == ["first_name", "region"]
    assert doc.required_fields() == ["first_name"]


def test_integrity_check_detects_tampering(repo):
    doc, _ = repo.save(text_spec())
    doc.verify_integrity()
    repo.session.execute(
        update(StandardDocument).where(StandardDocument.id == doc.id).values(body="Changed!")
    )
    repo.session.commit()
    repo.session.expire_all()
    with pytest.raises(TemplateIntegrityError):
        repo.get("greeting").verify_integrity()


def test_load_spec_with_pdf_file(tmp_path):
    (tmp_path / "form.pdf").write_bytes(make_pdf_form(["txtSurname"]))
    (tmp_path / "form.yaml").write_text(
        "name: my-form\ntitle: My form\nkind: pdf_form\npdf_file: form.pdf\n"
        "field_map:\n  txtSurname: last_name | upper\n"
    )
    spec = load_spec(tmp_path / "form.yaml")
    assert spec.pdf_data and spec.field_map == {"txtSurname": "last_name | upper"}


def test_load_spec_errors(tmp_path):
    (tmp_path / "bad.yaml").write_text("- just\n- a list\n")
    with pytest.raises(TemplateError):
        load_spec(tmp_path / "bad.yaml")
    (tmp_path / "invalid.yaml").write_text("name: x y\ntitle: t\nbody: b\n")
    with pytest.raises(TemplateError):
        load_spec(tmp_path / "invalid.yaml")


def test_bundled_standard_documents_are_valid():
    specs = load_directory(bundled_specs_dir())
    assert {spec.name for spec in specs} >= {"residence-declaration", "personal-data-sheet"}
