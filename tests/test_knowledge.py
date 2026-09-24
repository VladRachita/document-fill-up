"""The Romanian legal knowledge base: specs, rules, versioning, laws, feedback, wizard, CLI."""

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import select, update
from typer.testing import CliRunner

from docfill.api import create_app
from docfill.app import build_app
from docfill.cli import app as cli_app
from docfill.errors import KnowledgeError, KnowledgeNotFoundError
from docfill.knowledge import (
    KnowledgeFile,
    bundled_dir,
    dump_yaml,
    load_directory,
    load_text,
    parse_amount,
)
from docfill.knowledge.checks import run_check
from docfill.knowledge.ingest import html_to_text
from docfill.knowledge.specs import Check
from docfill.knowledge.store import KnowledgeEntry
from docfill.knowledge.texts import article_number, citation_key, split_passages
from docfill.ro import cnp_control_digit
from docfill.templates import bundled_specs_dir
from docfill.templates import load_directory as load_templates
from docfill.wizard import field_rows

ENTITIES = {"srl", "srl-d", "sa", "pfa", "pfi", "ii", "if"}
OPERATIONS = {"infiintare", "modificare", "radiere"}
# Fictitious text shaped like a Romanian law (not an official text).
LAW = """LEGEA DE TEST privind societățile (text fictiv)

Art. 9. - Text de test despre forma societății.

Art. 10. - (1) Text de test: capitalul social minim al unei societăți pe acțiuni.
(2) Al doilea alineat de test.

Art. 12. - Text de test despre numărul maxim de asociați în societatea cu răspundere limitată.

Art. 12^1. - Articol introdus ulterior, text de test.
"""


def cnp(born: date, male: bool = True) -> str:
    first = ("1" if male else "2") if born.year < 2000 else ("5" if male else "6")
    base = f"{first}{born:%y%m%d}12345"
    return base + cnp_control_digit(base)


@pytest.fixture
def context(settings):
    app = build_app(settings, use_ner=False)
    with app.sessions() as session:
        repo = app.repository(session)
        for spec in load_templates(bundled_specs_dir()):
            repo.save(spec)
    return app


@pytest.fixture
def kb(context):
    context.knowledge.seed()
    return context.knowledge


def rule_yaml(key="test-rule", value=100, entities="[srl]", message="Minim 100 lei."):
    return f"""
rules:
  - key: {key}
    title: Regulă de test
    applies_to: {{entities: {entities}, operations: [infiintare]}}
    severity: error
    check: {{type: min_amount, field: share_capital, value: {value}}}
    message: {message}
    legal_basis:
      - {{citation: Legea nr. 31/1990, article: art. 10 alin. (1)}}
"""


# --------------------------------------------------------------------------- bundled knowledge


def test_bundled_knowledge_covers_every_legal_form_and_operation():
    specs = load_directory(bundled_dir())
    procedures = {(s.entity, s.operation) for s in specs if s.kind == "procedure"}
    assert procedures == {(e, o) for e in ENTITIES for o in OPERATIONS}
    assert {s.key for s in specs if s.kind == "entity"} == ENTITIES
    for spec in specs:  # every piece of legal knowledge says where it comes from
        if spec.kind in ("entity", "procedure", "rule"):
            assert spec.legal_basis, spec.key


def test_seed_is_consistent_and_everything_starts_as_draft(context):
    results = context.knowledge.seed()
    assert {r.action for r in results} == {"created"}
    assert {e.status for e in context.knowledge.entries()} == {"draft"}
    _, warnings = context.knowledge.check_references(load_directory(bundled_dir()))
    assert warnings == []  # every form template and field exists
    assert {r.action for r in context.knowledge.seed()} == {"unchanged"}


