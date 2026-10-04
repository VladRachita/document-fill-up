"""The identification clause of Romanian legal documents.

Declarations, powers of attorney, decisions of the shareholders and articles of incorporation
identify a person in one sentence, always with the same pieces in a varying order::

    POPESCU ION, CNP 1871114321239, cu domiciliul în Mun. Cluj-Napoca, Str. Florilor nr. 5,
    bl. A2, ap. 10, jud. Cluj, țara România, cetățenia română, născut în Mun. Sibiu, jud. Sibiu,
    la data de 14.11.1987, identificat prin CI, seria AX, nr. 123456, emisă de SPCLEP
    Cluj-Napoca, la data de 22.06.2022, valabilă până la data de 14.11.2032, în calitate de
    administrator al societății EXEMPLU SOFT S.R.L.

The sentence is cut at its keywords (``CNP``, ``domiciliat``, ``născut``, ``seria``...) and
each piece becomes a field. Names written in capitals are family name first, as Romanian
documents print them. Only text that is actually there is read; nothing is guessed.
"""

from __future__ import annotations

import re
import unicodedata

from docfill.extraction.text_utils import clean_value
from docfill.models import ExtractedField
from docfill.ro import check_cnp, normalize_date, parse_ro_address

CLAUSE_CONFIDENCE = 0.93
_UPPER = "A-ZĂÂÎȘȚŞŢ"
_DATE = r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"

# The keywords that open a piece of the clause (matched on accent-free lower-case text).
_KEYWORDS = {
    "cnp": r"\bcnp\b",
    "domicile": r"\b(?:cu\s+)?domicil\w*(?:\s*\(\w+\)|\s*/\s*\w+)?\s+(?:in|la)\b",
    "residence": r"\b(?:cu\s+)?resedint\w*\s+(?:in|la)\b",
    "country": r"\btara\b",
    "citizenship": r"\b(?:de\s+)?cetatenie\b|\bcetatenia\b|\bcetatean\w*\b",
    "born": r"\bnascut\w*(?:\s*\(\w+\))?",
    "document": (
        r"\b(?:identificat\w*(?:\s*/\s*\w+)?(?:\s*\(\w+\))?\s+(?:prin|cu)|posesor\w*"
        r"(?:\s*\(\w+\))?\s+(?:al|a)(?:\s+(?:actului|cartii)\s+de\s+identitate)?"
        r"|act(?:ul)?\s+(?:de\s+)?identitate)\b"
    ),
    "issued": r"\b(?:emis|eliberat)\w*(?:\s*\(\w+\))?(?:\s*/\s*\w+)?\s+de\b",
    "valid": r"\bvalabil\w*(?:\s*\(\w+\))?\s+pana\s+la\b",
    "date": r"\bla\s+data\s+(?:de\b)?",
    "capacity": r"\bin\s+calitate\s+de\b",
}
_ANY = re.compile("|".join(f"(?P<{name}>{pattern})" for name, pattern in _KEYWORDS.items()))
# "POPESCU ION, CNP ..." / "D-nul POPESCU ION, născut(ă) ...": a name in capitals followed by
# the first piece of an identification.
_CLAUSE_START = re.compile(
    rf"(?P<name>[{_UPPER}][{_UPPER}'’-]+(?:[ \t]+[{_UPPER}][{_UPPER}'’-]+){{1,4}})[ \t]*,[ \t]*"
    r"(?=(?i:cnp|cu\s+domiciliul|domicil|n[ăa]scut|cet[ăa][țţt]|posesor|identificat|de\s+cet))"
)
_NOT_NAMES = {"subsemnatul", "subsemnata", "subsemnatul(a)", "d-nul", "d-na", "dl", "dna"}
# A party to a contract (the owner lending the registered office...) does not sign the request:
# "în calitate de comodant" is no capacity of the applicant.
_CONTRACT_PARTIES = {
    "comodant", "comodatar", "locator", "locatar", "chirias", "proprietar", "proprietara",
    "vanzator", "vanzatoare", "cumparator", "cumparatoare", "mandant", "cedent", "cesionar",
}  # fmt: skip
_ID_DOCUMENT = re.compile(
    rf"(?P<kind>[^,]*?)[\s,]*seria[\s:]*(?P<series>[{_UPPER}]{{1,2}})\b[\s,]*"
    r"(?:nr\.?|num[ăa]r(?:ul)?)[\s:]*(?P<number>\d{6,9})\b",
    re.I,
)
_ID_TYPES = {
    "ci": "CI",
    "c.i.": "CI",
    "carte de identitate": "CI",
    "cartea de identitate": "CI",
    "bi": "BI",
    "b.i.": "BI",
    "buletin de identitate": "BI",
    "pasaport": "Pașaport",
    "pasaportul": "Pașaport",
    "cis": "CIS",
    "carte de identitate simpla": "CIS",
    "cei": "CEI",
    "carte electronica de identitate": "CEI",
    "cartea electronica de identitate": "CEI",
}
_COMPANY = re.compile(
    r"\b(?:al|a|la)\s+(?:societ\w+|firmei|S\.?C\.?)\s+(?P<name>[^,;()]+?"
    r"(?:S\.?\s?R\.?\s?L\.?(?:-?D\.?)?|S\.?\s?A\.?|S\.?\s?N\.?\s?C\.?|S\.?\s?C\.?\s?S\.?))(?=[\s,.;(]|$)",
)


