import pytest

from docfill.extraction import FieldExtractor, derive_fields, extract_labeled, extract_patterns
from docfill.extraction.text_utils import is_country, parse_address, split_full_name
from docfill.models import ExtractedField, ExtractionResult, SanitizedDocument
from tests.conftest import requires_spacy_model


def values(text: str) -> dict[str, str]:
    return {field.name: field.value for field in extract_labeled(text)}


def extract(text: str, settings) -> ExtractionResult:
    return FieldExtractor(settings).extract(SanitizedDocument(source="t", text=text))


def test_labels_inline_several_per_line():
    found = values("First name: JOHN Last name: SMITH")
    assert found == {"first_name": "John", "last_name": "Smith"}


def test_labels_romanian_with_and_without_diacritics():
    found = values("Nume: POPESCU\nPrenume: Ion\nJudeț: Cluj\nLocul nasterii: Făgăraș")
    assert found == {
        "last_name": "Popescu",
        "first_name": "Ion",
        "region": "Cluj",
        "place_of_birth": "Făgăraș",
    }


def test_labels_after_other_key_value_pairs():
    found = values("Date: 12.05.2024 Name: John Smith; City: Paris")
    assert found == {"full_name": "John Smith", "city": "Paris"}


def test_label_with_note_and_other_separators():
    found = values("Surname (as in passport) | Doe\n2. Given name - Jane")
    assert found == {"last_name": "Doe", "first_name": "Jane"}


def test_label_on_its_own_line():
    found = values("First name\nAnna\nSurname:\nKowalski")
    assert found == {"first_name": "Anna", "last_name": "Kowalski"}


def test_label_on_own_line_followed_by_another_label_is_empty():
    assert values("First name:\nLast name: Smith") == {"last_name": "Smith"}


def test_multi_line_address():
    found = values("Home address:\n742 Evergreen Terrace\nSpringfield, IL 62704\nUSA\n\nOther")
    assert found["full_address"] == "742 Evergreen Terrace, Springfield, IL 62704, USA"


def test_inline_address_with_continuation_lines():
    found = values("Address: 12 Baker Street\nLondon NW1 6XE\nUnited Kingdom\nPhone: 123")
    assert found["full_address"] == "12 Baker Street, London NW1 6XE, United Kingdom"


def test_rejects_implausible_values():
    # Digits in a name, words that are part of a longer label, free text after an empty label.
    found = values("First name: 12345\nCompany name: ACME Ltd\nFirst name\nplease write clearly")
    assert found == {"company_name": "ACME Ltd"}  # a company, never a person's name


def test_postal_code_is_extracted_from_noise():
    assert values("ZIP: 62704 (USA)") == {"postal_code": "62704"}


@pytest.mark.parametrize(
    ("full", "expected"),
    [
        ("John Smith", ("John", "Smith")),
        ("John Michael Smith", ("John Michael", "Smith")),
        ("Smith, John", ("John", "Smith")),
        ("POPESCU Ion Andrei", ("Ion Andrei", "Popescu")),
        ("Ludwig van Beethoven", ("Ludwig", "van Beethoven")),
        ("Madonna", None),
    ],
)
def test_split_full_name(full, expected):
    assert split_full_name(full) == expected


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        (
            "12 Baker Street, London NW1 6XE, United Kingdom",
            {
                "street_address": "12 Baker Street",
                "city": "London",
                "postal_code": "NW1 6XE",
                "country": "United Kingdom",
            },
        ),
        (
            "Str. Florilor nr. 5, 400001 Cluj-Napoca, jud. Cluj, România",
            {
                "street_address": "Str. Florilor nr. 5",
                "postal_code": "400001",
                "city": "Cluj-Napoca",
                "region": "Cluj",
                "country": "România",
            },
        ),
        (
            "742 Evergreen Terrace, Springfield, IL 62704, USA",
            {
                "street_address": "742 Evergreen Terrace",
                "city": "Springfield",
                "region": "IL",
                "postal_code": "62704",
                "country": "USA",
            },
        ),
        (
            "Strada Lunga, nr. 3, Sibiu",
            {"street_address": "Strada Lunga, nr. 3", "city": "Sibiu"},
        ),
    ],
)
def test_parse_address(address, expected):
    assert parse_address(address) == expected


def test_is_country():
    assert is_country("Romania") and is_country("România") and is_country("the Netherlands")
    assert is_country("USA") and is_country("Germany")
    assert not is_country("London") and not is_country("IL")


def test_street_patterns():
    fields = {f.name: f.value for f in extract_patterns("We moved.\nat 221B Baker Street, London")}
    assert fields["street_address"] == "221B Baker Street"
    assert fields["city"] == "London"
    fields = {f.name: f.value for f in extract_patterns("locuiesc pe Str. Mihai Eminescu nr. 7")}
    assert fields["street_address"] == "Str. Mihai Eminescu nr. 7"


def test_labels_win_over_patterns(settings):
    result = extract("Street: 1 Main Road\nLiving at 99 Other Street", settings)
    assert result.fields["street"].value == "1 Main Road"
    assert result.fields["street"].source == "label"


def test_derives_full_name_and_address_parts(settings):
    result = extract(
        "First name: Anna\nLast name: Kowalski\nAddress: Hauptstraße 5, 10115 Berlin", settings
    )
    assert result.fields["full_name"].value == "Anna Kowalski"
    assert result.fields["full_name"].source == "derived"
    assert result.fields["city"].value == "Berlin"
    assert result.fields["postal_code"].value == "10115"


def test_derives_first_and_last_from_full_name(settings):
    result = extract("Name: SMITH John", settings)
    # The source casing is kept; it is what tells the family name apart.
    assert result.values() == {
        "full_name": "SMITH John",
        "first_name": "John",
        "last_name": "Smith",
    }


def test_merge_keeps_most_confident():
    a = ExtractionResult()
    a.offer(ExtractedField(name="city", value="Paris", confidence=0.5, source="ner"))
    b = ExtractionResult()
    b.offer(ExtractedField(name="city", value="Lyon", confidence=0.95, source="label"))
    assert derive_fields(a.merge(b)).fields["city"].value == "Lyon"


@requires_spacy_model
def test_ner_finds_people_and_places_in_free_text(ner_settings):
    text = (
        "Dear Sir,\nMy name is Maria Garcia and I was born in Madrid. "
        "I currently live in Berlin, Germany, at Hauptstraße 5.\nRegards, Maria Garcia"
    )
    result = extract(text, ner_settings)
    found = result.values()
    assert found["first_name"] == "Maria"
    assert found["last_name"] == "Garcia"
    assert found["place_of_birth"] == "Madrid"
    assert found["city"] == "Berlin"
    assert found["country"] == "Germany"
    assert found["full_address"] == "Hauptstraße 5, Berlin, Germany"
    assert result.fields["full_name"].source == "ner"


def test_ner_missing_model_degrades_gracefully(settings):
    extractor = FieldExtractor(settings.model_copy(update={"spacy_model": "no_such_model"}))
    result = extractor.extract(SanitizedDocument(source="t", text="Name: John Smith"))
    assert result.fields["full_name"].value == "John Smith"
    assert not extractor.ner_available
    assert "no_such_model" in extractor.ner.error