def test_procedure_view_lists_forms_documents_and_rules(kb):
    view = kb.procedure_view("sa.infiintare")
    assert view["templates"] == ["onrc-anexa-2a", "onrc-anexa-4"]
    assert view["entity"]["abbreviation"] == "SA"
    assert {"sa-capital-minim", "sa-actionari-minim", "firma-sa"} <= {
        r["key"] for r in view["rules"]
    }
    assert any(d["doc_type"] == "act_constitutiv" for d in view["documents"])
    assert "rule/sa-capital-minim" in view["unverified"]
    # forms docfill does not have yet are listed, not hidden
    assert kb.procedure_view("pfa.radiere")["templates"] == []
    cases = kb.cases()
    assert [e["abbreviation"] for e in cases["entities"]] == [
        "SRL",
        "SRL-D",
        "SA",
        "PFA",
        "PFI",
        "II",
        "IF",
    ]
    assert cases["procedures"][0]["key"] == "srl.infiintare"


# --------------------------------------------------------------------------- specs


@pytest.mark.parametrize(
    "check",
    [
        {"type": "min_amount", "field": "share_capital"},  # no value
        {"type": "contains_any", "field": "company_name"},  # no phrases
        {"type": "pattern", "field": "cnp", "pattern": "(["},  # bad regex
        {"type": "required"},  # no fields
        {"type": "contains_field", "field": "company_name"},  # no other
    ],
)
def test_invalid_checks_are_refused(check):
    with pytest.raises(ValidationError):
        Check.model_validate(check)


def test_invalid_knowledge_is_refused_with_a_readable_message():
    with pytest.raises(KnowledgeError, match="rules.0.severity"):
        load_text(rule_yaml().replace("severity: error", "severity: fatal"))
    with pytest.raises(KnowledgeError, match="not valid YAML"):
        load_text("rules: [unclosed")
    with pytest.raises(KnowledgeError, match="at least 8 words"):
        load_text("doc_types:\n  - {key: scurt, title: Scurt, seeds: [prea scurt]}")


def test_dump_and_load_round_trip():
    specs = load_directory(bundled_dir())
    assert load_text(dump_yaml(specs)) == list(KnowledgeFile.of(specs).entries())


# --------------------------------------------------------------------------- checks


@pytest.mark.parametrize(
    ("text", "amount"),
    [
        ("90.000 lei", 90000),
        ("90 000", 90000),
        ("1.500,50 RON", 1500.5),
        ("200", 200),
        ("0,5 lei", 0.5),
        ("capital: 1,000,000", 1000000),
        ("fără sumă", None),
    ],
)
def test_parse_amount(text, amount):
    assert parse_amount(text) == amount


def check(type_, **options):
    return Check.model_validate({"type": type_, **options})


def test_checks():
    today = date(2026, 9, 24)
    values = {
        "share_capital": "50.000 lei",
        "associates": "POPESCU ION | 1 | 50%\nIONESCU ANA | 2 | 50%\n",
        "company_name": "Exemplu Construct S.R.L.",
        "last_name": "Popescu",
        "cnp": cnp(date(2010, 5, 1)),
        "caen_activities": "6201 Software\nsoftware fără cod",
    }

    def status(c):
        return run_check(c, values, today).status

    assert status(check("min_amount", field="share_capital", value=90000)) == "failed"
    assert status(check("max_amount", field="share_capital", value=90000)) == "passed"
    assert status(check("min_count", field="associates", value=2)) == "passed"
    assert status(check("max_count", field="associates", value=1)) == "failed"
    assert status(check("contains_any", field="company_name", any_of=["SRL"])) == "passed"
    assert status(check("contains_any", field="company_name", any_of=["SA"])) == "failed"
    assert status(check("contains_field", field="company_name", other="last_name")) == "failed"
    assert status(check("min_age", field="cnp", value=18)) == "failed"  # 16 years old
    assert status(check("lines_pattern", field="caen_activities", pattern=r"\d{4}\b")) == "failed"
    assert status(check("one_of", field="last_name", any_of=["POPESCU", "Ionescu"])) == "passed"
    assert status(check("pattern", field="last_name", pattern="[A-Z][a-z]+")) == "passed"
    assert status(check("required", fields=["share_capital", "company_cui"])) == "failed"
    # an empty field is not a failure (that is the job of 'required')
    assert status(check("min_amount", field="company_cui", value=1)) == "skipped"
    adult = {"cnp": cnp(date(1987, 11, 14))}
    assert run_check(check("min_age", field="cnp", value=18), adult, today).status == "passed"


