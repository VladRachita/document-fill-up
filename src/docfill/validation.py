"""Validators and cross-checks that catch wrong values before they reach a document.

Nothing here invents data. Values that fail a hard check (a CNP whose control digit does not
match) lose confidence, so they are offered for review instead of being filled in; values that
two independent sources agree on (printed CNP vs. MRZ date of birth) gain confidence.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date

from docfill import names
from docfill.extraction.fields import FIELDS, PERSON_PREFIXES
from docfill.extraction.idcard import CORRECTED, NAME_NOTES, SUSPECT_CONFIDENCE
from docfill.extraction.patterns import MRZ_NAMES_TRUSTED
from docfill.models import ExtractedField, ExtractionResult
from docfill.ro import check_cnp, parse_date
from docfill.sanitize import _iban_valid

INVALID_CONFIDENCE = 0.3
CONFIRMED_CONFIDENCE = 0.97
SETTLED_CONFIDENCE = 0.85  # a name the list of Romanian names decided between two readings
# The "<" fillers of the zone, as OCR reads them when it is unsure (lower-case, accents removed).
_FILLER_LETTERS = set("ckelsx")


_DIGITS_AS_LETTERS = str.maketrans("01256", "oizsg")
_LOOKALIKES = str.maketrans("lq", "io")  # I read as l, O read as Q: same name for the comparison


def _plain(text: str) -> str:
    """The letters of a name: no accents, hyphens or spaces, lower case; digits that OCR read
    in place of letters (``Liv1U``) are the letters they stand for."""
    import unicodedata

    decomposed = unicodedata.normalize("NFD", text).lower().translate(_DIGITS_AS_LETTERS)
    return "".join(c for c in decomposed if c.isalpha())


def _key(plain: str) -> str:
    """:func:`_plain` with the letters OCR mixes up merged (same length, so positions agree)."""
    return plain.translate(_LOOKALIKES)


def _agrees(printed: str, zone: str) -> bool:
    """Whether a printed name and the name of the machine readable zone are the same one: the
    zone has no diacritics or hyphens, OCR mixes up some of its letters and reads its ``<``
    fillers as letters after the name."""
    letters, in_zone = _plain(printed), _plain(zone)
    key_printed, key_zone = _key(letters), _key(in_zone)
    rest = in_zone[len(letters) :] if key_zone.startswith(key_printed) else None
    return key_printed.startswith(key_zone) or (rest is not None and set(rest) <= _FILLER_LETTERS)


def cross_check(result: ExtractionResult) -> ExtractionResult:
    """Check each person's CNP, date of birth, sex, names (against the MRZ) and identity card."""
    for prefix in PERSON_PREFIXES:
        _check_person(result, prefix)
    return result


