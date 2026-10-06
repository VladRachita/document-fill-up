"""Romanian document knowledge: counties, CNP, dates, sex and addresses in the style printed on
identity cards (``Jud.AB Mun.Alba Iulia Str.Mihai Viteazul nr.12 bl.A2 sc.1 et.3 ap.10``)."""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from functools import cache

from docfill.lexicon import (
    ROMANIAN_MARKS,
    gazetteer,
    given_names,
    plain,
    street_words,
    surnames,
    with_case_of,
)

# Two-letter county codes printed on identity cards and car plates.
COUNTY_CODES = {
    "AB": "Alba",
    "AR": "Arad",
    "AG": "Argeș",
    "BC": "Bacău",
    "BH": "Bihor",
    "BN": "Bistrița-Năsăud",
    "BT": "Botoșani",
    "BV": "Brașov",
    "BR": "Brăila",
    "B": "București",
    "BZ": "Buzău",
    "CS": "Caraș-Severin",
    "CL": "Călărași",
    "CJ": "Cluj",
    "CT": "Constanța",
    "CV": "Covasna",
    "DB": "Dâmbovița",
    "DJ": "Dolj",
    "GL": "Galați",
    "GR": "Giurgiu",
    "GJ": "Gorj",
    "HR": "Harghita",
    "HD": "Hunedoara",
    "IL": "Ialomița",
    "IS": "Iași",
    "IF": "Ilfov",
    "MM": "Maramureș",
    "MH": "Mehedinți",
    "MS": "Mureș",
    "NT": "Neamț",
    "OT": "Olt",
    "PH": "Prahova",
    "SM": "Satu Mare",
    "SJ": "Sălaj",
    "SB": "Sibiu",
    "SV": "Suceava",
    "TR": "Teleorman",
    "TM": "Timiș",
    "TL": "Tulcea",
    "VS": "Vaslui",
    "VL": "Vâlcea",
    "VN": "Vrancea",
}

# County part (JJ) of the CNP.
CNP_COUNTIES = {
    1: "Alba",
    2: "Arad",
    3: "Argeș",
    4: "Bacău",
    5: "Bihor",
    6: "Bistrița-Năsăud",
    7: "Botoșani",
    8: "Brașov",
    9: "Brăila",
    10: "Buzău",
    11: "Caraș-Severin",
    12: "Cluj",
    13: "Constanța",
    14: "Covasna",
    15: "Dâmbovița",
    16: "Dolj",
    17: "Galați",
    18: "Gorj",
    19: "Harghita",
    20: "Hunedoara",
    21: "Ialomița",
    22: "Iași",
    23: "Ilfov",
    24: "Maramureș",
    25: "Mehedinți",
    26: "Mureș",
    27: "Neamț",
    28: "Olt",
    29: "Prahova",
    30: "Satu Mare",
    31: "Sălaj",
    32: "Sibiu",
    33: "Suceava",
    34: "Teleorman",
    35: "Timiș",
    36: "Tulcea",
    37: "Vaslui",
    38: "Vâlcea",
    39: "Vrancea",
    40: "București",
    41: "București",
    42: "București",
    43: "București",
    44: "București",
    45: "București",
    46: "București",
    47: "București",
    48: "București",
    51: "Călărași",
    52: "Giurgiu",
}

MONTHS = {
    "ian": 1,
    "ianuarie": 1,
    "jan": 1,
    "january": 1,
    "feb": 2,
    "februarie": 2,
    "february": 2,
    "mar": 3,
    "martie": 3,
    "march": 3,
    "apr": 4,
    "aprilie": 4,
    "april": 4,
    "mai": 5,
    "may": 5,
    "iun": 6,
    "iunie": 6,
    "jun": 6,
    "june": 6,
    "iul": 7,
    "iulie": 7,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "septembrie": 9,
    "september": 9,
    "oct": 10,
    "octombrie": 10,
    "october": 10,
    "noi": 11,
    "nov": 11,
    "noiembrie": 11,
    "november": 11,
    "dec": 12,
    "decembrie": 12,
    "december": 12,
}


def _fold(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", text) if unicodedata.category(c) != "Mn"
    ).lower()


_COUNTY_BY_FOLDED = {_fold(name): name for name in COUNTY_CODES.values()}
_CODE_BY_COUNTY = {name: code for code, name in COUNTY_CODES.items()}