# --------------------------------------------------------------------------- versioning


def test_changes_are_versioned_and_need_verification_again(kb):
    [created] = kb.save_all(load_text(rule_yaml()))
    assert (created.action, created.version, created.status) == ("created", 1, "draft")
    assert kb.save_all(load_text(rule_yaml()))[0].action == "unchanged"

    verified = kb.verify("rule", "test-rule", "Av. Test", "checked against the law")
    assert (verified.status, verified.verified_by) == ("verified", "Av. Test")
    # saving the same content again keeps the verification
    assert kb.save_all(load_text(rule_yaml()))[0].status == "verified"

    [changed] = kb.save_all(load_text(rule_yaml(value=200)))
    assert (changed.action, changed.version, changed.status) == ("updated", 2, "draft")
    assert kb.get("rule", "test-rule").verified_by is None

    [signed] = kb.save_all(load_text(rule_yaml(value=300)), verified_by="Av. Test")
    assert (signed.version, signed.status) == (3, "verified")

    retired = kb.retire("rule", "test-rule", "Av. Test", "law changed")
    assert retired.status == "retired"
    assert "test-rule" not in {r.key for r in kb.rules_for(kb.procedure("srl.infiintare"))}
    actions = [h["action"] for h in kb.history("rule", "test-rule")]
    assert actions == ["created", "verified", "updated", "updated", "retired"]
    with pytest.raises(KnowledgeError):
        kb.verify("rule", "test-rule", "Av. Test")
    assert kb.save_all(load_text(rule_yaml(value=300)))[0].action == "restored"


def test_seeding_never_overwrites_local_changes(kb, tmp_path):
    local = load_text(rule_yaml(key="sa-capital-minim", entities="[sa]", message="Local."))
    kb.save_all(local)
    kb.retire("rule", "firma-sa", "Av. Test", "not needed here")
    results = {(r.kind, r.key): r.action for r in kb.seed()}
    assert results[("rule", "sa-capital-minim")] == "kept-local"
    assert results[("rule", "firma-sa")] == "kept-local"
    assert kb.get("rule", "sa-capital-minim").spec.message == "Local."
    assert kb.get("rule", "firma-sa").status == "retired"

    # a changed bundled entry comes back as draft (it must be verified again)
    kb.verify("rule", "srl-asociati-maxim", "Av. Test")
    changed = (bundled_dir() / "rules.yaml").read_text(encoding="utf-8")
    directory = tmp_path / "bundle"
    directory.mkdir()
    (directory / "rules.yaml").write_text(
        changed.replace("value: 50}", "value: 49}"), encoding="utf-8"
    )
    kb.seed(directory)
    entry = kb.get("rule", "srl-asociati-maxim")
    assert (entry.version, entry.status) == (2, "draft")


def test_references_are_checked(kb):
    with pytest.raises(KnowledgeError, match="unknown legal form 'snc'"):
        kb.save_all(load_text(rule_yaml(entities="[snc]")))
    with pytest.raises(KnowledgeError, match="built-in document type"):
        kb.save_all(
            load_text(
                "doc_types:\n  - key: id_card\n    title: X\n"
                "    seeds: [unu doi trei patru cinci șase șapte opt nouă]"
            )
        )
    procedure = """
procedures:
  - key: snc.infiintare
    entity: srl
    operation: infiintare
    title: Test
    forms: [{title: Form, template: not-registered}]
    documents: [{title: Doc, doc_type: nu_exista}]
    legal_basis: [{citation: Legea nr. 31/1990}]
"""
    with pytest.raises(KnowledgeError, match="unknown document type 'nu_exista'"):
        kb.save_all(load_text(procedure))
    errors, warnings = kb.check_references(load_text(procedure.replace("nu_exista", "id_card")))
    assert not errors
    assert any("not-registered" in w for w in warnings)


