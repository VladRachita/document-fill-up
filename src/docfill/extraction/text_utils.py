"""String helpers shared by the extractors: folding, name splitting, address parsing."""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

import pycountry


def fold(text: str) -> str:
    """Lower-case and strip accents *without changing the string length*.

    Keeping offsets stable lets us match on the folded string and slice the original.
    """
    out = []
    for char in text:
        decomposed = unicodedata.normalize("NFD", char)
        out.append(decomposed[0] if decomposed else char)
    return "".join(out).lower()


def collapse(text: str) -> str:
    return " ".join(text.split())


def clean_value(text: str) -> str:
    return collapse(text).strip(" \t,;:|-–—_*")


# --------------------------------------------------------------------------- names

_NAME_PARTICLES = {
    "van",
    "von",
    "de",
    "der",
    "den",
    "da",
    "di",
    "du",
    "del",
    "della",
    "la",
    "le",
    "dos",
    "das",
    "ter",
    "ten",
    "bin",
    "ibn",
    "al",
    "el",
}
_NAME_TOKEN = re.compile(r"^[^\W\d_][^\W\d_'’.-]*(?:['’.-][^\W\d_]*)*\.?$")


def is_all_caps(token: str) -> bool:
    letters = [char for char in token if char.isalpha()]
    return len(letters) > 1 and all(char.isupper() for char in letters)


def normalize_name(value: str) -> str:
    """``POPESCU`` -> ``Popescu``; mixed-case names are left untouched."""
    value = collapse(value)
    if is_all_caps(value):
        return value.title()
    return value


def looks_like_name(value: str, strict: bool = False) -> bool:
    tokens = value.split()
    if not 1 <= len(tokens) <= 5 or len(value) > 80:
        return False
    if not all(_NAME_TOKEN.match(token) for token in tokens):
        return False
    if strict:
        return all(token[0].isupper() or token.lower() in _NAME_PARTICLES for token in tokens)
    return True


def split_full_name(full_name: str) -> tuple[str, str] | None:
    """Split a full name into (first, last). Returns ``None`` when it cannot be split.

    Handles ``Smith, John``, ``POPESCU Ion`` (upper-case family name) and name particles
    (``Ludwig van Beethoven`` -> ``Ludwig`` / ``van Beethoven``).
    """
    full_name = collapse(full_name)
    if "," in full_name:
        last, _, first = full_name.partition(",")
        first, last = collapse(first), collapse(last)
        if first and last:
            return normalize_name(first), normalize_name(last)
        return None
    tokens = full_name.split()
    if len(tokens) < 2:
        return None

    caps = [is_all_caps(token) for token in tokens]
    if any(caps) and not all(caps):
        last_tokens = [token for token, cap in zip(tokens, caps, strict=True) if cap]
        first_tokens = [token for token, cap in zip(tokens, caps, strict=True) if not cap]
        return normalize_name(" ".join(first_tokens)), normalize_name(" ".join(last_tokens))

    for index in range(1, len(tokens) - 1):
        if tokens[index] in _NAME_PARTICLES:
            first, last = tokens[:index], tokens[index:]
            return normalize_name(" ".join(first)), normalize_name(" ".join(last))
    return normalize_name(" ".join(tokens[:-1])), normalize_name(tokens[-1])


# --------------------------------------------------------------------------- countries

_COUNTRY_ALIASES = {
    "uk",
    "u.k.",
    "great britain",
    "england",
    "scotland",
    "wales",
    "usa",
    "u.s.a.",
    "us",
    "u.s.",
    "america",
    "romania",
    "deutschland",
    "italia",
    "espana",
    "moldova",
    "osterreich",
    "schweiz",
    "suisse",
    "nederland",
    "belgique",
    "polska",
    "magyarorszag",
    "turkey",
}


@lru_cache
def _country_names() -> frozenset[str]:
    names = set(_COUNTRY_ALIASES)
    for country in pycountry.countries:
        for attr in ("name", "official_name", "common_name"):
            value = getattr(country, attr, None)
            if value:
                names.add(fold(value))
    return frozenset(names)


def is_country(value: str) -> bool:
    folded = fold(collapse(value)).strip(" .")
    if folded.startswith("the "):
        folded = folded[4:]
    return folded in _country_names()


