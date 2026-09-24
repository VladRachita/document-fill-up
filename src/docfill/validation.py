"""Validators and cross-checks that catch wrong values before they reach a document.

Nothing here invents data. Values that fail a hard check (a CNP whose control digit does not
match) lose confidence, so they are offered for review instead of being filled in; values that
two independent sources agree on (printed CNP vs. MRZ date of birth) gain confidence.
"""

from __future__ import annotations

import re
from datetime import date

from docfill.extraction.fields import FIELDS
from docfill.models import ExtractionResult
from docfill.ro import check_cnp, parse_date
from docfill.sanitize import _iban_valid

INVALID_CONFIDENCE = 0.3
CONFIRMED_CONFIDENCE = 0.97


def _letters(text: str) -> str:
    import unicodedata

    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if c.isalpha()).lower()


def cross_check(result: ExtractionResult) -> ExtractionResult:
    fields = result.fields

    def flag(name: str, message: str, cap: float | None = None) -> None:
        field = fields.get(name)
        if field is None:
            return
        if message not in field.issues:
            field.issues.append(message)
        if cap is not None:
            field.confidence = min(field.confidence, cap)

    def confirm(name: str, by: str) -> None:
        field = fields.get(name)
        if field is not None:
            field.confidence = max(field.confidence, CONFIRMED_CONFIDENCE)
            field.evidence = f"{field.evidence or field.value} (confirmed by {by})"

    if cnp := fields.get("cnp"):
        info = check_cnp(cnp.value)
        if not info.valid:
            flag("cnp", f"Invalid CNP: {info.reason}", INVALID_CONFIDENCE)
        else:
            birth = fields.get("date_of_birth")
            if birth and birth.source != "derived":
                parsed = parse_date(birth.value)
                if parsed == info.birth_date:
                    confirm("date_of_birth", "the CNP")
                    confirm("cnp", "the date of birth")
                elif parsed:
                    flag(
                        "date_of_birth",
                        "Differs from the date of birth in the CNP",
                        birth.confidence * 0.6,
                    )
            sex = fields.get("sex")
            if sex and sex.source != "derived" and info.sex and sex.value != info.sex:
                flag("sex", "Differs from the sex encoded in the CNP", sex.confidence * 0.6)

    # printed name vs. machine readable zone (MRZ has no diacritics, hyphens become spaces)
    for name in ("last_name", "first_name"):
        field = fields.get(name)
        mrz = next((c for c in result.candidates.get(name, []) if c.source == "mrz"), None)
        if field is None or mrz is None or field.source == "mrz":
            continue
        if _letters(field.value) == _letters(mrz.value):
            confirm(name, "the MRZ")
        else:
            flag(name, f"Differs from the machine readable zone ({mrz.value})")

    issued, expires = fields.get("id_issue_date"), fields.get("id_expiry_date")
    issued_date = parse_date(issued.value) if issued else None
    expiry_date = parse_date(expires.value) if expires else None
    if issued_date and expiry_date and issued_date >= expiry_date:
        flag("id_issue_date", "Issue date is not before the expiry date")
    if expiry_date and expiry_date < date.today():
        flag("id_expiry_date", "The identity card has expired")
    return result


def validate_values(values: dict[str, str]) -> dict[str, list[str]]:
    """Problems in the values currently entered (shown live in the wizard)."""
    issues: dict[str, list[str]] = {}

    def add(name: str, message: str) -> None:
        issues.setdefault(name, []).append(message)

    for name, value in values.items():
        spec = FIELDS.get(name)
        value = (value or "").strip()
        if not spec or not value:
            continue
        if spec.kind == "date" and not parse_date(value):
            add(name, "Not a valid date (dd.mm.yyyy)")
        elif spec.kind == "email" and not re.fullmatch(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", value):
            add(name, "Not a valid e-mail address")
        elif spec.kind == "iban" and not _iban_valid(value.replace(" ", "").upper()):
            add(name, "IBAN control digits do not match")
        elif spec.kind == "phone" and len(re.sub(r"\D", "", value)) < 6:
            add(name, "Too short for a phone number")
        elif spec.kind == "id_series" and not re.fullmatch(r"[A-Za-z]{1,2}", value):
            add(name, "An identity card series is 2 letters (e.g. AX)")
        elif spec.kind == "id_number" and not re.fullmatch(r"\d{6,7}", value):
            add(name, "A Romanian identity card number has 6-7 digits")
        elif spec.kind == "list" and name == "caen_activities":
            for line in filter(None, (line.strip() for line in value.splitlines())):
                if not re.match(r"\d{4}\b", line):
                    add(name, f"CAEN line should start with a 4-digit class: {line[:30]}")

    if cnp := values.get("cnp", "").strip():
        info = check_cnp(cnp)
        if not info.valid:
            add("cnp", f"Invalid CNP: {info.reason}")
        else:
            birth = parse_date(values.get("date_of_birth", "") or "")
            if birth and info.birth_date and birth != info.birth_date:
                add("date_of_birth", "Differs from the date of birth in the CNP")
            sex = (values.get("sex") or "").strip().upper()
            if sex in ("M", "F") and info.sex and sex != info.sex:
                add("sex", "Differs from the sex encoded in the CNP")
    issued = parse_date(values.get("id_issue_date", "") or "")
    expires = parse_date(values.get("id_expiry_date", "") or "")
    if issued and expires and issued >= expires:
        add("id_issue_date", "Issue date is not before the expiry date")
    if expires and expires < date.today():
        add("id_expiry_date", "The identity card has expired")
    return issues
