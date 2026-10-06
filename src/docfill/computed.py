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
from docfill.ro import format_amount, parse_amount, split_room

Compute = Callable[[Mapping[str, str]], str | None]


@dataclass(frozen=True)
class Computed:
    name: str
    label: str
    inputs: tuple[str, ...]
    # Inputs that must have a value when a document requires the computed value.
    required: tuple[str, ...]
    compute: Compute
    # The field a value printed by a filled form is read back as (a filled Anexa 4 as source).
    read_as: str | None = None


def _get(values: Mapping[str, str], name: str) -> str:
    return (values.get(name) or "").strip()


_STREET_KIND = re.compile(
    r"(?i)^(str(ada)?|bd|b-dul|bulevardul|calea|aleea|ale|al|șos|sos|șoseaua|soseaua|splaiul|"
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
    room = None
    if street and (split := split_room(street)):  # "Ale. Teilor, camera 1": after the apartment
        street, room = split
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
            parts.append(value if value.casefold().startswith("cam") else f"{label} {value}")
    if room:
        parts.append(room)
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


def _activity_at_office(values: Mapping[str, str]) -> bool:
    return bool(_get(values, "activities_at_office"))


def _activities_at_office(values: Mapping[str, str]) -> str | None:
    """Anexa 4, 3.1: the activities of the company, when they are carried out at the
    registered office."""
    if not _activity_at_office(values):
        return None
    return _get(values, "caen_activities") or None


def _activities_at_third_parties(values: Mapping[str, str]) -> str | None:
    """Anexa 4, 3.2: the activities of the company (the main one first), unless they are
    carried out at the registered office, then the other activities at third parties (each
    class once). A registered office without activity is the usual case."""
    lines = _get(values, "caen_third_party").splitlines()
    if not _activity_at_office(values):
        lines = _get(values, "caen_activities").splitlines() + lines
    kept: dict[str, str] = {}
    for line in filter(None, (line.strip() for line in lines)):
        kept.setdefault(line.split()[0], line)
    return "\n".join(kept.values()) or None


def _main_class(values: Mapping[str, str]) -> str | None:
    rows = _caen_rows(values)
    return rows[0][0] if rows and re.fullmatch(r"\d{4}", rows[0][0]) else None


def _main_activity_name(values: Mapping[str, str]) -> str | None:
    rows = _caen_rows(values)
    return rows[0][1] or None if rows else None


def _secondary_activity_lines(values: Mapping[str, str]) -> str | None:
    """``— clasa CAEN 4725 și denumirea activității Comerț cu amănuntul al băuturilor;``, one
    line per secondary activity (the last one ends the list with a full stop)."""
    rows = _caen_rows(values)[1:]
    lines = [f"— clasa CAEN {code} și denumirea activității {name}".rstrip() for code, name in rows]
    return "\n".join(f"{line}{'.' if i == len(lines) - 1 else ';'}" for i, line in enumerate(lines))


def with_de(count: float) -> str:
    """``50 de`` (părți sociale), ``10`` (părți sociale), ``120 de``, ``101``: Romanian puts "de"
    after a number whose last two digits make 20 or more, or are 00."""
    number = format_amount(count)
    rest = int(count) % 100
    return f"{number} de" if rest >= 20 or (rest == 0 and count >= 100) else number


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


BLANK = "_" * 20
# What identifies a person in an act: name, CNP, domicile, birth and identity card.
IDENTITY = (
    "last_name", "first_name", "cnp", "city", "street", "street_number", "building", "entrance",
    "floor", "apartment", "region", "country", "citizenship", "place_of_birth", "birth_county",
    "birth_country", "date_of_birth", "sex", "id_type", "id_series", "id_number", "id_issued_by",
    "id_issue_date", "id_expiry_date",
)  # fmt: skip


def person_name(values: Mapping[str, str], prefix: str = "") -> str:
    """``POPESCU ION``: family name first, in capitals, as acts write it."""
    names = (_get(values, prefix + "last_name"), _get(values, prefix + "first_name"))
    return " ".join(name.upper() for name in names if name)


def identification(values: Mapping[str, str], prefix: str = "") -> str | None:
    """The identification clause of acts and statements: ``POPESCU ION, CNP ..., cu domiciliul
    în ..., țara România, cetățenia Română, născut în ..., jud. ..., țara România, la data de
    ..., identificat prin CI, seria AX, nr. ..., emisă de ..., la data de ..., valabilă până la
    data de ...``. What is not known is left blank, to be filled in by hand."""
    name = person_name(values, prefix)
    if not name and not _get(values, prefix + "cnp"):
        return None

    def get(field: str) -> str:
        return _get(values, prefix + field) or BLANK

    born, identified = _agreeing("născut", prefix)(values), _agreeing("identificat", prefix)(values)
    return (
        f"{name or BLANK}, CNP {get('cnp')}, cu domiciliul în "
        f"{address_line(values, prefix) or BLANK}, țara {get('country')}, cetățenia "
        f"{get('citizenship')}, {born} în {get('place_of_birth')}, jud. {get('birth_county')}, "
        f"țara {get('birth_country')}, la data de {get('date_of_birth')}, {identified} prin "
        f"{get('id_type')}, seria {get('id_series')}, nr. {get('id_number')}, emisă de "
        f"{get('id_issued_by')}, la data de {get('id_issue_date')}, valabilă până la data de "
        f"{get('id_expiry_date')}"
    )


def _with_role(values: Mapping[str, str], roles: tuple[str, ...]) -> str | None:
    """The prefix of the first person having one of ``roles``."""
    return next((p for p in PERSON_PREFIXES if any(_get(values, p + role) for role in roles)), None)


def associate_prefix(values: Mapping[str, str]) -> str | None:
    """The sole associate: the person marked as associate (or holding shares); when nobody is
    marked, person 1."""
    found = _with_role(values, ("associate", "shares"))
    if found is None and (person_name(values) or _get(values, "cnp")):
        return ""
    return found


def administrator_prefix(values: Mapping[str, str]) -> str | None:
    """The administrator: the person with a board role; when nobody is marked, the associate."""
    found = _with_role(values, ("board_role",))
    return found if found is not None else associate_prefix(values)


def _of(choose: Callable[[Mapping[str, str]], str | None], render) -> Compute:
    def compute(values: Mapping[str, str]) -> str | None:
        prefix = choose(values)
        return render(values, prefix) if prefix is not None else None

    return compute


_ROLE_INPUTS = tuple(
    prefix + name
    for prefix in PERSON_PREFIXES
    for name in ("associate", "shares", "board_role", *IDENTITY)
)


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
            f"{person}: „{word}”, acordat cu sexul persoanei",
            (prefix + "cnp",),
            (),
            _agreeing(word, prefix),
        )
        for name, word in (
            ("born", "născut"),
            ("identified", "identificat"),
            ("domiciled", "domiciliat"),
        )
    ]
    return [
        *words,
        Computed(
            prefix + "domicile_line",
            f"{person}: domiciliul (compus)",
            address,
            (prefix + "city",),
            lambda values: address_line(values, prefix),
        ),
        Computed(
            prefix + "shares_value",
            f"{person}: valoarea părților sociale (lei)",
            (prefix + "shares", "share_capital", "share_count"),
            (prefix + "shares", "share_capital", "share_count"),
            shares_value,
        ),
        Computed(
            prefix + "shares_percent",
            f"{person}: cota din capitalul social (%)",
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
        for computed in _person_computed(prefix, f"Persoana {index}")
    ),
    Computed(
        "company_seat_line",
        "Sediul social (compus)",
        _COMPANY_ADDRESS,
        ("company_city", "company_street", "company_street_number", "company_county"),
        _company_seat,
    ),
    Computed(
        "share_capital_amount",
        "Capitalul social (suma)",
        ("share_capital",),
        ("share_capital",),
        lambda values: format_amount(v) if (v := _number(values, "share_capital")) else None,
    ),
    Computed(
        "share_value",
        "Valoarea nominală a unei părți sociale (lei)",
        ("share_capital", "share_count"),
        ("share_capital", "share_count"),
        lambda values: format_amount(v) if (v := share_value(values)) else None,
    ),
    Computed(
        "main_activity",
        "Activitatea principală (primul rând CAEN)",
        ("caen_activities",),
        ("caen_activities",),
        lambda values: _caen_line(rows[0]) if (rows := _caen_rows(values)) else None,
    ),
    Computed(
        "main_caen_group",
        "Grupa CAEN a activității principale",
        ("caen_activities",),
        ("caen_activities",),
        _main_group,
    ),
    Computed(
        "main_caen_class",
        "Clasa CAEN a activității principale",
        ("caen_activities",),
        ("caen_activities",),
        _main_class,
    ),
    Computed(
        "main_activity_name",
        "Denumirea activității principale (primul rând CAEN)",
        ("caen_activities",),
        ("caen_activities",),
        _main_activity_name,
    ),
    Computed(
        "secondary_activity_lines",
        "Activitățile secundare, câte una pe rând (actul constitutiv SRL)",
        ("caen_activities",),
        (),
        _secondary_activity_lines,
    ),
    Computed(
        "share_count_de",
        "Numărul de părți sociale, cu „de” când este necesar (50 de părți sociale)",
        ("share_count",),
        ("share_count",),
        lambda values: with_de(v) if (v := _number(values, "share_count")) else None,
    ),
    Computed(
        "associate_identification",
        "Asociatul unic, cu datele de identificare",
        _ROLE_INPUTS,
        # person 1 is the associate, or the administrator when the associate is another person
        ("last_name", "first_name", "cnp"),
        _of(associate_prefix, identification),
    ),
    Computed(
        "associate_name",
        "Numele asociatului unic",
        _ROLE_INPUTS,
        (),
        _of(associate_prefix, lambda values, prefix: person_name(values, prefix) or None),
    ),
    Computed(
        "administrator_identification",
        "Administratorul, cu datele de identificare",
        _ROLE_INPUTS,
        (),
        _of(administrator_prefix, identification),
    ),
    Computed(
        "caen_at_office",
        "Anexa 4, pct. 3.1: activitățile desfășurate la sediul social",
        ("caen_activities", "activities_at_office"),
        (),
        _activities_at_office,
        read_as="caen_activities",
    ),
    Computed(
        "caen_at_third_parties",
        "Anexa 4, pct. 3.2: activitățile desfășurate la terți",
        ("caen_activities", "activities_at_office", "caen_third_party"),
        (),
        _activities_at_third_parties,
        read_as="caen_third_party",
    ),
    Computed(
        "secondary_activities",
        "Activitățile secundare (celelalte rânduri CAEN)",
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
