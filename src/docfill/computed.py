"""Computed values: text composed from other fields when a document is filled.

A standard document can reference them like any field (``{{ domicile_line }}``,
``{{ p2_shares_percent }}``). They are never stored or typed: the wizard asks for their inputs
(the street, the number of shares...) and they are recomputed from those every time, so they
cannot go stale. Like derived values, they only combine values that were found or typed.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from docfill.extraction.fields import PERSON_PREFIXES
from docfill.ro import format_amount, parse_amount

Compute = Callable[[Mapping[str, str]], str | None]


@dataclass(frozen=True)
class Computed:
    name: str
    label: str
    inputs: tuple[str, ...]
    # Inputs that must have a value when a document requires the computed value.
    required: tuple[str, ...]
    compute: Compute


def _get(values: Mapping[str, str], name: str) -> str:
    return (values.get(name) or "").strip()


_STREET_KIND = re.compile(
    r"(?i)^(str(ada)?|bd|b-dul|bulevardul|calea|aleea|al|șos|sos|șoseaua|soseaua|splaiul|"
    r"piața|piata|p-ța|intrarea|intr|drumul|sat|satul)\b\.?"
)


def address_line(values: Mapping[str, str], prefix: str = "", county: str = "region") -> str | None:
    """``Mun. Cluj-Napoca, Str. Florilor nr. 5, bl. A2, sc. 1, et. 3, ap. 10, jud. Cluj``, the way
    trade register documents write a domicile."""
    city = _get(values, prefix + "city")
    street = _get(values, prefix + "street")
    region = _get(values, prefix + county)
    if not city and not street:
        return None
    parts = [city] if city else []
    number = _get(values, prefix + "street_number")
    if street:
        street = street if _STREET_KIND.match(street) else f"Str. {street}"
        parts.append(f"{street} nr. {number}" if number else street)
    elif number:
        parts.append(f"nr. {number}")
    for key, label in (
        ("building", "bl."),
        ("entrance", "sc."),
        ("floor", "et."),
        ("apartment", "ap."),
    ):
        if value := _get(values, prefix + key):
            parts.append(f"{label} {value}")
    if region:
        folded = region.casefold()
        bare = folded.startswith(("sector", "jud", "municipiul bucure"))
        parts.append(region if bare else f"jud. {region}")
    return ", ".join(parts)


def _number(values: Mapping[str, str], name: str) -> float | None:
    amount = parse_amount(_get(values, name))
    return amount if amount else None


def share_value(values: Mapping[str, str]) -> float | None:
    capital, count = _number(values, "share_capital"), _number(values, "share_count")
    return capital / count if capital and count else None


def _caen_rows(values: Mapping[str, str]) -> list[tuple[str, str]]:
    from docfill.templates.placeholders import split_row

    rows = []
    for line in _get(values, "caen_activities").splitlines():
        if line.strip():
            code, name = split_row(line.strip(), 2)
            rows.append((code, name.strip(" -–")))
    return rows


def _caen_line(row: tuple[str, str]) -> str:
    return f"{row[0]} - {row[1]}" if row[1] else row[0]


def _main_group(values: Mapping[str, str]) -> str | None:
    rows = _caen_rows(values)
    if rows and re.fullmatch(r"\d{4}", rows[0][0]):
        return rows[0][0][:3]
    return None


def _sex(values: Mapping[str, str], prefix: str) -> str:
    """``M`` / ``F`` from the sex field or the CNP, ``""`` when unknown."""
    from docfill.ro import check_cnp

    sex = _get(values, prefix + "sex").upper()[:1]
    if sex in ("M", "F"):
        return sex
    info = check_cnp(_get(values, prefix + "cnp"))
    return info.sex or "" if info.valid else ""


def _agreeing(word: str, prefix: str) -> Compute:
    """``născut`` for a man, ``născută`` for a woman, ``născut(ă)`` when the sex is unknown."""
    return lambda values: {"M": word, "F": word + "ă"}.get(_sex(values, prefix), word + "(ă)")


def _person_computed(prefix: str, person: str) -> list[Computed]:
    def shares_value(values: Mapping[str, str]) -> str | None:
        shares, value = _number(values, prefix + "shares"), share_value(values)
        return format_amount(shares * value) if shares and value else None

    def shares_percent(values: Mapping[str, str]) -> str | None:
        shares, count = _number(values, prefix + "shares"), _number(values, "share_count")
        return format_amount(shares / count * 100) if shares and count else None

    address = tuple(
        prefix + name
        for name in (
            "city",
            "street",
            "street_number",
            "building",
            "entrance",
            "floor",
            "apartment",
            "region",
        )
    )
    words = [
        Computed(
            f"{prefix}{name}_word",
            f"{person}: “{word}” agreeing with the sex",
            (prefix + "cnp",),
            (),
            _agreeing(word, prefix),
        )
        for name, word in (
            ("born", "născut"),
            ("identified", "identificat"),
            ("appointed", "numit"),
            ("domiciled", "domiciliat"),
        )
    ]
    return [
        *words,
        Computed(
            prefix + "domicile_line",
            f"{person}: domicile (composed)",
            address,
            (prefix + "city",),
            lambda values: address_line(values, prefix),
        ),
        Computed(
            prefix + "shares_value",
            f"{person}: value of the shares (lei)",
            (prefix + "shares", "share_capital", "share_count"),
            (prefix + "shares", "share_capital", "share_count"),
            shares_value,
        ),
        Computed(
            prefix + "shares_percent",
            f"{person}: share of the capital (%)",
            (prefix + "shares", "share_count"),
            (prefix + "shares", "share_count"),
            shares_percent,
        ),
    ]


def _company_seat(values: Mapping[str, str]) -> str | None:
    return address_line(values, "company_", county="county")


_COMPANY_ADDRESS = tuple(
    "company_" + name
    for name in (
        "city",
        "street",
        "street_number",
        "building",
        "entrance",
        "floor",
        "apartment",
        "county",
    )
)

_SPECS: list[Computed] = [
    *(
        computed
        for index, prefix in enumerate(PERSON_PREFIXES, start=1)
        for computed in _person_computed(prefix, f"Person {index}")
    ),
    Computed(
        "company_seat_line",
        "Registered office (composed)",
        _COMPANY_ADDRESS,
        ("company_city", "company_street", "company_street_number", "company_county"),
        _company_seat,
    ),
    Computed(
        "share_capital_amount",
        "Share capital (amount)",
        ("share_capital",),
        ("share_capital",),
        lambda values: format_amount(v) if (v := _number(values, "share_capital")) else None,
    ),
    Computed(
        "share_value",
        "Nominal value of a share (lei)",
        ("share_capital", "share_count"),
        ("share_capital", "share_count"),
        lambda values: format_amount(v) if (v := share_value(values)) else None,
    ),
    Computed(
        "main_activity",
        "Main activity (first CAEN line)",
        ("caen_activities",),
        ("caen_activities",),
        lambda values: _caen_line(rows[0]) if (rows := _caen_rows(values)) else None,
    ),
    Computed(
        "main_caen_group",
        "CAEN group of the main activity",
        ("caen_activities",),
        ("caen_activities",),
        _main_group,
    ),
    Computed(
        "secondary_activities",
        "Secondary activities (other CAEN lines)",
        ("caen_activities",),
        (),
        lambda values: (
            "\n".join(f"clasa CAEN {_caen_line(r)}" for r in _caen_rows(values)[1:]) or None
        ),
    ),
]

COMPUTED: dict[str, Computed] = {spec.name: spec for spec in _SPECS}


def expand(names: Iterable[str]) -> list[str]:
    """Field names with every computed value replaced by its inputs (order kept, no repeats)."""
    expanded: list[str] = []
    for name in names:
        for item in COMPUTED[name].inputs if name in COMPUTED else (name,):
            if item not in expanded:
                expanded.append(item)
    return expanded


def expand_required(names: Iterable[str]) -> list[str]:
    """Required field names with every computed value replaced by its required inputs."""
    expanded: list[str] = []
    for name in names:
        for item in COMPUTED[name].required if name in COMPUTED else (name,):
            if item not in expanded:
                expanded.append(item)
    return expanded


def with_computed(values: Mapping[str, str]) -> dict[str, str]:
    """``values`` plus every computed value that can be computed (given values win)."""
    completed = dict(values)
    for name, spec in COMPUTED.items():
        if not _get(completed, name) and (value := spec.compute(values)):
            completed[name] = value
    return completed


__all__ = ["COMPUTED", "Computed", "address_line", "expand", "expand_required", "with_computed"]
