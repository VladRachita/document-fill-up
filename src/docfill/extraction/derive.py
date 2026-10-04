"""Derived fields: fill gaps from related values that were found (never invented).

* full name <-> first / last name
* an address -> street, number, block, staircase, floor, apartment, locality, county, country
* ``Jud.SB Mun.Mediaș`` as place of birth -> locality + county of birth
* a valid CNP -> date of birth, sex (and the county that issued it)
* company county -> trade register office
* the shares of each person -> the list of associates; the board roles -> how the company is
  administered; a filer -> who files the beneficial owner declaration
* a lawyer or a proxy filing the request -> "prin ... conform ..." and the filer's capacity

Person-level derivations run for every person (the applicant, then ``p2_``, ``p3_``).
"""

from __future__ import annotations

import re
from collections.abc import Callable

from docfill.extraction.clauses import fold, split_name
from docfill.extraction.fields import PERSON_PREFIXES, split_person
from docfill.extraction.idcard import SUSPECT_CONFIDENCE
from docfill.extraction.text_utils import compose_address, parse_address, split_full_name
from docfill.models import ExtractedField, ExtractionResult
from docfill.ro import (
    check_cnp,
    compose_street_line,
    county_code,
    county_name,
    format_amount,
    format_date,
    looks_romanian_address,
    parse_amount,
    parse_ro_address,
    place_doubt,
    split_room,
)
from docfill.templates.placeholders import split_row

DERIVED_FACTOR = 0.95
SUGGESTED_CONFIDENCE = SUSPECT_CONFIDENCE - 0.01  # offered to a person, never filled in
_COUNTY_TRUSTED = 0.7  # a county read from the text; not the one a CNP was issued in
_ADDRESS_PARTS = ("street_address", "postal_code", "city", "region", "country")
_RO_PARTS = ("street", "street_number", "building", "entrance", "floor", "apartment", "city")
_STREET_WORD = re.compile(r"\b(?:str|strada|bd|b-dul|aleea|calea|sos|soseaua|nr|bl|sc|et|ap)\b")
_NOT_READ = {"manual", "form", "default", "memory"}  # values that are not read from a text


def _derived(
    name: str, value: str, confidence: float, evidence: str, document: str | None
) -> ExtractedField:
    return ExtractedField(
        name=name,
        value=value,
        confidence=round(min(confidence, 0.99), 4),
        source="derived",
        evidence=evidence,
        document=document,
    )


def _family_name_first(found: ExtractedField) -> bool:
    """Read under a Romanian label that names the family name first ("Nume și prenume")."""
    return bool(re.search(r"\bnume(?:le)?\b.{0,12}\bprenume(?:le)?\b", fold(found.evidence or "")))


def _address_parts(text: str) -> dict[str, str]:
    """Romanian parser for ``Jud.AB Mun.X Str.Y nr.Z`` style, generic parser otherwise."""
    parts: dict[str, str] = {}
    generic = parse_address(text)
    if looks_romanian_address(text):
        ro = parse_ro_address(text)
        parts.update({k: ro[k] for k in _RO_PARTS if k in ro})
        if "room" in ro and "street" in parts:  # "ap. 29, camera 1": no box for the room
            parts["street"] = f"{parts['street']}, {ro['room']}"
        region = ro.get("sector") or ro.get("county")
        if region:
            parts["region"] = region
        if "country" in ro:
            parts["country"] = ro["country"]
        street_line = compose_street_line(ro)
        if street_line:
            parts["street_address"] = street_line
        for key in ("postal_code", "city", "country"):
            if key in parts or key not in generic:
                continue
            if key == "city" and _STREET_WORD.search(fold(generic["city"])):
                continue  # the generic parser took the street line for the locality
            parts[key] = generic[key]
    else:
        parts.update(generic)
    return parts


def derive_fields(result: ExtractionResult) -> ExtractionResult:
    _derive_person(result)
    for prefix in PERSON_PREFIXES[1:]:
        _derive_other_person(result, prefix)
    _derive_company(result)
    _derive_roles(result)
    return result