def test_entries_changed_outside_docfill_are_not_used(kb, context):
    with context.sessions() as session:
        session.execute(
            update(KnowledgeEntry)
            .where(KnowledgeEntry.key == "sa-capital-minim")
            .values(data={**kb.get("rule", "sa-capital-minim").spec.model_dump(), "title": "X"})
        )
        session.commit()
    kb.invalidate()
    assert not kb.find("sa-capital-minim", "rule")
    problems = kb.stats()["integrity_problems"]
    assert problems == [
        {"entry": "rule/sa-capital-minim", "problem": "modified outside docfill (checksum)"}
    ]


def test_resolve(kb):
    assert kb.resolve("rule/firma-sa").kind == "rule"
    assert kb.resolve("srl.infiintare").kind == "procedure"
    with pytest.raises(KnowledgeNotFoundError):
        kb.resolve("nothing")


# --------------------------------------------------------------------------- laws


def test_split_passages_by_article():
    passages = split_passages(LAW)
    assert [p.article for p in passages] == [None, "art. 9", "art. 10", "art. 12", "art. 12^1"]
    assert "(2) Al doilea alineat" in passages[2].text
    long_article = "Art. 1. - " + " ".join(f"({i}) " + "cuvânt " * 150 for i in range(1, 6))
    long_article += "\nArt. 2. - scurt."
    parts = split_passages(long_article.replace(" (", "\n("))
    assert [p.article for p in parts].count("art. 1") > 1
    assert all(len(p.text) <= 3000 for p in parts)
    plain = split_passages("Primul paragraf.\n\nAl doilea paragraf.")
    assert len(plain) == 1 and plain[0].article is None


def test_citations_and_articles():
    assert citation_key("Legea nr. 31/1990 privind societățile") == "31/1990"
    assert citation_key("OUG nr. 44 / 2008") == "44/2008"
    assert article_number("art. 10 alin. (1)") == "10"
    assert article_number("Art. 12^1") == "12^1"
    assert article_number(None) is None


def test_fed_law_is_shown_next_to_the_rules_and_searchable(kb):
    result = kb.ingest_text(LAW, "Legea nr. 31/1990", "Test")
    assert (result["passages"], result["articles"]) == (5, 4)
    [capital] = [
        r
        for r in kb.evaluate("sa.infiintare", {"share_capital": "10"})
        if r.rule == "sa-capital-minim"
    ]
    assert capital.status == "failed"
    assert capital.law_text.startswith("Art. 10.")
    hits = kb.search("numarul maxim de asociati")  # no diacritics
    assert any(h["kind"] == "law" and "Art. 12." in h["text"] for h in hits)
    assert any(h["kind"] == "rule" and h["ref"] == "srl-asociati-maxim" for h in hits)
    # the same citation replaces the text
    kb.ingest_text("Art. 1. - Nou.\nArt. 2. - Tot nou.", "Legea nr. 31/1990")
    assert kb.sources()[0]["passages"] == 2
    kb.remove_source("Legea nr. 31/1990")
    assert kb.sources() == []
    with pytest.raises(KnowledgeError):
        kb.ingest_text("   ", "Legea nr. 1/2000")


def test_html_to_text():
    text = html_to_text(
        "<html><head><title>t</title><script>x()</script></head>"
        "<body><p>Art. 1. - Unu</p><p>Art. 2. - Doi</p></body></html>"
    )
    assert "x()" not in text
    assert [p.article for p in split_passages(text)] == ["art. 1", "art. 2"]


# --------------------------------------------------------------------------- feedback


def test_overridden_rules_are_flagged_and_documents_suggested(kb):
    results = kb.evaluate("sa.infiintare", {"share_capital": "10", "associates": "A"})
    for _ in range(3):
        kb.record_case("sa.infiintare", results, ["id_card", "certificat_inregistrare"])
    stats = kb.stats()
    capital = next(r for r in stats["rules"] if r["rule"] == "sa-capital-minim")
    assert (capital["failed"], capital["overridden"], capital["needs_review"]) == (3, 3, True)
    assert "sa-capital-minim" in stats["rules_to_review"]
    [procedure] = stats["procedures"]
    assert procedure["suggested_documents"] == [
        {"doc_type": "certificat_inregistrare", "cases": 3, "of": 3}
    ]
    kb.reset_feedback()
    assert kb.stats()["rules"] == []


# --------------------------------------------------------------------------- ML


