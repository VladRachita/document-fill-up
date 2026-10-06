"""Several persons (applicant, shareholders, board members, beneficial owners), the identification
clause of Romanian documents, computed values, line conditions and the new form options."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from docfill.api import create_app
from docfill.app import build_app
from docfill.computed import address_line, expand, expand_required, with_computed
from docfill.extraction.clauses import extract_clauses
from docfill.extraction.derive import complete_values
from docfill.extraction.fields import FIELDS, base_field, split_person
from docfill.knowledge.checks import run_check
from docfill.knowledge.specs import Check
from docfill.models import ExtractedField, ExtractionResult
from docfill.pipeline import DocFill, comb_characters, for_person
from docfill.readers import read_bytes
from docfill.readers.doc import doc_converter
from docfill.samples import Person, sworn_statement_docx, sworn_statement_text
from docfill.templates import TemplateRepository, bundled_specs_dir, load_directory
from docfill.templates.placeholders import choose, render_body, validate_body
from docfill.validation import cross_check, validate_values
from docfill.wizard import assist, field_rows
from tests.test_reference_forms import PERSON_2, document

MURESAN = Person(
    last_name="MUREȘAN",
    first_name="VLAD",
    birth=date(1979, 7, 21),
    birth_county_code="AB",
    birth_locality="Mun.Aiud",
    county_code="AB",
    locality="Mun.Alba Iulia",
    street_line="Str.Mihai Viteazul nr.12 bl.A2 sc.1 et.3 ap.10",
    series="AX",
    number="781245",
    issued_by="SPCLEP Alba Iulia",
    issued=date(2015, 7, 21),
    expires=date(2035, 7, 21),
    serial="088",
)


def values_of(fields: list[ExtractedField]) -> dict[str, str]:
    return {field.name: field.value for field in fields}


# --------------------------------------------------------------------------- clause


def test_sworn_statement_identification_is_read_completely():
    found = values_of(extract_clauses("\n".join(sworn_statement_text(MURESAN))))
    assert found == {
        "last_name": "Mureșan",
        "first_name": "Vlad",
        "cnp": MURESAN.cnp,
        "full_address": "Mun. Alba Iulia, Str. Mihai Viteazul nr. 12, bl. A2, sc. 1, et. 3, "
        "ap. 10, jud. Alba",
        "city": "Mun. Alba Iulia",
        "street": "Mihai Viteazul",
        "street_number": "12",
        "building": "A2",
        "entrance": "1",
        "floor": "3",
        "apartment": "10",
        "region": "Alba",
        "country": "România",
        "citizenship": "Română",
        "place_of_birth": "Mun. Aiud",
        "birth_county": "Alba",
        "birth_country": "România",
        "date_of_birth": "21.07.1979",
        "id_type": "CI",
        "id_series": "AX",
        "id_number": "781245",
        "id_issued_by": "SPCLEP Alba Iulia",
        "id_issue_date": "21.07.2015",
        "id_expiry_date": "21.07.2035",
        "capacity": "administrator",
        "company_name": "EXEMPLU INVEST S.A.",
    }


def test_clause_in_the_wording_of_the_articles_of_incorporation():
    text = (
        "D-nul/d-na POPESCU ION-ANDREI, cetăţenie română, născut(ă) la data de 14.11.1987, în "
        "Mun. Sibiu, jud. Sibiu, domiciliat(ă) în Mun. Cluj-Napoca, str. Florilor, nr. 5, "
        "judeţ/sector 3, posesor(e) al CI, seria AX nr. 123456, eliberat de SPCLEP Cluj-Napoca, "
        f"la data de 22.06.2022, CNP {Person().cnp},\n"
    )
    found = values_of(extract_clauses(text))
    assert (found["last_name"], found["first_name"]) == ("Popescu", "Ion-Andrei")
    assert (found["date_of_birth"], found["place_of_birth"]) == ("14.11.1987", "Mun. Sibiu")
    assert (found["city"], found["street"], found["region"]) == (
        "Mun. Cluj-Napoca",
        "Florilor",
        "Sector 3",
    )
    assert (found["id_series"], found["id_number"], found["id_issue_date"]) == (
        "AX",
        "123456",
        "22.06.2022",
    )
    assert found["citizenship"] == "Română"


def test_no_clause_without_identification():
    assert extract_clauses("CNP 1871114321239") == []
    assert extract_clauses("Contract de închiriere între părți.") == []
    # identity cards have their own extractor
    assert extract_clauses("\n".join(sworn_statement_text()), doc_type="id_card") == []


def test_sworn_statement_through_the_pipeline(settings):
    analysis = DocFill(settings).analyze_bytes(sworn_statement_docx(MURESAN), "declaratie.docx")
    fields = analysis.extraction.fields
    assert (fields["last_name"].value, fields["first_name"].value) == ("Mureșan", "Vlad")
    assert fields["id_number"].value == "781245"
    assert fields["city"].value == "Mun. Alba Iulia"  # not the place of birth
    # the identity card number is not a postal code
    postal = fields.get("postal_code")
    assert postal is None or postal.confidence < settings.min_confidence


def test_nume_si_prenume_label_is_family_name_first(settings):
    text = "Data: 25.09.2026\nNume şi prenume: POPESCU ION\nSemnătura:"
    result = DocFill(settings).extract_texts([("d.txt", text, "other")])
    assert (result.fields["last_name"].value, result.fields["first_name"].value) == (
        "Popescu",
        "Ion",
    )


# --------------------------------------------------------------------------- persons


def test_person_fields():
    assert split_person("p2_cnp") == (2, "cnp")
    assert split_person("cnp") == (1, "cnp")
    assert split_person("company_name") == (0, "company_name")
    assert base_field("p3_shares") == "shares"
    assert FIELDS["p2_cnp"].kind == "cnp" and FIELDS["p2_cnp"].synonyms == ()
    assert FIELDS["p3_board_role"].options == (
        "președinte",
        "membru",
        "administrator unic",
        "administrator",
    )


def test_for_person_renames_person_fields_only():
    result = ExtractionResult()
    for name, value in (("cnp", "1"), ("company_name", "X SA"), ("capacity", "administrator")):
        result.offer(ExtractedField(name=name, value=value, confidence=0.9, source="label"))
    renamed = for_person(result, 2)
    assert set(renamed.fields) == {"p2_cnp", "company_name"}  # capacity is the applicant's
    assert renamed.fields["p2_cnp"].name == "p2_cnp"
    assert for_person(result, 1) is result


def test_each_person_document_is_a_person_and_the_same_cnp_the_same_person(settings):
    docfill = DocFill(settings)
    statements = [
        docfill.analyze_bytes(
            sworn_statement_docx(person), f"{name}.docx", "declaratie_administrator"
        )
        for name, person in (("popescu", Person()), ("muresan", MURESAN), ("ionescu", PERSON_2))
    ]
    again = docfill.analyze_bytes(sworn_statement_docx(MURESAN), "muresan-2.docx", "other")
    combined = docfill.combine([*statements, again])
    assert [a.person for a in [*statements, again]] == [1, 2, 3, 2]
    assert combined.fields["last_name"].value == "Popescu"
    assert combined.fields["p2_last_name"].value == "Mureșan"
    assert combined.fields["p3_cnp"].value == PERSON_2.cnp
    assert combined.fields["p2_date_of_birth"].value == "21.07.1979"


def test_extract_texts_for_a_chosen_person(settings):
    text = "\n".join(sworn_statement_text(MURESAN))
    result = DocFill(settings).extract_texts([("m.docx", text, "declaratie_administrator", 3)])
    assert result.fields["p3_cnp"].value == MURESAN.cnp
    assert "cnp" not in result.fields


def test_derivations_and_checks_run_for_every_person():
    derived = complete_values({"p2_cnp": PERSON_2.cnp, "cnp": Person().cnp})
    assert derived["p2_date_of_birth"][0] == "08.03.1994"
    assert derived["p2_sex"][0] == "F"
    assert derived["date_of_birth"][0] == "14.11.1987"
    issues = validate_values({"p3_cnp": "1234567890123", "p2_id_expiry_date": "01.01.2020"})
    assert "CNP invalid" in issues["p3_cnp"][0]
    assert issues["p2_id_expiry_date"] == ["Cartea de identitate a expirat"]
    result = ExtractionResult()
    result.offer(
        ExtractedField(name="p2_cnp", value="1234567890123", confidence=0.9, source="label")
    )
    assert cross_check(result).fields["p2_cnp"].confidence < 0.5


def test_roles_derive_associates_administration_and_filer():
    values = {
        "last_name": "Popescu",
        "first_name": "Ion",
        "cnp": Person().cnp,
        "shares": "450",
        "p2_last_name": "Ionescu",
        "p2_first_name": "Maria",
        "p2_shares": "450",
        "share_count": "900",
        "board_role": "administrator unic",
        "beneficial_owner": "art. 4 alin. (2) lit. a) pct. 1",
    }
    derived = complete_values(values)
    assert derived["associates"][0] == (
        f"POPESCU ION | {Person().cnp} | 450 acțiuni (50%)\nIONESCU MARIA | 450 acțiuni (50%)"
    )
    assert derived["administration"][0] == "administrator unic"
    assert derived["bo_filed_by"][0] == "reprezentantul legal"
    values["p2_board_role"] = "membru"
    values["filer_last_name"] = "Avocat"
    derived = complete_values(values)
    assert derived["administration"][0] == "consiliu de administrație"
    assert derived["bo_filed_by"][0] == "împuternicit"


def test_derived_values_follow_their_source():
    first = assist({"cnp": Person().cnp})["derived"]["date_of_birth"]["value"]
    # the CNP was corrected: the date of birth derived earlier is derived again
    corrected = assist({"cnp": PERSON_2.cnp, "date_of_birth": first}, derived=["date_of_birth"])
    assert corrected["derived"]["date_of_birth"]["value"] == "08.03.1994"
    typed = assist({"cnp": PERSON_2.cnp, "date_of_birth": first})
    assert "date_of_birth" not in typed["derived"]  # typed by the user: kept


# --------------------------------------------------------------------------- computed values


def test_address_line():
    assert address_line(
        {"city": "Mun. Cluj-Napoca", "street": "Florilor", "street_number": "5", "region": "Cluj"}
    ) == ("Mun. Cluj-Napoca, Str. Florilor nr. 5, jud. Cluj")
    assert address_line(
        {"p2_city": "Mun. București", "p2_street": "Bd. Unirii", "p2_region": "Sector 3"}, "p2_"
    ) == ("Mun. București, Bd. Unirii, Sector 3")
    assert address_line({}) is None


def test_computed_values():
    values = with_computed(
        {
            "share_capital": "90.000 lei",
            "share_count": "900",
            "shares": "300",
            "caen_activities": "6201 Software\n6202 Consultanță",
        }
    )
    assert (values["share_value"], values["share_capital_amount"]) == ("100", "90.000")
    assert (values["shares_value"], values["shares_percent"]) == ("30.000", "33,33")
    assert (values["main_activity"], values["main_caen_group"]) == ("6201 - Software", "620")
    assert values["secondary_activities"] == "clasa CAEN 6202 - Consultanță"
    assert "p2_shares_value" not in values  # nothing to compute from
    assert expand(["domicile_line", "cnp"])[:2] == ["city", "street"]
    assert expand_required(["company_seat_line"]) == [
        "company_city",
        "company_street",
        "company_street_number",
        "company_county",
    ]


# --------------------------------------------------------------------------- templates


def test_line_conditions():
    body = (
        "[[shares]] {{ last_name }} holds {{ shares }}\n"
        "[[p2_shares]] {{ p2_last_name }}\n"
        "[[a|b]] any\n"
        "[[administration=Administrator Unic]] sole\n"
        "[[!administration=administrator unic]] board\n"
        "always"
    )
    assert validate_body(body) == [
        "shares",
        "last_name",
        "p2_shares",
        "p2_last_name",
        "a",
        "b",
        "administration",
    ]
    rendered = render_body(
        body, {"shares": "10", "last_name": "Pop", "b": "x", "administration": "administrator unic"}
    )
    assert rendered == "Pop holds 10\nany\nsole\nalways"
    for bad in ("[[x", "[[a|b=c]] text"):
        with pytest.raises(Exception, match="condition"):
            validate_body(bad)


def test_choose_matches_a_single_contained_option():
    options = {"4.2 depășirea plafonului": "A", "4.3 prin opțiune": "B"}
    assert choose(options, "prin optiune") == "B"
    assert choose(options, "4.2") == "A"
    assert choose({"lit. a) pct. 1": "A", "lit. d) pct. 1": "D"}, "pct. 1") is None  # ambiguous


def test_comb_characters():
    date_spec = {"boxes": list("12345678"), "format": "date"}
    assert comb_characters("1.11.2026", date_spec) == list("01112026")
    amount = {"boxes": list("12345678"), "format": "amount", "align": "right"}
    assert "".join(comb_characters("250.000 lei", amount)) == "  250000"
    assert comb_characters("123.456.789 lei", amount) is None  # does not fit: left empty
    assert comb_characters("", amount) is None


def test_wizard_rows_ask_for_inputs_of_computed_values():
    rows = field_rows([document("act-constitutiv-sa")], None, 0.5)
    names = [row.name for row in rows]
    assert "domicile_line" not in names and "city" in names and "p3_street" in names
    assert len(names) == len(set(names))
    by_name = {row.name: row for row in rows}
    assert by_name["administration"].choices == ["consiliu de administrație", "administrator unic"]
    assert by_name["company_city"].required and not by_name["p2_city"].required
    assert by_name["share_form"].value == "nominative"  # default of the model


def test_procedure_fields_are_not_repeated():
    rows = field_rows(
        [document("onrc-anexa-4")],
        None,
        0.5,
        extra_fields=["share_capital", "associates"],
        extra_required=["share_capital", "associates"],
    )
    names = [row.name for row in rows]
    assert names.count("share_capital") == 1 and names.count("associates") == 1


# --------------------------------------------------------------------------- rules


def test_check_when():
    check = Check(type="required", fields=["p2_cnp"], when=["p2_shares", "p2_board_role"])
    assert run_check(check, {}).status == "skipped"
    assert run_check(check, {"p2_board_role": "membru"}).status == "failed"
    assert run_check(check, {"p2_board_role": "membru", "p2_cnp": "1"}).status == "passed"


# --------------------------------------------------------------------------- .doc


def test_reads_legacy_word_documents(settings):
    if doc_converter() is None:
        pytest.skip("antiword or LibreOffice is needed to read .doc files")
    path = bundled_specs_dir() / "model-act-constitutiv-sa-sistem-unitar.doc"
    raw = read_bytes(path.read_bytes(), path.name, settings)
    assert "ACTUL CONSTITUTIV AL SOCIETĂŢII" in raw.text
    assert "Art.13.1." in " ".join(raw.text.split())


# --------------------------------------------------------------------------- wizard API


def test_wizard_assigns_documents_to_persons(settings, tmp_path):
    context = build_app(settings, use_ner=False)
    with context.sessions() as session:
        repository = TemplateRepository(session)
        for spec in load_directory(bundled_specs_dir()):
            repository.save(spec)
    settings = settings.model_copy(update={"output_dir": tmp_path / "out"})
    with TestClient(create_app(settings)) as client:
        files = [
            ("files", ("popescu.docx", sworn_statement_docx(Person()), "application/octet-stream")),
            ("files", ("muresan.docx", sworn_statement_docx(MURESAN), "application/octet-stream")),
        ]
        response = client.post(
            "/wizard/analyze",
            files=files,
            data={"templates": ["onrc-declaratie-beneficiari-reali"]},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert [d["person"] for d in body["documents"]] == [1, 2]
        rows = {row["name"]: row for row in body["rows"]}
        assert rows["last_name"]["value"] == "Popescu"
        assert rows["p2_last_name"]["value"] == "Mureșan"
        assert rows["p2_cnp"]["value"] == MURESAN.cnp
        # the person of a document can be changed: its fields move
        documents = [
            {"source": d["source"], "text": d["text"], "doc_type": d["doc_type"], "person": p}
            for d, p in zip(body["documents"], (1, 3), strict=True)
        ]
        again = client.post(
            "/wizard/reextract",
            json={"templates": ["onrc-declaratie-beneficiari-reali"], "documents": documents},
        ).json()
        rows = {row["name"]: row for row in again["rows"]}
        assert rows["p3_last_name"]["value"] == "Mureșan" and not rows["p2_last_name"]["value"]