def _derive_other_person(result: ExtractionResult, prefix: str) -> None:
    """Person-level derivations for ``p2_`` / ``p3_`` fields: run on the plain names, then
    renamed back."""
    person = ExtractionResult()
    for name, found in result.fields.items():
        if name.startswith(prefix) and split_person(name)[0] > 1:
            base = name[len(prefix) :]
            person.fields[base] = found.model_copy(update={"name": base})
    if not person.fields:
        return
    before = dict(person.fields)
    _derive_person(person)
    for base, found in person.fields.items():
        if before.get(base) is found:
            continue
        renamed = found.model_copy(update={"name": prefix + base})
        if base in before:
            result.replace(renamed)
        else:
            result.offer(renamed)


def _derive_person(result: ExtractionResult) -> None:
    """Names, domicile, place of birth, CNP and identity card of one person."""
    fields = result.fields

    def offer_all(source: ExtractedField, parts: dict[str, str], prefix: str = "") -> None:
        confidence = source.confidence * DERIVED_FACTOR
        for name, value in parts.items():
            result.offer(_derived(prefix + name, value, confidence, source.value, source.document))

    # names ("Nume și prenume: POPESCU ION" is written family name first)
    if full := fields.get("full_name"):
        split = split_name(full.value) if _family_name_first(full) else None
        if split := split or split_full_name(full.value):
            offer_all(full, {"first_name": split[0], "last_name": split[1]})
    if (first := fields.get("first_name")) and (last := fields.get("last_name")):
        confidence = min(first.confidence, last.confidence) * DERIVED_FACTOR
        full_value = f"{first.value} {last.value}"
        result.offer(_derived("full_name", full_value, confidence, full_value, first.document))

    # domicile
    if address := fields.get("full_address"):
        offer_all(address, _address_parts(address.value))
    elif line := fields.get("street_address"):
        parts = parse_ro_address(line.value)
        offer_all(line, {k: parts[k] for k in _RO_PARTS if k in parts and k != "city"})
    parts = {name: fields[name] for name in _ADDRESS_PARTS if name in fields}
    if composed := compose_address({name: field.value for name, field in parts.items()}):
        confidence = min(field.confidence for field in parts.values()) * DERIVED_FACTOR
        current = fields.get("full_address")
        if current and composed != current.value and composed.startswith(current.value):
            # "Hauptstraße 5" + city/country found elsewhere -> "Hauptstraße 5, Berlin, Germany"
            result.replace(
                _derived(
                    "full_address",
                    composed,
                    max(confidence, current.confidence),
                    composed,
                    current.document,
                )
            )
        elif not current:
            result.offer(_derived("full_address", composed, confidence, composed, None))

    # place of birth written as on identity cards: "Jud.SB Mun.Mediaș", "Mun.București Sec.2"
    if (birth := fields.get("place_of_birth")) and looks_romanian_address(birth.value):
        ro = parse_ro_address(birth.value)
        place = ro.get("city")
        if place and "sector" in ro:  # Bucharest: the sector is part of the place of birth
            place = f"{place} {ro['sector']}"
        county = ro.get("county") or ("București" if "sector" in ro else None)
        parts = {}
        if place:
            parts["place_of_birth"] = place
        if county:
            parts["birth_county"] = county
            parts["birth_country"] = "România"
        if place and place != birth.value:
            result.replace(
                _derived(
                    "place_of_birth",
                    parts.pop("place_of_birth"),
                    birth.confidence,
                    birth.value,
                    birth.document,
                )
            )
        elif not place:  # "Jud.IS" and a smudge: that is not a place of birth to write in a form
            problem = "The locality could not be read: check the place of birth"
            suspect = {"confidence": min(birth.confidence, SUSPECT_CONFIDENCE)}
            result.replace(birth.model_copy(update={**suspect, "issues": [*birth.issues, problem]}))
        offer_all(birth, parts)

    # a municipality that is not one, a place cut off by glare or a fold: shown, not filled in
    # (for text read from a document, not for what a person typed or a filled form says)
    for name, county_name_ in (("place_of_birth", "birth_county"), ("city", "region")):
        field = fields.get(name)
        if field is None or field.source in _NOT_READ:
            continue
        scope = fields.get(county_name_)
        county = county_code(scope.value) if scope and scope.confidence >= _COUNTY_TRUSTED else None
        doubt = place_doubt(field.value, birth=name == "place_of_birth", county=county)
        if doubt is None:
            continue
        doubted = field.model_copy(
            update={
                "confidence": min(field.confidence, SUSPECT_CONFIDENCE),
                "issues": [*field.issues, doubt.problem],
            }
        )
        if len(doubt.suggestions) == 1:
            # cut off, or one letter off, and one name of the register fits: shown as the value
            # to check, the reading kept next to it
            result.replace(doubted.model_copy(update={"confidence": SUGGESTED_CONFIDENCE - 0.1}))
            suggested = _derived(
                name, doubt.suggestions[0], SUGGESTED_CONFIDENCE, field.value, None
            )
            result.replace(suggested.model_copy(update={"issues": [doubt.problem]}))
            continue
        result.replace(doubted)
        for suggestion in doubt.suggestions:
            result.offer(_derived(name, suggestion, SUGGESTED_CONFIDENCE, field.value, None))

    # CNP
    if (cnp := fields.get("cnp")) and (info := check_cnp(cnp.value)).valid:
        confidence = cnp.confidence * DERIVED_FACTOR
        evidence = f"CNP {cnp.value}"
        if info.birth_date:
            result.offer(
                _derived(
                    "date_of_birth",
                    format_date(info.birth_date),
                    confidence,
                    evidence,
                    cnp.document,
                )
            )
        if info.sex:
            result.offer(_derived("sex", info.sex, confidence, evidence, cnp.document))
        if info.county:  # county where the CNP was issued: usually, not always, the birth county
            result.offer(_derived("birth_county", info.county, 0.55, evidence, cnp.document))
        if cnp.value[0] in "123456":
            result.offer(_derived("citizenship", "Română", 0.6, evidence, cnp.document))

    # "Mihai Eminescu camera 2": the room goes with the apartment (unless there is one: "ap. 29,
    # camera 1" keeps the room with the street)
    for street_field, room_field in (("street", "apartment"),):
        if room_field in fields:
            continue
        if (street := fields.get(street_field)) and (split := split_room(street.value)):
            result.replace(
                _derived(street_field, split[0], street.confidence, street.value, street.document)
            )
            result.offer(
                _derived(
                    room_field,
                    split[1],
                    street.confidence * DERIVED_FACTOR,
                    street.value,
                    street.document,
                )
            )

    # identity card type, country from a Romanian county
    if "id_series" in fields and "id_number" in fields:
        series = fields["id_series"]
        result.offer(_derived("id_type", "CI", 0.6, series.value, series.document))
    for county_field, country_field in (("region", "country"), ("birth_county", "birth_country")):
        if (county := fields.get(county_field)) and county_name(county.value):
            result.offer(
                _derived(
                    country_field, "România", county.confidence * 0.9, county.value, county.document
                )
            )