def test_knowledge_document_types_are_learned_by_the_classifier(context):
    classifier = context.docfill.classifier
    assert "act_constitutiv" not in classifier.types()
    context.knowledge.seed()
    assert "act_constitutiv" in classifier.types()
    act = (
        "ACT CONSTITUTIV al societății EXEMPLU S.R.L. Subsemnații asociați am hotărât: "
        "Denumirea societății, sediul social, capitalul social de 500 lei împărțit în părți "
        "sociale, administrarea societății, dizolvarea și lichidarea."
    )
    assert classifier.predict(act).doc_type == "act_constitutiv"
    added = context.learning.add_doc_examples("dovada_sediu", ["contract comodat sediu 12"])
    assert added == 1
    assert ("contract comodat sediu 00", "dovada_sediu") in context.learning.doc_examples()


def test_procedure_fields_are_asked_for(kb, context):
    with context.sessions() as session:
        anexa4 = context.repository(session).get("onrc-anexa-4")
    procedure = kb.procedure("sa.infiintare")
    rows = field_rows(anexa4, None, 0.5, None, procedure.fields, procedure.required_fields)
    by_name = {row.name: row for row in rows}
    assert by_name["share_capital"].required and by_name["associates"].kind == "list"
    assert by_name["share_capital"].group == "company"


# --------------------------------------------------------------------------- API


@pytest.fixture
def client(context, settings, tmp_path):
    context.knowledge.seed()
    return TestClient(create_app(settings.model_copy(update={"output_dir": tmp_path / "out"})))


SA_VALUES = {
    "last_name": "Popescu",
    "first_name": "Ion",
    "cnp": cnp(date(1987, 11, 14)),
    "company_name": "Exemplu SA",
    "share_capital": "50.000 lei",
    "associates": "POPESCU ION | x | 100%",
}


def test_api_cases_and_procedures(client):
    cases = client.get("/knowledge/cases").json()
    assert len(cases["procedures"]) == 21
    assert client.get("/knowledge/procedures/sa.infiintare").json()["entity"]["key"] == "sa"
    assert client.get("/knowledge/procedures/nope").status_code == 404
    names = {d["name"] for d in client.get("/doctypes").json()}
    assert {"id_card", "act_constitutiv", "hotarare_aga"} <= names


def test_wizard_runs_the_legal_checks_and_records_overrides(client):
    request = {"templates": ["onrc-anexa-4"], "procedure": "sa.infiintare"}
    preview = client.post("/wizard/preview", json={**request, "values": SA_VALUES}).json()
    failed = {r["rule"] for r in preview["legal"] if r["status"] == "failed"}
    assert {"sa-capital-minim", "sa-actionari-minim"} <= failed

    export = {**request, "values": SA_VALUES, "allow_missing": True, "filename": "sa"}
    refused = client.post("/wizard/export", json=export)
    assert refused.status_code == 422
    assert "Capitalul social minim" in refused.json()["detail"]

    done = client.post("/wizard/export", json={**export, "legal_acknowledged": True})
    assert done.status_code == 200, done.text
    body = done.json()
    assert "sa-capital-minim" in body["knowledge"]["overridden"]
    assert body["dossier"]["forms"][1]["created"] is True  # Anexa 4
    stats = client.get("/knowledge/stats").json()
    assert next(r for r in stats["rules"] if r["rule"] == "sa-capital-minim")["overridden"] == 1


def test_wizard_requires_the_procedure_fields(client):
    request = {"templates": ["onrc-anexa-4"], "procedure": "sa.infiintare", "filename": "x"}
    values = {k: v for k, v in SA_VALUES.items() if k != "share_capital"}
    refused = client.post("/wizard/export", json={**request, "values": values})
    assert refused.status_code == 422
    assert "share_capital" in refused.json()["missing"]
    rows = client.post("/wizard/reextract", json={**request, "documents": []}).json()["rows"]
    assert {"share_capital", "associates"} <= {r["name"] for r in rows if r["required"]}


