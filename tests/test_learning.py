"""The learning loop: every reviewed document changes how the next one is read."""

import pytest

from docfill.app import build_app
from docfill.learning import (
    Found,
    Review,
    ReviewedDocument,
    ReviewedField,
    label_for_value,
    mask_text,
    outcome_of,
)
from docfill.models import ExtractedField, SanitizedDocument


@pytest.fixture
def app(settings):
    return build_app(settings, use_ner=False)


def extract(app, text, doc_type=None):
    return app.docfill.extractor.extract(SanitizedDocument(source="doc", text=text), doc_type)


def test_outcomes():
    found = Found("Popescu", "label", 0.95)
    assert outcome_of(ReviewedField("last_name", "POPESCU", found)) == "accepted"
    assert outcome_of(ReviewedField("last_name", "Ionescu", found)) == "corrected"
    assert outcome_of(ReviewedField("last_name", "", found)) == "cleared"
    assert outcome_of(ReviewedField("last_name", "Popescu", None)) == "added"
    assert outcome_of(ReviewedField("last_name", "", None)) is None


def test_label_for_value_same_line_and_line_above():
    text = "Nume titular ....... POPESCU\nPrenume/Prenom/First name\n\nION"
    assert label_for_value(text, "Popescu") == "nume titular"
    assert label_for_value(text, "Ion") == "prenume/prenom/first name"


def test_no_label_is_learned_from_part_of_a_word_or_glued_text():
    assert label_for_value("Jud.CJ Mun.Cluj-Napoca", "Cluj") is None
    assert label_for_value("Domiciliu: Jud.CJ Mun.Cluj", "Cluj") is None
    assert label_for_value("Company seat: Cluj", "Cluj") == "company seat"


def test_mask_text_removes_values_and_digits():
    masked = mask_text("Nume: POPESCU\nCNP 1871114321239", ["Popescu"])
    assert "popescu" not in masked and "1871114" not in masked and "nume" in masked


def test_new_label_is_learned_from_a_correction(app):
    text = "Titular contract: MARINESCU\nAlt text"
    assert "last_name" not in extract(app, text).fields
    summary = app.learning.record_review(
        Review(
            templates=["t"],
            fields=[ReviewedField("last_name", "Marinescu", None)],
            documents=[ReviewedDocument("doc", text, "other")],
        )
    )
    assert summary.new_labels == ["titular contract → last_name"]
    learned = extract(app, "Titular contract: IONESCU", "other").fields["last_name"]
    assert (learned.value, learned.source) == ("Ionescu", "learned")


def test_calibration_lowers_a_method_that_is_often_wrong(app):
    candidate = ExtractedField(name="city", value="Berlin", confidence=0.6, source="ner")
    assert app.learning.calibrate(candidate) == 0.6
    for _ in range(6):
        app.learning.record_review(
            Review(
                templates=["t"],
                fields=[ReviewedField("city", "Paris", Found("Berlin", "ner", 0.6))],
            )
        )
    assert app.learning.calibrate(candidate) < 0.5  # no longer auto-filled


def test_calibration_raises_a_method_that_is_always_right(app):
    candidate = ExtractedField(name="cnp", value="x", confidence=0.9, source="pattern")
    for _ in range(6):
        app.learning.record_review(
            Review(
                templates=["t"], fields=[ReviewedField("cnp", "123", Found("123", "pattern", 0.9))]
            )
        )
    assert app.learning.calibrate(candidate) > 0.95


def test_spelling_fix_is_learned(app):
    app.learning.record_review(
        Review(
            templates=["t"],
            fields=[ReviewedField("city", "Mun. Săcălaz", Found("Mun. Sacalaz", "label", 0.95))],
        )
    )
    fixed = extract(app, "City: Mun. Sacalaz").fields["city"]
    assert fixed.value == "Mun. Săcălaz" and "spelling learned" in fixed.evidence


def test_confirmed_document_types_train_the_classifier(app):
    text = "RAPORT DE EVALUARE IMOBILIARA evaluator autorizat valoare de piata"
    before = app.docfill.classifier.predict(text)
    for _ in range(3):
        app.learning.record_review(
            Review(templates=["t"], fields=[], documents=[ReviewedDocument("r", text, "id_card")])
        )
    after = app.docfill.classifier.predict(text)
    assert after.scores["id_card"] > before.scores.get("id_card", 0)
    assert app.learning.stats()["examples"] == {"id_card": 3}


def test_remember_fields_and_stats(app):
    app.learning.record_review(
        Review(
            templates=["t"],
            fields=[
                ReviewedField("represented_by", "avocat", None),
                ReviewedField("last_name", "Popescu", Found("Popescu", "label", 0.95)),
            ],
            remember={"represented_by"},
        )
    )
    assert app.learning.remembered() == {"represented_by": "avocat"}
    stats = app.learning.stats()
    assert stats["reviews"] == 1 and stats["remembered_fields"] == 1
    assert stats["accuracy_all"] == 1.0
    app.learning.reset()
    assert app.learning.remembered() == {} and app.learning.stats()["reviews"] == 0