def _derive_company(result: ExtractionResult) -> None:
    fields = result.fields

    def offer_all(source: ExtractedField, parts: dict[str, str], prefix: str = "") -> None:
        confidence = source.confidence * DERIVED_FACTOR
        for name, value in parts.items():
            result.offer(_derived(prefix + name, value, confidence, source.value, source.document))

    # company registered office
    if office := fields.get("company_address"):
        office_parts = _address_parts(office.value)
        mapping = {
            "street": "street",
            "street_number": "street_number",
            "building": "building",
            "entrance": "entrance",
            "floor": "floor",
            "apartment": "apartment",
            "city": "city",
            "region": "county",
        }
        offer_all(
            office,
            {
                f"company_{target}": office_parts[source]
                for source, target in mapping.items()
                if source in office_parts
            },
        )

    # "Mihai Eminescu camera 2": the room goes with the apartment (unless there is one)
    for street_field, room_field in (
        ("company_street", "company_apartment"),
        ("contact_street", "contact_apartment"),
    ):
        if room_field in fields:
            continue
        if (street := fields.get(street_field)) and (split := split_room(street.value)):
            result.replace(
                _derived(street_field, split[0], street.confidence, street.value, street.document)
            )
            result.offer(
                _derived(
                    room_field,
                    split[1],
                    street.confidence * DERIVED_FACTOR,
                    street.value,
                    street.document,
                )
            )

    # total pages of the submitted documents (third column of each row)
    if documents := fields.get("attached_documents"):
        pages = [split_row(row, 3)[2].strip() for row in documents.value.splitlines() if row]
        if pages and all(re.fullmatch(r"\d+", p) for p in pages):
            result.offer(
                _derived(
                    "attached_pages_total",
                    str(sum(int(p) for p in pages)),
                    documents.confidence,
                    "sum of the pages column",
                    documents.document,
                )
            )

    # trade register office
    if company_county := fields.get("company_county"):
        result.offer(
            _derived(
                "orc_office",
                company_county.value,
                company_county.confidence * 0.9,
                company_county.value,
                company_county.document,
            )
        )