def test_api_feed_verify_and_retire(client):
    added = client.post("/knowledge/entries", json={"yaml": rule_yaml(entities="[sa]")})
    assert added.status_code == 200
    assert added.json()["results"][0]["action"] == "created"
    bad = client.post("/knowledge/entries", json={"yaml": rule_yaml(entities="[xyz]")})
    assert bad.status_code == 422
    verified = client.post("/knowledge/entries/rule/test-rule/verify", json={"by": "Av. Test"})
    assert verified.json()["status"] == "verified"
    entry = client.get("/knowledge/entries/rule/test-rule").json()
    assert "min_amount" in entry["yaml"] and len(entry["history"]) == 2
    retired = client.post("/knowledge/entries/rule/test-rule/retire", json={"by": "Av. Test"})
    assert retired.json()["status"] == "retired"
    assert client.get("/knowledge/entries/nokind/x").status_code == 404
    assert "sa-capital-minim" in client.get("/knowledge/export").text


def test_api_laws_search_and_teaching(client):
    uploaded = client.post(
        "/knowledge/texts",
        files={"file": ("lege.txt", LAW.encode("utf-8"), "text/plain")},
        data={"citation": "Legea nr. 31/1990", "title": "Test"},
    )
    assert uploaded.status_code == 200 and uploaded.json()["articles"] == 4
    hits = client.get("/knowledge/search", params={"q": "capitalul social minim"}).json()
    assert hits and hits[0]["score"] > 0
    checks = client.post(
        "/knowledge/check", json={"procedure": "sa.infiintare", "values": SA_VALUES}
    ).json()
    assert next(r for r in checks if r["rule"] == "sa-capital-minim")["law_text"]
    assert (
        client.delete("/knowledge/texts", params={"citation": "Legea nr. 31/1990"}).status_code
        == 204
    )
    assert client.get("/knowledge/texts").json() == []

    example = ("act.txt", b"not a supported file", "text/plain")
    assert (
        client.post("/knowledge/doctypes/nope/examples", files={"files": example}).status_code
        == 404
    )
    from tests.conftest import make_docx

    docx = make_docx(["HOTĂRÂREA ADUNĂRII GENERALE A ASOCIAȚILOR nr. 1 din 01.09.2026"])
    taught = client.post(
        "/knowledge/doctypes/hotarare_aga/examples", files={"files": ("aga.docx", docx)}
    )
    assert taught.json() == {"doc_type": "hotarare_aga", "examples_added": 1}
    assert client.get("/knowledge").status_code == 200


# --------------------------------------------------------------------------- CLI


def test_cli_knowledge(tmp_path):
    runner = CliRunner()
    db = ["--db", f"sqlite:///{tmp_path / 'cli.db'}"]

    def run(*args):
        return runner.invoke(cli_app, [*db, *args])

    assert run("templates", "seed").exit_code == 0
    seeded = run("knowledge", "seed")
    assert seeded.exit_code == 0 and "62 created" in seeded.stdout
    assert "srl.infiintare" in run("knowledge", "list", "--kind", "procedure").stdout
    check = run("knowledge", "check", "sa.infiintare", "-s", "share_capital=100", "--json")
    assert check.exit_code == 1  # an error-level check failed
    assert any(r["rule"] == "sa-capital-minim" for r in json.loads(check.stdout))
    ok = run(
        "knowledge",
        "check",
        "sa.infiintare",
        "-s",
        "share_capital=100000",
        "-s",
        "associates=A\nB",
        "-s",
        "company_name=X SA",
    )
    assert ok.exit_code == 0, ok.stdout

    rule_file = tmp_path / "rule.yaml"
    rule_file.write_text(rule_yaml(), encoding="utf-8")
    added = run("knowledge", "add", str(rule_file), "--verified-by", "Av. Test")
    assert added.exit_code == 0 and "created" in added.stdout
    assert "verified" in run("knowledge", "show", "test-rule", "--history").stdout
    assert run("knowledge", "verify", "firma-sa", "--by", "Av. Test").exit_code == 0
    assert run("knowledge", "retire", "nothing", "--by", "x").exit_code == 1

    law = tmp_path / "lege.txt"
    law.write_text(LAW, encoding="utf-8")
    assert run("knowledge", "ingest", str(law), "--citation", "Legea nr. 31/1990").exit_code == 0
    assert "Legea nr. 31/1990" in run("knowledge", "texts").stdout
    assert "Art. 12" in run("knowledge", "search", "asociati", "societatea").stdout
    exported = tmp_path / "export.yaml"
    assert run("knowledge", "export", str(exported)).exit_code == 0
    assert "test-rule" in exported.read_text(encoding="utf-8")
    assert run("knowledge", "stats").exit_code == 0
    stats = json.loads(run("knowledge", "stats", "--json").stdout)
    assert stats["entries"]["rule"]["verified"] == 2


