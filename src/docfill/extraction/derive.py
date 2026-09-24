"""Derived fields: fill gaps from related values that were found (never invented).

* full name <-> first / last name
* an address -> street, number, block, staircase, floor, apartment, locality, county, country
* ``Jud.SB Mun.Mediaș`` as place of birth -> locality + county of birth
* a valid CNP -> date of birth, sex (and the county that issued it)
* company county -> trade register office
"""

from __future__ import annotations

import re
from collections.abc import Callable

from docfill.extraction.text_utils import compose_address, parse_address, split_full_name
from docfill.models import ExtractedField, ExtractionResult
from docfill.ro import (
    check_cnp,
    compose_street_line,
    county_name,
    format_date,
    looks_romanian_address,
    parse_ro_address,
    split_room,
)
from docfill.templates.placeholders import split_row

DERIVED_FACTOR = 0.95
_ADDRESS_PARTS = ("street_address", "postal_code", "city", "region", "country")
_RO_PARTS = ("street", "street_number", "building", "entrance", "floor", "apartment", "city")


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


def _address_parts(text: str) -> dict[str, str]:
    """Romanian parser for ``Jud.AB Mun.X Str.Y nr.Z`` style, generic parser otherwise."""
    parts: dict[str, str] = {}
    generic = parse_address(text)
    if looks_romanian_address(text):
        ro = parse_ro_address(text)
        parts.update({k: ro[k] for k in _RO_PARTS if k in ro})
        region = ro.get("sector") or ro.get("county")
        if region:
            parts["region"] = region
        if "country" in ro:
            parts["country"] = ro["country"]
        street_line = compose_street_line(ro)
        if street_line:
            parts["street_address"] = street_line
        for key in ("postal_code", "city", "country"):
            if key not in parts and key in generic:
                parts[key] = generic[key]
    else:
        parts.update(generic)
    return parts


def derive_fields(result: ExtractionResult) -> ExtractionResult:
    fields = result.fields

    def offer_all(source: ExtractedField, parts: dict[str, str], prefix: str = "") -> None:
        confidence = source.confidence * DERIVED_FACTOR
        for name, value in parts.items():
            result.offer(_derived(prefix + name, value, confidence, source.value, source.document))

    # names
    if (full := fields.get("full_name")) and (split := split_full_name(full.value)):
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

    # place of birth written as on identity cards: "Jud.SB Mun.Mediaș"
    if (birth := fields.get("place_of_birth")) and looks_romanian_address(birth.value):
        ro = parse_ro_address(birth.value)
        parts = {}
        if "city" in ro:
            parts["place_of_birth"] = ro["city"]
        if "county" in ro:
            parts["birth_county"] = ro["county"]
            parts["birth_country"] = "România"
        if parts.get("place_of_birth") and parts["place_of_birth"] != birth.value:
            result.replace(
                _derived(
                    "place_of_birth",
                    parts.pop("place_of_birth"),
                    birth.confidence,
                    birth.value,
                    birth.document,
                )
            )
        offer_all(birth, parts)

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

    # "Mihai Eminescu camera 2": the room goes with the apartment
    for street_field, room_field in (
        ("street", "apartment"),
        ("company_street", "company_apartment"),
        ("contact_street", "contact_apartment"),
    ):
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

    # identity card type, country from a Romanian county, trade register office
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
    return result


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
