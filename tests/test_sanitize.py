import pytest

from docfill.models import DocumentType, Page, RawDocument
from docfill.sanitize import SanitizeOptions, Sanitizer, dehyphenate, is_noise, sanitize


def doc(*pages: str) -> RawDocument:
    return RawDocument(
        source="test",
        doc_type=DocumentType.PDF,
        pages=[Page(number=i + 1, text=text) for i, text in enumerate(pages)],
    )


def test_fixes_unicode_and_whitespace():
    result = sanitize(doc("CafÃ©   de\tla   gare\x00\n\n\n\nnext"))
    assert result.text == "Café de la gare\n\nnext"


def test_dehyphenates_words_split_across_lines():
    assert dehyphenate(["residential ad-", "dress follows"]) == ["residential address", "follows"]
    # Real hyphenated names are kept.
    assert dehyphenate(["Cluj-", "Napoca"]) == ["Cluj-", "Napoca"]


def test_removes_page_artifacts():
    result = sanitize(doc("Name: John\nPage 1 of 2", "Address: Main St 1\n2"))
    assert "Page 1" not in result.text
    assert result.text.splitlines()[-1] == "Address: Main St 1"


def test_repeated_headers_are_kept_once():
    header = "ACME Insurance - Confidential"
    result = sanitize(doc(f"{header}\nName: John", f"{header}\nCity: Paris", f"{header}\nx y z"))
    assert result.text.count(header) == 1
    assert "City: Paris" in result.text


def test_removes_form_blanks_and_noise():
    result = sanitize(doc("First name: ________\n~~ ,.; ~~\n-----\nCity: Paris"))
    assert result.text == "First name:\nCity: Paris"


@pytest.mark.parametrize(
    ("line", "noise"),
    [("-----", True), ("~ ,.' ~ =", True), ("Name: John", False), ("12", False), ("", False)],
)
def test_is_noise(line, noise):
    assert is_noise(line) is noise


def test_redacts_valid_card_numbers_only():
    result = sanitize(doc("Card: 4111 1111 1111 1111\nRef: 1234 5678 9012 3456"))
    assert "[REDACTED CARD]" in result.text
    assert "1234 5678 9012 3456" in result.text  # fails the Luhn check, not a card
    assert result.redactions == {"credit_card": 1}


def test_redacts_valid_ibans_only():
    result = sanitize(doc("IBAN: RO49 AAAA 1B31 0075 9384 0000\nCode: RO12 ABCD 1234 5678 9012"))
    assert "[REDACTED IBAN]" in result.text
    assert "RO12 ABCD 1234 5678 9012" in result.text
    assert result.redactions == {"iban": 1}


def test_redaction_can_be_disabled():
    sanitizer = Sanitizer(SanitizeOptions(redact=()))
    result = sanitizer.sanitize(doc("Card: 4111 1111 1111 1111"))
    assert "4111 1111 1111 1111" in result.text


def test_unknown_redactor_rejected():
    with pytest.raises(ValueError):
        Sanitizer(SanitizeOptions(redact=("passport",)))