# --------------------------------------------------------------------------- Anexa 2a variants

COMPANY = {
    "last_name": "Popescu",
    "first_name": "Ion",
    "cnp": cnp(date(1987, 11, 14)),
    "company_name": "Exemplu Soft SRL",
    "company_registration_number": "J12/1234/2020",
    "company_cui": "RO12345678",
    "company_city": "Cluj-Napoca",
    "request_object": "Hotărârea AGA nr. 2/15.09.2026",
}


def _template(context, name):
    with context.sessions() as session:
        return context.repository(session).get(name)


def test_anexa_2a_mentiuni_fills_section_4(context):
    from docfill.export.pdf_form import read_form_values

    template = _template(context, "onrc-anexa-2a-mentiuni")
    values = {
        **COMPANY,
        "new_seat_county": "Sibiu",  # ticks its own box and the 4.1 section box
        "change_activity": "x",
        "capital_change": "majorare",
        "filed_gm_decision": "x",
    }
    pdf = read_form_values(context.docfill.fill(template, None, values, allow_missing=True).pdf)
    ticked = {name for name, value in pdf.items() if name.startswith("CheckBox")}
    # "înscriere mențiuni" (header and section 4), 4.1 + items, capital, 4.2 + AGA decision
    assert {"CheckBox4", "CheckBox14", "CheckBox15", "CheckBox18", "CheckBox24"} <= ticked
    assert {"CheckBox29", "CheckBox54", "CheckBox57"} <= ticked
    assert "CheckBox1" not in ticked  # not an înmatriculare
    assert pdf["CheckBox29_2"] == "/v1"  # capital: majorare
    assert (pdf["Text 49"], pdf["Text 51"], pdf["Text 64"]) == (
        "EXEMPLU SOFT SRL",
        "RO12345678",
        "Sibiu",
    )

    # a filled copy uploaded as a source is read through the matching variant of the form
    filled = context.docfill.fill(template, None, values, allow_missing=True).pdf
    analysis = context.docfill.analyze_bytes(filled, "mentiuni.pdf")
    assert analysis.extraction.fields["company_cui"].value == "RO12345678"
    assert analysis.extraction.fields["company_registration_number"].value == "J12/1234/2020"


def test_anexa_2a_radiere_fills_section_6(context):
    from docfill.export.pdf_form import read_form_values

    template = _template(context, "onrc-anexa-2a-radiere")
    values = {
        **COMPANY,
        "closure_other_reason": "Expirarea duratei",
        "filed_liquidation_statements": "x",
    }
    pdf = read_form_values(context.docfill.fill(template, None, values, allow_missing=True).pdf)
    ticked = {name for name, value in pdf.items() if name.startswith("CheckBox")}
    # "radiere" (header and section 6), persoană juridică (default), motiv "altele", 4.2
    assert {
        "CheckBox6",
        "CheckBox74",
        "CheckBox75",
        "CheckBox81",
        "CheckBox65",
        "CheckBox54",
    } <= ticked
    assert not ticked & {"CheckBox1", "CheckBox4", "CheckBox78"}
    assert (pdf["pg. 3 text 22"], pdf["pg. 3 text 37"]) == ("EXEMPLU SOFT SRL", "Expirarea duratei")


def test_ticks_must_name_existing_pdf_fields():
    from docfill.templates import StandardDocumentSpec, bundled_specs_dir, load_spec

    spec = load_spec(bundled_specs_dir() / "onrc-anexa-2a-radiere.yaml")
    data = spec.model_dump()
    data["ticks"] = {"NoSuchBox": ["closure_by_will"]}
    with pytest.raises(ValidationError, match="unknown PDF fields"):
        StandardDocumentSpec.model_validate(data)