# A county code as OCR misreads it: the hook of J becomes ) } ], the capital I is a bar.
_COUNTY_CODE_OCR = str.maketrans(
    {")": "J", "}": "J", "]": "J", "|": "I", "!": "I", "1": "I", "l": "I", "0": "O", "$": "S"}
)


_COUNTY_TOKEN = re.compile(r"(?i:\bjud)\.?\s*(?P<code>[A-Za-z|!1)}\]]{2})(?=[\s,.;]|$)")


def repair_county_codes(text: str) -> str:
    """``Jud.C} Or.Huedin`` -> ``Jud.CJ Or.Huedin``: a county code that OCR misread is put back
    when the letters it can only be make a real county code."""

    def fix(match: re.Match[str]) -> str:
        code = match["code"].translate(_COUNTY_CODE_OCR).upper()
        if code not in COUNTY_CODES:
            return match.group()
        return match.group()[: match.start("code") - match.start()] + code

    return _COUNTY_TOKEN.sub(fix, text)


def county_name(text: str) -> str | None:
    """``AB`` / ``Alba`` / ``jud. alba`` -> ``Alba``; ``None`` if it is not a Romanian county.
    A code whose letters OCR mixed up (``C}`` for ``CJ``, ``!S`` for ``IS``) is read as the
    county code it can only be."""
    cleaned = re.sub(r"^(?:jud(?:e[tț]ul?)?\.?)\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = cleaned.strip(" .,")
    if cleaned.upper() in COUNTY_CODES:
        return COUNTY_CODES[cleaned.upper()]
    if len(cleaned) == 2:
        repaired = cleaned.translate(_COUNTY_CODE_OCR).upper()
        if repaired in COUNTY_CODES:
            return COUNTY_CODES[repaired]
    return _COUNTY_BY_FOLDED.get(_fold(cleaned))


def county_code(text: str | None) -> str | None:
    """``Cluj`` / ``CJ`` / ``jud. cluj`` / ``Sector 2`` -> ``CJ`` / ``CJ`` / ``CJ`` / ``B``;
    ``None`` if it is not a Romanian county."""
    if not text:
        return None
    if re.match(r"\s*sec(?:tor(?:ul)?)?\b", text, re.IGNORECASE):
        return "B"
    name = county_name(text)
    return _CODE_BY_COUNTY.get(name) if name else None


# --------------------------------------------------------------------------- CNP

_CNP_WEIGHTS = "279146358279"


@dataclass(frozen=True)
class CNPInfo:
    valid: bool
    reason: str = ""
    sex: str | None = None
    birth_date: date | None = None
    county: str | None = None


def check_cnp(value: str) -> CNPInfo:
    """Validate a Romanian personal numeric code (format S AA LL ZZ JJ NNN C)."""
    cnp = re.sub(r"\s", "", value)
    if not re.fullmatch(r"\d{13}", cnp):
        return CNPInfo(False, "un CNP are exact 13 cifre")
    control = sum(int(d) * int(w) for d, w in zip(cnp[:12], _CNP_WEIGHTS, strict=True)) % 11
    if (1 if control == 10 else control) != int(cnp[12]):
        return CNPInfo(
            False, "cifra de control nu corespunde (greșeală de tastare sau de citire OCR?)"
        )
    s = int(cnp[0])
    century = {1: 1900, 2: 1900, 3: 1800, 4: 1800, 5: 2000, 6: 2000}.get(s)
    if century is None:  # 7/8 residents, 9 foreigners: century not encoded
        yy = int(cnp[1:3])
        century = 2000 if yy <= date.today().year % 100 else 1900
    try:
        birth = date(century + int(cnp[1:3]), int(cnp[3:5]), int(cnp[5:7]))
    except ValueError:
        return CNPInfo(False, "data nașterii codificată în CNP nu există")
    sex = "M" if s in (1, 3, 5, 7) else "F" if s in (2, 4, 6, 8) else None
    return CNPInfo(True, sex=sex, birth_date=birth, county=CNP_COUNTIES.get(int(cnp[7:9])))


def cnp_control_digit(first12: str) -> str:
    control = sum(int(d) * int(w) for d, w in zip(first12, _CNP_WEIGHTS, strict=True)) % 11
    return "1" if control == 10 else str(control)


# --------------------------------------------------------------------------- dates / sex


def format_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def parse_date(text: str, pivot_future_years: int = 15) -> date | None:
    """Parse dates written the Romanian way (``14.11.1987``, ``14/11/87``, ``14 noiembrie
    1987``) and ISO ``1987-11-14``. Two-digit years up to ``pivot_future_years`` ahead are 20xx."""
    text = text.strip()
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        year, month, day = (int(g) for g in match.groups())
    else:
        match = re.fullmatch(r"(\d{1,2})\s*[./-]\s*(\d{1,2})\s*[./-]\s*(\d{2}|\d{4})", text)
        if match:
            day, month, year = (int(g) for g in match.groups())
        else:
            match = re.fullmatch(r"(\d{1,2})\s+([A-Za-zăâîșțĂÂÎȘȚ]+)\.?\s+(\d{2}|\d{4})", text)
            if not match or _fold(match.group(2)) not in MONTHS:
                return None
            day, month, year = int(match.group(1)), MONTHS[_fold(match.group(2))], int(match[3])
        if year < 100:
            current = date.today().year
            year += 2000 if year <= (current + pivot_future_years) % 100 else 1900
    try:
        return date(year, month, day)
    except ValueError:
        return None


def normalize_date(text: str) -> str | None:
    parsed = parse_date(text)
    return format_date(parsed) if parsed else None


_SEX_WORDS = {
    "m": "M",
    "masculin": "M",
    "barbatesc": "M",
    "barbat": "M",
    "male": "M",
    "b": "M",
    "f": "F",
    "feminin": "F",
    "femeiesc": "F",
    "femeie": "F",
    "female": "F",
}


def normalize_sex(text: str) -> str | None:
    return _SEX_WORDS.get(_fold(text).strip(" ./"))


# --------------------------------------------------------------------------- addresses

_STOP_WORDS = (
    r"(?:Str|Strada|Bd|B-dul|Bulevardul|Calea|Aleea|Ale|Al|Sos|Șos|Soseaua|Șoseaua|Spl|Splaiul|"
    r"Piata|Piața|Intr|Intrarea|Drumul|Sec|Sector|Jud|Judet|Județ|Sat|Satul|Com|Comuna|Mun|"
    r"Municipiul|Oras|Oraș|Orasul|Orașul|Nr|Bl|Sc|Et|Ap)\b"
)
_NAME_WORD = rf"(?!{_STOP_WORDS})[A-ZĂÂÎȘȚ][\wăâîșțĂÂÎȘȚ\-]*"
_LOCALITY_PREFIX = re.compile(
    r"\b(?P<kind>Mun(?:icipiul)?|Ora[sș](?:ul)?|Or|Com(?:una)?|Sat(?:ul)?)(?:\.\s*|\s+)"
    rf"(?P<name>{_NAME_WORD}(?:\s+{_NAME_WORD})*)"
)
_OCR_CODE = r"[A-Za-z|!1)}\]]"
_COUNTY = re.compile(  # "jud.", "județul", "judeţul" (cedilla), "judeftil" (a scanner's OCR)
    r"\b(?i:jud(?:e[\wțţ!|]{0,3}l?)?)(?:\.\s*|\s+)"
    rf"(?P<county>{_OCR_CODE}{_OCR_CODE}(?=[\s,.;]|$)"  # the code, possibly misread: "C}"
    r"|[A-Z]{1,2}\b"
    r"|[A-ZĂÂÎȘȚ][\wăâîșț\-]+(?:\s[A-ZĂÂÎȘȚ][\wăâîșț]+)?)",
)
_SECTOR = re.compile(r"\bSec(?:tor(?:ul)?)?\.?\s*(?P<sector>[1-6])\b", re.IGNORECASE)
_STREET = re.compile(
    r"\b(?P<kind>Str(?:ada)?|Bd|B-dul|Bulevardul|Calea|Aleea|Ale(?=\.)|Al(?=\.)|[SȘ]os(?:eaua)?|Spl(?:aiul)?|"
    r"Pia[tț]a|P-[tț]a|Intr(?:area)?|Drumul|Fund(?:[aă]tura)?)(?:\.\s*|\s+)"
    r"(?P<name>[^\s,.].*?)(?=\s*,?\s*\b(?:nr|bl|sc|et|ap|cam|camera)\b\.?|\s*,|\s*$)",
    re.IGNORECASE,
)
_PARTS = {
    "street_number": re.compile(r"\bnr\.?\s*(?P<v>\d+[A-Za-z]?(?:\s*[-/]\s*\d+[A-Za-z]?)?)", re.I),
    # "BI. 69": the l of "Bl." read as a capital I by OCR
    "building": re.compile(
        r"\b(?:bl(?:oc)?|(?-i:B[I1](?=\.)))\.?\s*(?P<v>[A-Za-z0-9][\w\-/]*)", re.I
    ),
    "entrance": re.compile(r"\bsc(?:ara)?\.?\s*(?P<v>[A-Za-z0-9]+)", re.I),
    # "Et. VII" as OCR may read it: "Et. Vil", "Et. Vll"
    "floor": re.compile(
        r"\bet(?:aj)?\.?\s*(?P<v>\d+|parter|P|D|M|(?-i:[IVX][IVXil1]{0,3})(?=[\s,.;]|$))\b",
        re.I,
    ),
    "apartment": re.compile(
        r"\b(?:ap(?:artament)?\.?\s*(?P<v>\d+[A-Za-z]?)|(?P<room>cam(?:era)?\.?\s*\d+\w*))", re.I
    ),
}
_ROOM = re.compile(r"\bcam(?:era)?\.?\s*\d+\w*", re.I)
_KEEP_STREET_KIND = {
    "bd": "Bd.",
    "b-dul": "Bd.",
    "bulevardul": "Bd.",
    "calea": "Calea",
    "aleea": "Aleea",
    "ale": "Ale.",
    "al": "Aleea",
    "sos": "Șos.",
    "șos": "Șos.",
    "soseaua": "Șos.",
    "șoseaua": "Șos.",
    "spl": "Splaiul",
    "splaiul": "Splaiul",
    "piata": "Piața",
    "piața": "Piața",
    "p-ta": "Piața",
    "p-ța": "Piața",
    "intr": "Intrarea",
    "intrarea": "Intrarea",
    "drumul": "Drumul",
    "fund": "Fundătura",
    "fundatura": "Fundătura",
    "fundătura": "Fundătura",
}


def looks_romanian_address(text: str) -> bool:
    return bool(
        _COUNTY.search(text)
        or _LOCALITY_PREFIX.search(text)
        or _STREET.search(text)
        and _PARTS["street_number"].search(text)
    )


def format_locality(kind: str, name: str) -> str:
    kind = _fold(kind).rstrip(".")
    prefix = {
        "mun": "Mun.",
        "municipiul": "Mun.",
        "oras": "Oraș",
        "orasul": "Oraș",
        "or": "Oraș",
        "com": "Com.",
        "comuna": "Com.",
        "sat": "Sat",
        "satul": "Sat",
    }.get(kind, "")
    return f"{prefix} {name.strip()}".strip()


def parse_ro_address(text: str) -> dict[str, str]:
    """Split a Romanian address into county, city, street, number, block, staircase, floor,
    apartment (and sector for Bucharest; ``room`` for the room of an apartment, ``ap. 29,
    camera 1``). Only the parts present are returned."""
    text = " ".join(text.replace("\n", ", ").split())
    result: dict[str, str] = {}
    if match := _COUNTY.search(text):
        county = county_name(match["county"])
        if county:
            result["county"] = county
    if match := _SECTOR.search(text):
        result["sector"] = f"Sector {match['sector']}"
    code = _CODE_BY_COUNTY.get(result.get("county", "")) or ("B" if "sector" in result else None)
    localities = [
        format_locality(m["kind"], spell_locality(m["kind"], _without_noise(m["name"], code), code))
        for m in _LOCALITY_PREFIX.finditer(text)
    ]
    if localities:  # "Com. Hărman, Sat Podu Oltului"
        result["city"] = restore_diacritics(", ".join(localities))
    if match := _STREET.search(text):
        name = restore_diacritics(match["name"].strip(" ,."))
        kind = _KEEP_STREET_KIND.get(_fold(match["kind"]).rstrip("."))
        result["street"] = f"{kind} {name}" if kind else name
    for part, pattern in _PARTS.items():
        if match := pattern.search(text):
            if match.groupdict().get("room"):
                result[part] = " ".join(match["room"].split()).lower()
            else:
                result[part] = match["v"].replace(" ", "")
    if re.fullmatch(r"[IVX][IVXil1]*", floor := result.get("floor", "")):
        result["floor"] = floor.translate(str.maketrans("il1", "III"))
    # "ap. 29, camera 1": the room of an apartment (a registered office often is one room)
    if (room := _ROOM.search(text)) and (room := " ".join(room.group().split()).lower()) != (
        result.get("apartment")
    ):
        result["room"] = room
    if "county" in result or "city" in result:
        result.setdefault("country", "România")
    return result


def compose_street_line(values: dict[str, str]) -> str | None:
    """``Str. Florilor nr. 5, bl. A2, sc. 1, et. 3, ap. 10`` from its parts."""
    street = values.get("street")
    if not street:
        return None
    parts = [
        street
        if re.match(r"(?i)(bd|calea|aleea|ale\.|șos|splaiul|piața|intrarea|drumul)", street)
        else f"Str. {street}"
    ]
    labels = (
        ("street_number", "nr."),
        ("building", "bl."),
        ("entrance", "sc."),
        ("floor", "et."),
        ("apartment", "ap."),
    )
    for key, label in labels:
        if values.get(key):
            parts.append(f"{label} {values[key]}")
    return ", ".join([parts[0] + (f" {parts[1]}" if len(parts) > 1 else ""), *parts[2:]])


CITIZENSHIP_BY_CODE = {"ROU": "Română", "MDA": "Moldovenească"}

CITIZENSHIP_NORMAL = {
    "romana": "Română",
    "roumaine": "Română",
    "romanian": "Română",
    "rou": "Română",
    "moldoveneasca": "Moldovenească",
}

# County seats and larger towns, used to restore diacritics lost by OCR ("Fagaras" -> "Făgăraș").
_PLACES = """
Alba Iulia, Arad, Pitești, Bacău, Oradea, Bistrița, Botoșani, Brașov, Brăila, București, Buzău,
Reșița, Călărași, Cluj-Napoca, Constanța, Sfântu Gheorghe, Târgoviște, Craiova, Galați, Giurgiu,
Târgu Jiu, Miercurea Ciuc, Deva, Slobozia, Iași, Buftea, Baia Mare, Drobeta-Turnu Severin,
Târgu Mureș, Piatra Neamț, Slatina, Ploiești, Satu Mare, Zalău, Sibiu, Suceava, Alexandria,
Timișoara, Tulcea, Vaslui, Râmnicu Vâlcea, Focșani, Onești, Comănești, Moinești, Mediaș,
Făgăraș, Săcele, Codlea, Râșnov, Zărnești, Câmpina, Bârlad, Pașcani, Roman, Turda, Dej, Gherla,
Câmpia Turzii, Hunedoara, Petroșani, Lugoj, Medgidia, Mangalia, Năvodari, Sighișoara, Reghin,
Câmpulung, Câmpulung Moldovenesc, Curtea de Argeș, Caransebeș, Fetești, Tecuci, Rădăuți, Fălticeni,
Vatra Dornei, Adjud,
Mărășești, Otopeni, Voluntari, Pantelimon, Popești-Leordeni, Chitila, Bragadiru, Mioveni,
Târgu Neamț, Huși, Negrești, Dorohoi, Sighetu Marmației, Borșa, Vișeu de Sus, Carei, Beiuș,
Salonta, Marghita, Aiud, Blaj, Sebeș, Cugir, Orăștie, Brad, Vulcan, Lupeni, Petrila, Făget,
Buziaș, Jimbolia, Sânnicolau Mare, Lipova, Ineu, Motru, Băilești, Calafat, Caracal, Balș,
Drăgășani, Horezu, Rovinari, Târgu Cărbunești, Zimnicea, Roșiori de Vede, Roșiorii de Vede,
Turnu Măgurele,
Oltenița, Urziceni, Rm. Sărat, Râmnicu Sărat, Tulcea, Măcin, Babadag, Cernavodă, Eforie,
Techirghiol, Odobești, Panciu, Târgu Secuiesc, Odorheiu Secuiesc, Gheorgheni, Toplița,
Târgu Frumos, Hârlău, Bicaz, Sângeorz-Băi, Năsăud, Beclean, Șimleu Silvaniei, Jibou,
Tășnad, Negrești-Oaș, Moldova Nouă, Oravița, Anina, Bocșa, Hațeg, Simeria, Călan, Ocna Mureș,
Luduș, Sovata, Târnăveni, Cisnădie, Avrig, Agnita, Dumbrăveni
"""
_PLACE_BY_FOLDED = {
    _fold(p.strip()): p.strip() for p in _PLACES.replace("\n", " ").split(",") if p.strip()
}
_KNOWN_TOWNS = frozenset(_PLACE_BY_FOLDED)  # municipalities and towns, without the counties
_PLACE_BY_FOLDED.update({_fold(name): name for name in COUNTY_CODES.values()})


@cache
def _places() -> tuple[dict[str, str], int]:
    """The places whose diacritics can be restored, by their accent-free name, and the number of
    words of the longest: the list above, the counties and every municipality, town and commune
    of the register that is spelled one way wherever it is (``Săcălaz``)."""
    by_key = dict(_PLACE_BY_FOLDED)
    spellings: dict[str, set[str]] = defaultdict(set)
    for locality in gazetteer().entries("MOC"):
        spellings[_fold(locality.name)].add(locality.name)
    for key, names in spellings.items():
        if len(names) == 1 and key not in by_key:
            by_key[key] = next(iter(names))
    return by_key, max(len(name.split()) for name in by_key.values())


def _restore_words(text: str) -> str:
    """The diacritics of the names of people and the street words that the lists spell with
    them (``Stefan`` -> ``Ștefan``, ``Libertatii`` -> ``Libertății``)."""

    def fix(match: re.Match[str]) -> str:
        word = match.group()
        for words in (street_words(), given_names(), surnames()):
            found = words.lookup(word)
            if found.status == "restorable" and found.spelling:
                return found.spelling
            if found.status in ("exact", "known"):
                break
        return word

    return re.sub(r"[^\W\d_]{4,}", fix, text)


def restore_diacritics(text: str) -> str:
    """Replace known place names written without (or with wrong) diacritics by their correct
    spelling: "Fagaras" / "Focşani" (cedilla) -> "Făgăraș" / "Focșani"; the same for the names
    of people and the words of street names ("Str. Stefan cel Mare" -> "Str. Ștefan cel Mare").

    Only whole words matching a known name accent-insensitively are replaced, so unknown names
    and names already written correctly are left alone.
    """
    places, longest = _places()
    tokens = re.split(r"(\s+|[.,;:/()])", text)
    words = [(i, t) for i, t in enumerate(tokens) if t and not re.fullmatch(r"\s+|[.,;:/()]", t)]
    position = 0
    while position < len(words):
        for size in range(min(longest, len(words) - position), 0, -1):
            chunk = words[position : position + size]
            first, last = chunk[0][0], chunk[-1][0]
            original = "".join(tokens[first : last + 1])
            proper = places.get(_fold(" ".join(t for _, t in chunk)))
            if proper and _fold(original) == _fold(proper):
                if original.lower() != proper.lower():  # missing or wrong diacritics
                    fixed = proper.upper() if original.isupper() else proper
                    tokens[first : last + 1] = [fixed] + [""] * (last - first)
                position += size
                break
        else:
            position += 1
    return _restore_words("".join(tokens))


# --------------------------------------------------------------------------- doubtful places

# The letter the register uses for the kind of a locality, by the prefix a card prints.
_KIND_OF_PREFIX = {"mun": "M", "oras": "O", "com": "C", "sat": "S"}
_PLACE_PART = re.compile(r"(?P<kind>Mun\.|Oraș|Com\.|Sat)\s+(?P<name>[^,]+?)\s*$")


def _marks(text: str) -> int:
    return sum(char in ROMANIAN_MARKS for char in text)


def _without_noise(name: str, county: str | None) -> str:
    """``Brașov Sr`` -> ``Brașov``: one or two letters after a locality, that the register has
    without them, are specks of the card read as letters."""
    words = name.split()
    if len(words) < 2 or len(words[-1]) > 2 or gazetteer().find(name, county):
        return name
    rest = " ".join(words[:-1])
    return rest if gazetteer().find(rest, county) else name


def spell_locality(kind_prefix: str, name: str, county: str | None = None) -> str:
    """The name of a locality as the register spells it: ``Harman`` -> ``Hărman``, ``Cluj Napoca``
    -> ``Cluj-Napoca``. Only when the register has one such locality in the county (anywhere in
    the country when the county is not known); the name is left as it was read otherwise."""
    letter = _KIND_OF_PREFIX.get(_fold(kind_prefix).rstrip("."))
    found = gazetteer().find(name, county)
    found = [item for item in found if item.kind == letter] or found
    spellings = {item.name for item in found}
    if len(spellings) != 1:
        return name
    spelling = next(iter(spellings))
    # a register spelling without diacritics never takes away those that were read
    return name if _marks(name) > _marks(spelling) else with_case_of(name, spelling)


@dataclass(frozen=True)
class PlaceDoubt:
    problem: str
    suggestions: tuple[str, ...] = ()  # whole values to offer instead, in the form of the field
    completed: bool = False  # the value is the beginning of the suggestions: it was cut off


def _part_doubt(
    prefix: str, name: str, birth: bool, county: str | None
) -> tuple[str, list[str], bool] | None:
    """The doubt about one locality (``Mun. Sib``), the names that could replace it and whether
    it is the beginning of them."""
    key = _fold(name).strip(" .")
    if key.startswith("bucuresti"):
        if birth and key == "bucuresti":
            return (
                "Lipsește sectorul municipiului București: comparați cu cartea de identitate",
                [],
                False,
            )
        return None
    letter = _KIND_OF_PREFIX.get(_fold(prefix).rstrip("."), "")
    register = gazetteer()
    # a status changes over the years (a town becomes a municipality): "Mun." and "Oraș" accept
    # any of the three, but neither a village, which no card prints with them
    accepted = "MOC" if letter in ("M", "O") else "MOCS"
    if register.find(name, county, accepted):
        return None
    where = f"județul {COUNTY_CODES.get(county or '', county)}" if county else None
    elsewhere = register.find(name, None, accepted)
    if not elsewhere and key in _KNOWN_TOWNS and letter in ("M", "O"):
        return None  # a name the register spells differently (an abbreviation, a variant)
    # a name that is in the register, but in another county: the county or the name was misread
    if county and elsewhere:
        others = sorted({COUNTY_CODES.get(item.county, item.county) for item in elsewhere})
        return (
            f"Nu este o localitate din {where} (există una cu acest nume în: "
            f"{', '.join(others)}): verificați județul și localitatea",
            [],
            False,
        )
    # the kind printed in front of the name comes first: "Mun." is a municipality, if one fits
    kinds = {"M": ("M", "MO"), "O": ("O", "MO"), "C": ("C",), "S": ("S",)}.get(letter, ())
    if kinds == ("S",) and not county:  # too many villages to search the whole country
        kinds = ()
    cut = next((found for k in kinds if (found := register.completions(name, county, k))), [])
    if cut:
        names = list(dict.fromkeys(item.name for item in cut))
        return (
            f"Pare trunchiat pe scanare (… {names[0]}?): comparați cu cartea de identitate",
            names,
            True,
        )
    near = next((found for k in kinds if (found := register.similar(name, county, k))), [])
    if near:
        names = list(dict.fromkeys(item.name for item in near))
        scope = f"Nu este o localitate din {where}" if where else "Nu este o localitate cunoscută"
        return (
            f"{scope}: ați vrut să scrieți {names[0]}? Comparați cu cartea de identitate",
            names,
            False,
        )
    if letter == "M":  # the municipalities are a closed list that the register has in full
        return (
            "Nu este un municipiu cunoscut: verificați scrierea pe cartea de identitate",
            [],
            False,
        )
    return None


_OFFICE = re.compile(r"^(?P<office>[A-ZĂÂÎȘȚ]{3,8})\s+(?P<place>[^\d]+?)\s*$")


def office_doubt(office: str) -> PlaceDoubt | None:
    """Why the place in the name of an issuing office (``SPCLEP Cluj-Napoca``) cannot be trusted:
    it is a municipality, a town or a commune, so a name that is none of them was misread
    (``Drobeta-Tumu Severin``) or cut off. A sector of Bucharest is not a place of the register."""
    match = _OFFICE.match(office.strip())
    if not match or plain(match["place"]).startswith("sec"):
        return None
    place = match["place"].strip(" .")
    register = gazetteer()
    if len(plain(place)) < 3 or register.find(place, None, "MOC") or _fold(place) in _KNOWN_TOWNS:
        return None
    for kinds in ("M", "MO", "MOC"):
        cut = register.completions(place, None, kinds)
        if cut:
            names = list(dict.fromkeys(item.name for item in cut))
            problem = (
                f"Pare trunchiat pe scanare (… {names[0]}?): comparați cu cartea de identitate"
            )
            return PlaceDoubt(problem, tuple(f"{match['office']} {name}" for name in names), True)
    if near := register.similar(place, None, "MOC"):
        names = list(dict.fromkeys(item.name for item in near))
        problem = (
            f"Nu este o localitate cunoscută: ați vrut să scrieți {names[0]}? Comparați cu "
            "cartea de identitate"
        )
        return PlaceDoubt(problem, tuple(f"{match['office']} {name}" for name in names))
    return None


def place_doubt(locality: str, birth: bool = False, county: str | None = None) -> PlaceDoubt | None:
    """Why a locality read from a card cannot be trusted, with the names it may be instead.

    The register of localities has every municipality, town, commune and village of a county,
    so a name it does not have was misread or cut off: ``Mun. Sib`` (Sibiu), ``Mun. Cluj``
    (Cluj-Napoca, glare took the end of the line), ``Oraș Huedi``, ``Com. Harmann``. A name that
    is not in the register and looks like no name in it is left alone (it may be new). A place
    of birth in Bucharest is printed with its sector, so without one the line was cut. ``county``
    is the code a card prints (``CJ``, ``B``); without it the whole country is searched."""
    parts = [part.strip() for part in locality.split(",")]
    for index, part in enumerate(parts):
        match = _PLACE_PART.match(part)
        if not match:
            continue
        found = _part_doubt(match["kind"], match["name"], birth, county)
        if found:
            problem, names, completed = found
            suggestions = tuple(
                ", ".join([*parts[:index], f"{match['kind']} {name}", *parts[index + 1 :]])
                for name in names
            )
            return PlaceDoubt(problem, suggestions, completed)
    return None


def doubtful_place(locality: str, birth: bool = False, county: str | None = None) -> str | None:
    """The problem :func:`place_doubt` finds with a locality, or ``None``."""
    doubt = place_doubt(locality, birth, county)
    return doubt.problem if doubt else None


_ROOM_SUFFIX = re.compile(r"^(?P<street>.*?)[\s,]+(?P<room>cam(?:era)?\.?\s*\d+\w*)$", re.I)


def split_room(street: str) -> tuple[str, str] | None:
    """``Mihai Eminescu camera 2`` -> ("Mihai Eminescu", "camera 2")."""
    match = _ROOM_SUFFIX.match(street.strip())
    if not match or not match["street"]:
        return None
    return match["street"].strip(" ,"), " ".join(match["room"].split()).lower()


# --------------------------------------------------------------------------- amounts


def parse_amount(text: str) -> float | None:
    """``90.000 lei``, ``90 000``, ``1.500,50 RON``, ``200`` -> a number (Romanian notation:
    ``.`` groups thousands, ``,`` marks decimals)."""
    match = re.search(r"\d[\d .,]*", text or "")
    if not match:
        return None
    number = match.group().strip().replace(" ", "").rstrip(".,")
    if "." in number and "," in number:
        decimal = "," if number.rfind(",") > number.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        number = number.replace(thousands, "").replace(decimal, ".")
    elif re.fullmatch(r"\d{1,3}([.,]\d{3})+", number):
        number = re.sub(r"[.,]", "", number)
    else:
        number = number.replace(",", ".")
    try:
        return float(number)
    except ValueError:
        return None


def format_amount(value: float) -> str:
    """``90000`` -> ``90.000``, ``0.1`` -> ``0,1`` (Romanian notation)."""
    text = f"{value:,.2f}"
    if text.endswith(".00"):
        text = text[:-3]
    elif text.endswith("0"):
        text = text[:-1]
    return text.replace(",", " ").replace(".", ",").replace(" ", ".")


def counted(number: int, one: str, many: str) -> str:
    """``1 pagină``, ``2 pagini``, ``20 de pagini``, ``101 pagini``: Romanian puts "de" after a
    number whose last two digits make 20 or more, or are 00 (as :func:`format_amount` writes it)."""
    if number == 1:
        return f"1 {one}"
    rest = number % 100
    de = "de " if rest >= 20 or (rest == 0 and number >= 100) else ""
    return f"{format_amount(number)} {de}{many}"