def fold(text: str) -> str:
    """Lower-case, accents removed, same length as ``text`` (positions stay valid)."""
    out = []
    for char in text:
        base = unicodedata.normalize("NFD", char)[0]
        out.append(base.lower() if len(base.lower()) == 1 else char)
    return "".join(out)


def _pieces(text: str) -> list[tuple[str, str]]:
    """``[(keyword, text after it up to the next keyword)]`` in order."""
    folded = fold(text)
    matches = list(_ANY.finditer(folded))
    pieces = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = text[match.end() : end]
        pieces.append((match.lastgroup or "", value.strip(" ,;:\n\t")))
    return pieces


def _county_label(text: str) -> str:
    """``Judeţ/sector Timiș`` -> ``jud. Timiș``; ``judeţ/sector 3`` -> ``Sector 3``."""

    def replace(match: re.Match[str]) -> str:
        return "Sector " if match["number"] else "jud. "

    return re.sub(
        r"(?i)\bjude[țţt](?:ul)?\s*/\s*sector(?:ul)?\s*(?=(?P<number>\d)?)", replace, text
    )


def _first_date(text: str) -> str | None:
    found = re.search(_DATE, text)
    return normalize_date(found.group()) if found else None


def _citizenship(text: str) -> str | None:
    word = fold(text.split(",")[0]).strip(" .")
    if word.startswith(("roman", "rou")):
        return "Română"
    if word.startswith("moldov"):
        return "Moldovenească"
    word = text.split(",")[0].strip(" .")
    return word[:1].upper() + word[1:] if word and len(word) < 40 else None


def _place(text: str) -> dict[str, str]:
    """``Mun. Sibiu, jud. Sibiu`` -> locality and county."""
    text = _county_label(text)
    parts = parse_ro_address(text)
    found = {}
    if parts.get("city"):
        found["city"] = parts["city"]
    elif text.split(",")[0].strip():
        found["city"] = text.split(",")[0].strip()
    if parts.get("county") or parts.get("sector"):
        found["county"] = parts.get("county") or parts["sector"]
    return found