def test_company_changes_and_closing_have_forms(kb):
    modificare = kb.procedure_view("srl.modificare")
    assert modificare["templates"] == ["onrc-anexa-2a-mentiuni"]  # Anexa 4 is optional
    assert any(f["template"] == "onrc-anexa-4" and not f["required"] for f in modificare["forms"])
    assert kb.procedure_view("sa.radiere")["templates"] == ["onrc-anexa-2a-radiere"]
    failed = {r.rule for r in kb.evaluate("srl.modificare", {}) if r.status == "failed"}
    assert "mentiuni-declarate" in failed
    passed = {
        r.rule for r in kb.evaluate("srl.modificare", {"change_seat": "x"}) if r.status == "passed"
    }
    assert "mentiuni-declarate" in passed
    reasons = kb.evaluate("srl.radiere", {"closure_other_reason": "altul"})
    assert next(r for r in reasons if r.rule == "motiv-radiere").status == "passed"


def test_natural_person_forms_link_to_the_official_form(kb):
    forms = kb.procedure_view("pfa.infiintare")["forms"]
    anexa_2b = next(f for f in forms if "Anexa 2b" in f["title"])
    assert not anexa_2b["available"] and anexa_2b["url"].startswith("https://www.onrc.ro/")


def test_pfi_is_registered_at_anaf(kb):
    view = kb.procedure_view("pfi.infiintare")
    assert view["authority"] == "ANAF"
    assert view["entity"]["abbreviation"] == "PFI"
    assert view["templates"] == []  # form 070 is not in docfill yet
    assert "anaf.ro" in view["forms"][0]["url"]
    results = {r.rule: r.status for r in kb.evaluate("pfi.infiintare", {"profession": "Avocat"})}
    assert results["pfi-profesie"] == "passed"
    assert "caen-activitati" not in results  # trade register rules do not apply
    radiere = {r.rule for r in kb.evaluate("pfi.radiere", {})}
    assert "firma-existenta-identificata" not in radiere and "temei-radiere" in radiere


def test_required_any():
    values = {"a": "", "b": "x"}
    assert run_check(check("required_any", fields=["a", "b"]), values).status == "passed"
    assert run_check(check("required_any", fields=["a", "c"]), values).status == "failed"


def test_new_optional_fields_do_not_change_stored_checksums():
    from docfill.knowledge.specs import ProcedureSpec
    from docfill.knowledge.store import spec_checksum

    data = {
        "key": "x.infiintare",
        "entity": "x",
        "operation": "infiintare",
        "title": "X",
        "forms": [{"title": "F"}],
    }
    explicit = {**data, "authority": "ONRC", "forms": [{"title": "F", "url": None}]}
    assert spec_checksum(ProcedureSpec.model_validate(data)) == spec_checksum(
        ProcedureSpec.model_validate(explicit)
    )


def test_wizard_exports_a_change_request(client):
    from docfill.export.pdf_form import read_form_values

    request = {
        "templates": ["onrc-anexa-2a-mentiuni"],
        "procedure": "srl.modificare",
        "values": {**COMPANY, "change_seat": "x"},
        "allow_missing": True,
        "filename": "mentiuni",
    }
    response = client.post("/wizard/export", json=request)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["dossier"]["forms"][0]["created"] is True
    pdf = client.get(body["files"][0]["url"]).content
    values = read_form_values(pdf)
    assert values["CheckBox4"] == "x" and values["CheckBox19"] == "x"


def test_entries_saved_with_the_first_checksum_are_still_intact(kb, context):
    from docfill.knowledge.store import _legacy_checksum

    with context.sessions() as session:
        row = session.scalar(select(KnowledgeEntry).where(KnowledgeEntry.key == "sa-capital-minim"))
        row.checksum = _legacy_checksum(row.kind, row.data)
        session.commit()
    kb.invalidate()
    assert kb.find("sa-capital-minim", "rule")
    assert kb.stats()["integrity_problems"] == []