def _derive_roles(result: ExtractionResult) -> None:
    """What the roles of the persons imply for the company."""
    fields = result.fields

    def value(name: str) -> str:
        found = fields.get(name)
        return found.value.strip() if found else ""

    # associates: "NAME | CNP | shares", one line per person holding shares
    count = parse_amount(value("share_count"))
    rows, sources = [], []
    for prefix in PERSON_PREFIXES:
        shares = parse_amount(value(prefix + "shares"))
        name = " ".join(filter(None, (value(prefix + "last_name"), value(prefix + "first_name"))))
        if not shares or not name:
            continue
        held = f"{format_amount(shares)} acțiuni"
        if count:
            held += f" ({format_amount(shares / count * 100)}%)"
        rows.append(" | ".join(filter(None, (name.upper(), value(prefix + "cnp"), held))))
        sources.append(fields[prefix + "shares"])
    if rows:
        confidence = min(source.confidence for source in sources) * DERIVED_FACTOR
        result.offer(
            _derived("associates", "\n".join(rows), confidence, "the shares of each person", None)
        )

    # a sole administrator or a board of directors
    roles = [value(prefix + "board_role").casefold() for prefix in PERSON_PREFIXES]
    roles = [role for role in roles if role]
    if roles:
        administration = (
            "administrator unic" if roles == ["administrator unic"] else "consiliu de administrație"
        )
        result.offer(_derived("administration", administration, 0.9, "the board roles", None))

    # who the representative filing the request is: "prin avocat, conform împuternicirii
    # avocațiale" (IV), "în calitate de avocat, conform ..." (XII)
    if kind := fields.get("representative_type"):
        for name, written in representation(kind.value).items():
            result.offer(_derived(name, written, 0.9, kind.value, kind.document))

    # the beneficial owner declaration is filed by the proxy when there is one. Derived again
    # from what is known now: the act constitutiv alone said "no proxy" before the proxy's
    # identity card was merged in. A value read or typed is kept.
    filed_by = None
    if filer := fields.get("filer_last_name"):
        filed_by = _derived("bo_filed_by", "împuternicit", 0.9, filer.value, filer.document)
    elif any(value(prefix + "beneficial_owner") for prefix in PERSON_PREFIXES):
        filed_by = _derived(
            "bo_filed_by", "reprezentantul legal", 0.9, "no proxy (filer) given", None
        )
    current = fields.get("bo_filed_by")
    if filed_by and (current is None or current.source == "derived"):
        result.replace(filed_by)


def representation(kind: str) -> dict[str, str]:
    """What a representative writes in the forms: a lawyer acts under an împuternicire
    avocațială, a proxy under an authenticated special or general power of attorney."""
    folded = fold(kind)
    if "avocat" in folded:
        who, basis = "avocat", "împuternicirii avocațiale"
    elif "general" in folded:
        who, basis = "împuternicit", "procurii generale autentice"
    elif re.search(r"imputernicit|procur|special", folded):
        who, basis = "împuternicit", "procurii speciale autentice"
    else:
        return {}
    return {
        "represented_by": who,
        "representation_basis": basis,
        "filer_capacity": who,
        "filer_basis": basis,
    }


def complete_values(values: dict[str, str]) -> dict[str, tuple[str, str]]:
    """Suggestions for empty fields derived from the current values (e.g. typed in the wizard).

    Returns ``{field: (value, explanation)}`` for fields not already in ``values``.
    """
    result = ExtractionResult()
    for name, value in values.items():
        if value:
            result.offer(ExtractedField(name=name, value=value, confidence=1.0, source="manual"))
    derive_fields(result)
    suggestions: dict[str, tuple[str, str]] = {}
    for name, field in result.fields.items():
        if not values.get(name) and field.source == "derived":
            suggestions[name] = (field.value, f"derived from {field.evidence}")
    return suggestions


Derivation = Callable[[ExtractionResult], None]