def clause_fields(text: str) -> dict[str, str]:
    """The fields of one identification clause (plain docfill names)."""
    found: dict[str, str] = {}
    pieces = _pieces(text)
    context = ""  # which part of the clause we are in: domicile, birth, document

    def put(name: str, value: str | None) -> None:
        value = clean_value(value or "").strip(" ,;:")
        if name != "company_name":  # "S.R.L." keeps its full stop
            value = value.rstrip(".").strip()
        if value and name not in found:
            found[name] = value

    for keyword, value in pieces:
        if keyword == "cnp":
            digits = re.sub(r"\D", "", value)[:13]
            if check_cnp(digits).valid:
                put("cnp", digits)
        elif keyword in ("domicile", "residence"):
            context = "domicile"
            value = _county_label(value)
            address = parse_ro_address(value)
            put("full_address", value)
            for name in ("city", "street", "street_number", "building", "entrance"):
                put(name, address.get(name))
            put("floor", address.get("floor"))
            put("apartment", address.get("apartment"))
            put("region", address.get("sector") or address.get("county"))
        elif keyword == "country":
            put("birth_country" if context == "birth" else "country", value.split(",")[0])
        elif keyword == "citizenship":
            put("citizenship", _citizenship(value))
        elif keyword == "born":
            context = "birth"
            if value.lower().startswith(("in ", "în ")):
                place = _place(value[3:])
                put("place_of_birth", place.get("city"))
                put("birth_county", place.get("county"))
            elif date := _first_date(value):
                put("date_of_birth", date)
        elif keyword == "date":
            date = _first_date(value)
            if context == "birth":
                put("date_of_birth", date)
                # "la data de 14.11.1987, în Mun. Sibiu, jud. Sibiu" (date first, then place)
                after = re.search(_DATE, value)
                rest = value[after.end() :].strip(" ,") if after else ""
                if rest.lower().startswith(("in ", "în ")):
                    place = _place(rest[3:])
                    put("place_of_birth", place.get("city"))
                    put("birth_county", place.get("county"))
            elif context == "document":
                put("id_issue_date", date)
        elif keyword == "document":
            context = "document"
            _id_document(value, put)
        elif keyword == "issued" and context == "document":
            put("id_issued_by", value)
        elif keyword == "valid":
            put("id_expiry_date", _first_date(value))
        elif keyword == "capacity":
            capacity = re.split(r"\s+(?:al|a|la)\s+(?:societ|firm|S\.?C\b)", value, maxsplit=1)[0]
            capacity = re.sub(r"\s+(?:numit|numită|desemnat|desemnată|ales|aleasă)$", "", capacity)
            if next(iter(fold(capacity).split()), "").strip(" .,") not in _CONTRACT_PARTIES:
                put("capacity", capacity)
            if company := _COMPANY.search(value):
                put("company_name", company["name"])
    if "id_series" not in found:  # "CI seria AX nr. 123456" without "identificat prin"
        _id_document(" ".join(value for _, value in pieces), put)
    return found


def _id_document(text: str, put) -> None:
    match = _ID_DOCUMENT.search(text)
    if not match:
        return
    kind = fold(match["kind"]).strip(" .,:").replace("/", " ")
    kind = re.sub(r"^(?:al|a|cu|prin)\s+", "", kind)
    for label, id_type in _ID_TYPES.items():
        if kind == label or kind.startswith(label + " ") or kind.endswith(" " + label):
            put("id_type", id_type)
            break
    put("id_series", match["series"].upper())
    put("id_number", match["number"])


def split_name(name: str) -> tuple[str, str] | None:
    """A name printed in capitals, family name first: ``POPESCU ION`` -> (``Ion``,
    ``Popescu``); ``POPESCU-IONESCU ANA MARIA`` -> (``Ana Maria``, ``Popescu-Ionescu``)."""
    tokens = [t for t in name.split() if fold(t).strip(".") not in _NOT_NAMES]
    if len(tokens) < 2:
        return None
    return " ".join(tokens[1:]).title(), tokens[0].title()


def extract_clauses(
    text: str, document: str | None = None, doc_type: str | None = None
) -> list[ExtractedField]:
    """Fields of the first identification clause of ``text``: from the name in capitals (or,
    without one, from the CNP / domicile) to the end of the sentence."""
    if doc_type in ("id_card", "birth_certificate"):  # read by their own extractors
        return []
    start = _CLAUSE_START.search(text)
    anchor = start.end() if start else None
    if anchor is None:
        found = re.search(r"\bcnp\b|\b(?:cu\s+)?domicil\w*", fold(text))
        if not found:
            return []
        anchor = found.start()
    # the sentence: up to the first full stop followed by a new line (or a blank line)
    end = re.search(r"\.\s*\n|\n\s*\n", text[anchor:])
    sentence = text[anchor : anchor + end.start() + 1 if end else len(text)]
    fields = clause_fields(sentence)
    if len(fields) < (3 if start else 4):  # a lone "CNP" is not a clause
        return []
    if start and (name := split_name(start["name"])):
        fields.setdefault("first_name", name[0])
        fields.setdefault("last_name", name[1])
    evidence = clean_value(sentence)[:160]
    return [
        ExtractedField(
            name=name,
            value=value,
            confidence=CLAUSE_CONFIDENCE,
            source="pattern",
            evidence=evidence,
            document=document,
        )
        for name, value in fields.items()
    ]


__all__ = ["CLAUSE_CONFIDENCE", "clause_fields", "extract_clauses", "split_name"]