# --------------------------------------------------------------------------- addresses

POSTAL_CODE = r"(?:\d{4,6}(?:-\d{3,4})?|[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}|[A-Z]\d[A-Z]\s?\d[A-Z]\d)"
_POSTAL_ONLY = re.compile(rf"^{POSTAL_CODE}$")
_POSTAL_THEN_CITY = re.compile(rf"^(?P<postal>{POSTAL_CODE})\s+(?P<city>[^\d,]+)$")
_CITY_THEN_POSTAL = re.compile(
    rf"^(?P<city>[^\d,]+?)\s+(?:(?P<region>[A-Z]{{2}})\s+)?(?P<postal>{POSTAL_CODE})$"
)
_US_STATE_POSTAL = re.compile(r"^(?P<region>[A-Z]{2})\s+(?P<postal>\d{5}(?:-\d{4})?)$")
_REGION_PREFIX = re.compile(r"^(?:jud(?:etul|et)?\.?|county|state|province)\s+", re.IGNORECASE)


def looks_like_postal_code(value: str) -> bool:
    return bool(_POSTAL_ONLY.match(value.strip().upper())) and any(c.isdigit() for c in value)


def parse_address(address: str) -> dict[str, str]:
    """Best-effort split of a one-line address into street/city/postal_code/region/country.

    ``12 Baker Street, London NW1 6XE, United Kingdom``
    ``Str. Florilor nr. 5, 400001 Cluj-Napoca, jud. Cluj, Romania``
    ``742 Evergreen Terrace, Springfield, IL 62704, USA``
    """
    parts = [clean_value(part) for part in re.split(r"[,;\n]", address)]
    parts = [part for part in parts if part]
    result: dict[str, str] = {}
    if not parts:
        return result

    if len(parts) > 1 and is_country(parts[-1]):
        result["country"] = parts.pop()
    if not parts:
        return result
    result["street_address"] = parts.pop(0)
    # A lone "nr. 5" / "Apt 3" right after the street belongs to it.
    while parts and re.match(
        r"^(?:nr\.?|no\.?|apt\.?|bl\.?|sc\.?|et\.?|ap\.?)\s*\w+", parts[0], re.IGNORECASE
    ):
        result["street_address"] += ", " + parts.pop(0)

    for part in parts:
        if match := _US_STATE_POSTAL.match(part):
            result.setdefault("region", match["region"])
            result.setdefault("postal_code", match["postal"])
        elif match := _POSTAL_THEN_CITY.match(part):
            result.setdefault("postal_code", match["postal"])
            result.setdefault("city", clean_value(match["city"]))
        elif (match := _CITY_THEN_POSTAL.match(part)) and any(c.isdigit() for c in match["postal"]):
            result.setdefault("city", clean_value(match["city"]))
            if match["region"]:
                result.setdefault("region", match["region"])
            result.setdefault("postal_code", match["postal"])
        elif looks_like_postal_code(part):
            result.setdefault("postal_code", part)
        elif _REGION_PREFIX.match(part):
            result.setdefault("region", _REGION_PREFIX.sub("", part))
        elif "city" not in result:
            result["city"] = part
        else:
            result.setdefault("region", part)
    return result


def compose_address(values: dict[str, str]) -> str | None:
    street = values.get("street_address")
    if not street:
        return None
    locality = " ".join(v for v in (values.get("postal_code"), values.get("city")) if v)
    parts = [street, locality, values.get("region"), values.get("country")]
    return ", ".join(part for part in parts if part)


_US_LOCALITY = re.compile(r"^[A-Z][A-Za-z .'-]+,\s*[A-Z]{2}\s+\d{5}(?:-\d{4})?$")


def is_address_continuation(line: str) -> bool:
    """True for lines like ``London NW1 6XE``, ``400001 Cluj-Napoca``, ``Springfield, IL 62704``
    or a bare country name, which usually continue an address written over several lines."""
    line = clean_value(line)
    if not line or len(line) > 80:
        return False
    if is_country(line) or _US_LOCALITY.match(line) or _POSTAL_THEN_CITY.match(line):
        return True
    match = _CITY_THEN_POSTAL.match(line)
    return bool(match and any(char.isdigit() for char in match["postal"]))
