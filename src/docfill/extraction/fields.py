"""Catalog of the personal-data fields docfill knows how to extract.

Adding a field is a matter of adding a :class:`FieldSpec` here: the label-based extractor picks
up its synonyms automatically and standard documents can reference it as ``{{ name }}``.
Synonyms are matched case- and accent-insensitively ("Județ" == "judet").
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

FieldKind = Literal["name", "address", "place", "postal_code", "country", "text"]


@dataclass(frozen=True)
class FieldSpec:
    name: str
    label: str
    kind: FieldKind
    synonyms: tuple[str, ...]
    max_length: int = 120


FIELDS: dict[str, FieldSpec] = {
    spec.name: spec
    for spec in (
        FieldSpec(
            "first_name",
            "First name",
            "name",
            (
                "first name",
                "first names",
                "given name",
                "given names",
                "forename",
                "forenames",
                "christian name",
                # Romanian
                "prenume",
                "prenumele",
            ),
            max_length=80,
        ),
        FieldSpec(
            "last_name",
            "Last name",
            "name",
            (
                "last name",
                "surname",
                "family name",
                # Romanian
                "nume",
                "numele",
                "nume de familie",
            ),
            max_length=80,
        ),
        FieldSpec(
            "full_name",
            "Full name",
            "name",
            (
                "full name",
                "name",
                "name and surname",
                "applicant",
                "applicant name",
                "holder",
                "holder name",
                # Romanian
                "nume si prenume",
                "numele si prenumele",
                "nume complet",
                "subsemnatul",
                "subsemnata",
            ),
            max_length=120,
        ),
        FieldSpec(
            "full_address",
            "Address",
            "address",
            (
                "address",
                "home address",
                "residential address",
                "permanent address",
                "address of residence",
                "place of residence",
                "residence",
                "domicile",
                # Romanian
                "adresa",
                "adresa de domiciliu",
                "domiciliu",
                "domiciliul",
                "domiciliat in",
                "domiciliata in",
                "resedinta",
            ),
            max_length=250,
        ),
        FieldSpec(
            "street_address",
            "Street",
            "address",
            (
                "street",
                "street address",
                "address line 1",
                "address line",
                # Romanian
                "strada",
            ),
            max_length=150,
        ),
        FieldSpec(
            "city",
            "City",
            "place",
            (
                "city",
                "town",
                "city/town",
                "locality",
                "municipality",
                "village",
                # Romanian
                "oras",
                "orasul",
                "localitate",
                "localitatea",
                "municipiul",
                "comuna",
            ),
            max_length=80,
        ),
        FieldSpec(
            "postal_code",
            "Postal code",
            "postal_code",
            (
                "postal code",
                "postcode",
                "post code",
                "zip",
                "zip code",
                "zipcode",
                # Romanian
                "cod postal",
            ),
            max_length=12,
        ),
        FieldSpec(
            "region",
            "State / county",
            "place",
            (
                "state",
                "county",
                "province",
                "region",
                "state/province",
                # Romanian
                "judet",
                "judetul",
            ),
            max_length=80,
        ),
        FieldSpec(
            "country",
            "Country",
            "country",
            (
                "country",
                "country of residence",
                # Romanian
                "tara",
                "tara de resedinta",
            ),
            max_length=80,
        ),
        FieldSpec(
            "place_of_birth",
            "Place of birth",
            "place",
            (
                "place of birth",
                "birthplace",
                "birth place",
                "city of birth",
                # Romanian
                "locul nasterii",
                "loc nastere",
                "locul de nastere",
            ),
            max_length=120,
        ),
    )
}


def field_labels() -> dict[str, str]:
    return {name: spec.label for name, spec in FIELDS.items()}
