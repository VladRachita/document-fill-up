"""Romanian document knowledge: counties, CNP, dates, sex and addresses in the style printed on
identity cards (``Jud.AB Mun.Alba Iulia Str.Mihai Viteazul nr.12 bl.A2 sc.1 et.3 ap.10``)."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

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


def county_name(text: str) -> str | None:
    """``AB`` / ``Alba`` / ``jud. alba`` -> ``Alba``; ``None`` if it is not a Romanian county."""
    cleaned = re.sub(r"^(?:jud(?:e[tț]ul?)?\.?)\s*", "", text.strip(), flags=re.IGNORECASE)
    cleaned = cleaned.strip(" .,")
    if cleaned.upper() in COUNTY_CODES:
        return COUNTY_CODES[cleaned.upper()]
    return _COUNTY_BY_FOLDED.get(_fold(cleaned))


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
        return CNPInfo(False, "a CNP has exactly 13 digits")
    control = sum(int(d) * int(w) for d, w in zip(cnp[:12], _CNP_WEIGHTS, strict=True)) % 11
    if (1 if control == 10 else control) != int(cnp[12]):
        return CNPInfo(False, "control digit does not match (typing or OCR error?)")
    s = int(cnp[0])
    century = {1: 1900, 2: 1900, 3: 1800, 4: 1800, 5: 2000, 6: 2000}.get(s)
    if century is None:  # 7/8 residents, 9 foreigners: century not encoded
        yy = int(cnp[1:3])
        century = 2000 if yy <= date.today().year % 100 else 1900
    try:
        birth = date(century + int(cnp[1:3]), int(cnp[3:5]), int(cnp[5:7]))
    except ValueError:
        return CNPInfo(False, "the birth date encoded in the CNP does not exist")
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
    r"(?:Str|Strada|Bd|B-dul|Bulevardul|Calea|Aleea|Al|Sos|Șos|Soseaua|Șoseaua|Spl|Splaiul|"
    r"Piata|Piața|Intr|Intrarea|Drumul|Sec|Sector|Jud|Judet|Județ|Sat|Satul|Com|Comuna|Mun|"
    r"Municipiul|Oras|Oraș|Orasul|Orașul|Nr|Bl|Sc|Et|Ap)\b"
)
_NAME_WORD = rf"(?!{_STOP_WORDS})[A-ZĂÂÎȘȚ][\wăâîșțĂÂÎȘȚ\-]*"
_LOCALITY_PREFIX = re.compile(
    r"\b(?P<kind>Mun(?:icipiul)?|Ora[sș](?:ul)?|Or\.|Com(?:una)?|Sat(?:ul)?)(?:\.\s*|\s+)"
    rf"(?P<name>{_NAME_WORD}(?:\s+{_NAME_WORD})*)"
)
_COUNTY = re.compile(
    r"\b(?i:jud(?:e[tț]ul?)?)(?:\.\s*|\s+)"
    r"(?P<county>[A-Z]{1,2}\b|[A-ZĂÂÎȘȚ][\wăâîșț\-]+(?:\s[A-ZĂÂÎȘȚ][\wăâîșț]+)?)",
)
_SECTOR = re.compile(r"\bSec(?:tor(?:ul)?)?\.?\s*(?P<sector>[1-6])\b", re.IGNORECASE)
_STREET = re.compile(
    r"\b(?P<kind>Str(?:ada)?|Bd|B-dul|Bulevardul|Calea|Aleea|Al(?=\.)|[SȘ]os(?:eaua)?|Spl(?:aiul)?|"
    r"Pia[tț]a|P-[tț]a|Intr(?:area)?|Drumul|Fund(?:[aă]tura)?)(?:\.\s*|\s+)"
    r"(?P<name>[^\s,.].*?)(?=\s*,?\s*\b(?:nr|bl|sc|et|ap|cam|camera)\b\.?|\s*,|\s*$)",
    re.IGNORECASE,
)
_PARTS = {
    "street_number": re.compile(r"\bnr\.?\s*(?P<v>\d+[A-Za-z]?(?:\s*[-/]\s*\d+[A-Za-z]?)?)", re.I),
    "building": re.compile(r"\bbl(?:oc)?\.?\s*(?P<v>[A-Za-z0-9][\w\-/]*)", re.I),
    "entrance": re.compile(r"\bsc(?:ara)?\.?\s*(?P<v>[A-Za-z0-9]+)", re.I),
    "floor": re.compile(r"\bet(?:aj)?\.?\s*(?P<v>\d+|parter|P|D|M)\b", re.I),
    "apartment": re.compile(
        r"\b(?:ap(?:artament)?\.?\s*(?P<v>\d+[A-Za-z]?)|(?P<room>cam(?:era)?\.?\s*\d+\w*))", re.I
    ),
}
_KEEP_STREET_KIND = {
    "bd": "Bd.",
    "b-dul": "Bd.",
    "bulevardul": "Bd.",
    "calea": "Calea",
    "aleea": "Aleea",
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
    apartment (and sector for Bucharest). Only the parts present are returned."""
    text = " ".join(text.replace("\n", ", ").split())
    result: dict[str, str] = {}
    if match := _COUNTY.search(text):
        county = county_name(match["county"])
        if county:
            result["county"] = county
    if match := _SECTOR.search(text):
        result["sector"] = f"Sector {match['sector']}"
    localities = [format_locality(m["kind"], m["name"]) for m in _LOCALITY_PREFIX.finditer(text)]
    if localities:  # "Com. Hărman, Sat Podu Oltului"
        result["city"] = ", ".join(localities)
    if match := _STREET.search(text):
        name = match["name"].strip(" ,.")
        kind = _KEEP_STREET_KIND.get(_fold(match["kind"]).rstrip("."))
        result["street"] = f"{kind} {name}" if kind else name
    for part, pattern in _PARTS.items():
        if match := pattern.search(text):
            if match.groupdict().get("room"):
                result[part] = " ".join(match["room"].split()).lower()
            else:
                result[part] = match["v"].replace(" ", "")
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
        if re.match(r"(?i)(bd|calea|aleea|șos|splaiul|piața|intrarea|drumul)", street)
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
Câmpulung, Curtea de Argeș, Caransebeș, Fetești, Tecuci, Rădăuți, Fălticeni, Vatra Dornei, Adjud,
Mărășești, Otopeni, Voluntari, Pantelimon, Popești-Leordeni, Chitila, Bragadiru, Mioveni,
Târgu Neamț, Huși, Negrești, Dorohoi, Sighetu Marmației, Borșa, Vișeu de Sus, Carei, Beiuș,
Salonta, Marghita, Aiud, Blaj, Sebeș, Cugir, Orăștie, Brad, Vulcan, Lupeni, Petrila, Făget,
Buziaș, Jimbolia, Sânnicolau Mare, Lipova, Ineu, Motru, Băilești, Calafat, Caracal, Balș,
Drăgășani, Horezu, Rovinari, Târgu Cărbunești, Zimnicea, Roșiori de Vede, Turnu Măgurele,
Oltenița, Urziceni, Rm. Sărat, Râmnicu Sărat, Tulcea, Măcin, Babadag, Cernavodă, Eforie,
Techirghiol, Odobești, Panciu, Târgu Secuiesc, Odorheiu Secuiesc, Gheorgheni, Toplița,
Târgu Frumos, Hârlău, Bicaz, Sângeorz-Băi, Năsăud, Beclean, Șimleu Silvaniei, Jibou,
Tășnad, Negrești-Oaș, Moldova Nouă, Oravița, Anina, Bocșa, Hațeg, Simeria, Călan, Ocna Mureș,
Luduș, Sovata, Târnăveni, Cisnădie, Avrig, Agnita, Dumbrăveni
"""
_PLACE_BY_FOLDED = {
    _fold(p.strip()): p.strip() for p in _PLACES.replace("\n", " ").split(",") if p.strip()
}
_PLACE_BY_FOLDED.update({_fold(name): name for name in COUNTY_CODES.values()})
_MAX_PLACE_WORDS = max(len(p.split()) for p in _PLACE_BY_FOLDED.values())


def restore_diacritics(text: str) -> str:
    """Replace known place names written without (or with wrong) diacritics by their correct
    spelling: "Fagaras" / "Focşani" (cedilla) -> "Făgăraș" / "Focșani".

    Only whole words matching a known name accent-insensitively are replaced, so unknown names
    and names already written correctly are left alone.
    """
    tokens = re.split(r"(\s+|[.,;:/()])", text)
    words = [(i, t) for i, t in enumerate(tokens) if t and not re.fullmatch(r"\s+|[.,;:/()]", t)]
    position = 0
    while position < len(words):
        for size in range(min(_MAX_PLACE_WORDS, len(words) - position), 0, -1):
            chunk = words[position : position + size]
            first, last = chunk[0][0], chunk[-1][0]
            original = "".join(tokens[first : last + 1])
            proper = _PLACE_BY_FOLDED.get(_fold(" ".join(t for _, t in chunk)))
            if proper and _fold(original) == _fold(proper):
                if original.lower() != proper.lower():  # missing or wrong diacritics
                    fixed = proper.upper() if original.isupper() else proper
                    tokens[first : last + 1] = [fixed] + [""] * (last - first)
                position += size
                break
        else:
            position += 1
    return "".join(tokens)


_ROOM_SUFFIX = re.compile(r"^(?P<street>.*?)[\s,]+(?P<room>cam(?:era)?\.?\s*\d+\w*)$", re.I)


def split_room(street: str) -> tuple[str, str] | None:
    """``Mihai Eminescu camera 2`` -> ("Mihai Eminescu", "camera 2")."""
    match = _ROOM_SUFFIX.match(street.strip())
    if not match or not match["street"]:
        return None
    return match["street"].strip(" ,"), " ".join(match["room"].split()).lower()