def _check_person(result: ExtractionResult, prefix: str) -> None:
    fields = {
        name[len(prefix) :]: found
        for name, found in result.fields.items()
        if name.startswith(prefix) and (prefix or not name.startswith(PERSON_PREFIXES[1:]))
    }

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
            note = f"(confirmat prin {by})"
            if not (field.evidence or "").endswith(note):
                field.evidence = f"{field.evidence or field.value} {note}"
            field.issues = [issue for issue in field.issues if not issue.startswith(NAME_NOTES)]
            field.original = None  # the second reading agrees: no guess left to overrule

    if cnp := fields.get("cnp"):
        info = check_cnp(cnp.value)
        if not info.valid:
            flag("cnp", f"CNP invalid: {info.reason}", INVALID_CONFIDENCE)
        else:
            birth = fields.get("date_of_birth")
            if birth and birth.source != "derived":
                parsed = parse_date(birth.value)
                if parsed == info.birth_date:
                    confirm("date_of_birth", "CNP")
                    confirm("cnp", "data nașterii")
                elif parsed:
                    flag(
                        "date_of_birth",
                        "Diferă de data nașterii din CNP",
                        birth.confidence * 0.6,
                    )
            sex = fields.get("sex")
            if sex and sex.source != "derived" and info.sex and sex.value != info.sex:
                flag("sex", "Diferă de sexul codificat în CNP", sex.confidence * 0.6)

    # the CNP printed on the card vs. the one carried by the machine readable zone: two sources
    # that fail independently, so agreement is strong evidence and a difference needs a person
    cnp = fields.get("cnp")
    pool = result.candidates.get(prefix + "cnp", [])
    from_mrz = next((c for c in pool if c.source == "mrz"), None)
    if cnp is not None and from_mrz is not None and cnp.source != "mrz":
        if cnp.value == from_mrz.value:
            confirm("cnp", "zona citibilă automat")
        else:
            message = f"Diferă de CNP-ul din zona citibilă automat ({from_mrz.value})"
            flag("cnp", message, SUSPECT_CONFIDENCE)

    # printed name vs. machine readable zone (no diacritics, hyphens become spaces, "<" fillers
    # sometimes read as letters)
    for name in ("last_name", "first_name"):
        _reconcile_name(result, prefix, name, fields, flag, confirm)

    # the sex a first name is given to against the sex of the person
    first, sex = fields.get("first_name"), fields.get("sex")
    if first and sex and sex.value in ("M", "F") and sex.confidence >= SUSPECT_CONFIDENCE:
        word = next(iter(re.findall(r"[^\W\d_]+", first.value)), "")
        given = names.given_names().section(word)
        if given in ("M", "F") and given != sex.value:
            usual = "de femeie" if given == "F" else "de bărbat"
            flag(
                "first_name",
                f"{word} este de obicei prenume {usual}, dar CNP-ul / cartea de identitate indică "
                f"sexul {'feminin' if sex.value == 'F' else 'masculin'}: verificați prenumele și "
                "CNP-ul",
            )

    issued, expires = fields.get("id_issue_date"), fields.get("id_expiry_date")
    issued_date = parse_date(issued.value) if issued else None
    expiry_date = parse_date(expires.value) if expires else None
    if issued_date and expiry_date and issued_date >= expiry_date:
        flag("id_issue_date", "Data eliberării nu este anterioară datei de expirare")
    if expiry_date and expiry_date < date.today():
        flag("id_expiry_date", "Cartea de identitate a expirat")

    # six digits read as a postal code that are in fact the identity card number
    postal, number = fields.get("postal_code"), fields.get("id_number")
    if postal and number and postal.value.strip() == number.value.strip():
        flag("postal_code", "Aceleași cifre ca numărul cărții de identitate", INVALID_CONFIDENCE)


def _reconcile_name(
    result: ExtractionResult,
    prefix: str,
    name: str,
    fields: dict[str, ExtractedField],
    flag: Callable[..., None],
    confirm: Callable[[str, str], None],
) -> None:
    """Settle a name read from the text of the card against the one in the machine readable zone.

    Both readings come from OCR and fail independently. When they agree the name is confirmed;
    a name cut off on the scan is finished with the zone; when they differ the list of Romanian
    names decides if it can (one of two words is not a name, the other is: ``JOANA`` /
    ``IOANA``); otherwise the readings are left to a person. The printed reading stays the value
    (it has the hyphens and the diacritics of the card), unless the zone gave the word."""
    pool = result.candidates.get(prefix + name, [])
    mrz = next((c for c in pool if c.source == "mrz"), None)
    printed = max((c for c in pool if c.source != "mrz"), key=lambda c: c.confidence, default=None)
    if mrz is None or printed is None:
        return
    kind = names.GIVEN if name == "first_name" else names.FAMILY
    fields[name] = field = printed
    if (
        field.original
        and _agrees(field.original, mrz.value)
        and not _agrees(field.value, mrz.value)
    ):
        field.value, field.original = field.original, None  # the zone says it was read right
    if _agrees(field.value, mrz.value):
        confirm(name, "zona citibilă automat")
    elif _plain(mrz.value).startswith(_plain(field.value)) and mrz.confidence >= MRZ_NAMES_TRUSTED:
        # The name is cut off on the scan (glare, a fold): finish it with the zone's letters.
        # Only for an exact prefix: letters merged as look-alikes could keep a misread one.
        rest = _plain(mrz.value)[len(_plain(field.value)) :]
        field.evidence = f"{field.evidence or field.value} (completat din zona citibilă automat)"
        field.value, field.source = field.value + rest, "derived"
        field.value = names.fix_name(field.value, kind).value  # the zone has no diacritics
        field.issues = [issue for issue in field.issues if not issue.startswith(NAME_NOTES)]
        field.confidence = max(field.confidence, mrz.confidence)
        flag(name, "Trunchiat pe scanare: completat din zona citibilă automat")
    else:
        settled = names.settle(field.value, mrz.value, kind)
        if settled and settled.from_zone:
            before = field.value
            field.evidence = (
                f"{field.evidence or field.value} (stabilit prin zona citibilă automat)"
            )
            field.value, field.source = settled.value, "derived"
            field.issues = [issue for issue in field.issues if not issue.startswith(NAME_NOTES)]
            field.confidence = min(max(field.confidence, mrz.confidence), SETTLED_CONFIDENCE)
            flag(name, f"{CORRECTED} „{before}”: zona citibilă automat conține {mrz.value}")
        elif settled:  # the zone's word is no Romanian name: a misreading of the zone
            mrz.confidence = min(mrz.confidence, INVALID_CONFIDENCE)
            flag(name, f"Zona citibilă automat conține {mrz.value}: nu este un nume românesc", 0.8)
        else:  # two readings that disagree: a person decides, neither is filled in
            flag(name, f"Diferă de zona citibilă automat ({mrz.value})", SUSPECT_CONFIDENCE)
            mrz.confidence = min(mrz.confidence, SUSPECT_CONFIDENCE)
    result.fields[prefix + name] = field


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
            add(name, "Dată invalidă (formatul zz.ll.aaaa)")
        elif spec.kind == "email" and not re.fullmatch(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", value):
            add(name, "Adresă de e-mail invalidă")
        elif spec.kind == "iban" and not _iban_valid(value.replace(" ", "").upper()):
            add(name, "Cifrele de control ale IBAN-ului nu corespund")
        elif spec.kind == "phone" and len(re.sub(r"\D", "", value)) < 6:
            add(name, "Prea scurt pentru un număr de telefon")
        elif spec.kind == "id_series" and not re.fullmatch(r"[A-Za-z]{1,2}", value):
            add(name, "Seria cărții de identitate are 2 litere (de exemplu, AX)")
        elif spec.kind == "id_number" and not re.fullmatch(r"\d{6,7}", value):
            add(name, "Numărul unei cărți de identitate românești are 6-7 cifre")
        elif spec.kind == "list" and name == "caen_activities":
            for line in filter(None, (line.strip() for line in value.splitlines())):
                if not re.match(r"\d{4}\b", line):
                    add(
                        name,
                        f"Rândul CAEN trebuie să înceapă cu clasa CAEN, din 4 cifre: {line[:30]}",
                    )

    for prefix in PERSON_PREFIXES:

        def get(name: str, prefix: str = prefix) -> str:
            return (values.get(prefix + name) or "").strip()

        if cnp := get("cnp"):
            info = check_cnp(cnp)
            if not info.valid:
                add(prefix + "cnp", f"CNP invalid: {info.reason}")
            else:
                birth = parse_date(get("date_of_birth"))
                if birth and info.birth_date and birth != info.birth_date:
                    add(prefix + "date_of_birth", "Diferă de data nașterii din CNP")
                sex = get("sex").upper()
                if sex in ("M", "F") and info.sex and sex != info.sex:
                    add(prefix + "sex", "Diferă de sexul codificat în CNP")
        issued = parse_date(get("id_issue_date"))
        expires = parse_date(get("id_expiry_date"))
        if issued and expires and issued >= expires:
            add(prefix + "id_issue_date", "Data eliberării nu este anterioară datei de expirare")
        if expires and expires < date.today():
            add(prefix + "id_expiry_date", "Cartea de identitate a expirat")
        shares = get("shares")
        if shares and not re.fullmatch(r"\d[\d.]*", shares):
            add(
                prefix + "shares",
                "Introduceți numărul de părți sociale / acțiuni (de exemplu, 450)",
            )

    # a representative files the request: lawyer or proxy is asked each time, never remembered
    if (
        any((values.get(name) or "").strip() for name in ("filer_last_name", "filer_cnp"))
        and not (values.get("representative_type") or "").strip()
    ):
        add(
            "representative_type",
            "Cererea este depusă de un reprezentant: alegeți avocat (împuternicire avocațială) "
            "sau împuternicit (procură autentică)",
        )

    if (turnover := (values.get("estimated_turnover") or "").strip()) and len(
        re.sub(r"\D", "", turnover.split(",")[0])
    ) > 8:
        add("estimated_turnover", "Formularul are 8 căsuțe pentru cifre: cel mult 99.999.999 lei")
    return issues
