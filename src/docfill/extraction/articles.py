"""Company documents: the articles of incorporation (act constitutiv), the proof that the firm
name is available (dovada disponibilității denumirii) and the proof of the registered office
(contract de comodat / închiriere).

An act constitutiv names the company and every person of it, each with a role::

    ACT CONSTITUTIV al Societății EXEMPLU SOFT S.R.L.
    Asociat unic: POPESCU ION, CNP 1871114321239, cu domiciliul în ...
    Art. 1.2. — Denumirea societății este: EXEMPLU SOFT S.R.L., conform dovezii privind
    disponibilitatea firmei nr. 123456 din 01.09.2026 ...
    Art. 1.4. — Sediul societății este în Mun. Cluj-Napoca, Str. Florilor nr. 5, jud. Cluj.
    — activitatea principală clasa CAEN 6201 și denumirea activității Realizarea soft-ului ...
    Art. 3.1. — ... capitalul social subscris ... este de 200 lei ... 20 de părți sociale ...
    Art. 6.1. — Administrarea societății se face de către: POPESCU ION, CNP ...
    Art. 10. — ... beneficiarul real al societății este: POPESCU ION, CNP ...

Every identification clause is read (:mod:`docfill.extraction.clauses`); the words just before
it say what the person is (associate, administrator, beneficial owner). The same CNP is the
same person, whatever the article. The persons are numbered for the forms: person 1, who signs
the requests, is the one who represents the company (an administrator, an associate one first),
then the other administrators, then the associates.

The proof of the firm name gives the name as the trade register reserved it; the proof of the
registered office gives the address of the premises (never the identity of the owner, who is
not a person of the request). Only text that is there is read; nothing is guessed.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from docfill.extraction.clauses import CLAUSE_CONFIDENCE, clause_fields, fold, split_name
from docfill.extraction.fields import CONTROL_OPTIONS, MAX_PERSONS, PERSON_PREFIXES
from docfill.extraction.text_utils import clean_value
from docfill.lexicon import given_names, street_words, surnames
from docfill.models import ExtractedField
from docfill.ro import check_cnp, format_amount, normalize_date, parse_amount

ROLE_CONFIDENCE = 0.9
# What the act constitutiv says about the company and the capacity of person 1 outranks what a
# person's own statement says ("în calitate de administrator al societății ...").
ARTICLES_CONFIDENCE = 0.95
OFFICIAL_CONFIDENCE = 0.95  # what the trade register itself wrote (the reserved firm name)
PREMISES_CONFIDENCE = 0.85  # the address of the premises: the act constitutiv has the last word

_UPPER = "A-ZĂÂÎȘȚŞŢ"
_DATE = r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}"
# The legal form ending a firm name: S.R.L., SRL, S.R.L.-D., S.A., S.N.C., S.C.S., S.C.A.
_LEGAL_FORM = (
    r"S\.?\s?R\.?\s?L\.?(?:\s?-\s?D\.?)?|s\.r\.l\.|S\.?\s?A\.?|S\.?\s?N\.?\s?C\.?"
    r"|S\.?\s?C\.?\s?[SA]\.?"
)
_FIRM = rf"(?P<name>[^\s,;:(][^\n,;:()]*?\b(?:{_LEGAL_FORM}))(?=[\s,.;:—–(\"”»]|$)"
# The firm right before "(în curs de înființare)": capitalised words ending with the legal form.
_FIRM_BEFORE = re.compile(
    rf"(?P<name>(?:[{_UPPER}0-9„\"][\w&'’„”\".-]*[ \t]+){{0,6}}(?:{_LEGAL_FORM}))[ \t]*,?[ \t]*\(?$"
)

# A person written "POPESCU ION, CNP ..." (clauses.py) or "Popescu Ion, CNP ...".
_PERSON_START = re.compile(
    rf"(?P<name>[{_UPPER}][\w'’-]+(?:[ \t]+[{_UPPER}][\w'’-]+){{1,4}})[ \t]*,[ \t]*"
    r"(?=(?i:cnp|cu\s+domiciliul|domicil|n[ăa]scut|cet[ăa][țţt]|posesor|identificat|de\s+cet))"
)
_NOT_NAMES = {"subsemnatul", "subsemnata", "subsemnatii", "d-nul", "d-na", "dl", "dna", "domnul"}

# What the words just before a clause say the person is (accent-free, lower case). The cue
# nearest to the name wins: "Capitalul social este deținut de către unicul asociat astfel:".
_CUES = (
    ("beneficial_owner", r"beneficiar\w*\s+real\w*"),
    ("censor", r"\bcenzor\w*|auditor\w*\s+financiar"),
    ("administrator", r"\badministr\w*"),
    ("sole_associate", r"\basociat(?:ul)?\s+unic\b|\bunicul\s+asociat\b"),
    (
        "associate",
        r"\basociat\w*|\bfondator\w*|\bactionar\w*|capitalul\s+social\s+(?:este\s+)?detinut",
    ),
)
_CUE_WINDOW = 400
# "în calitate de asociat unic și administrator" inside the clause itself
_CAPACITY_ROLES = (
    ("sole_associate", r"asociat\w*\s+unic"),
    ("associate", r"asociat|fondator|actionar"),
    ("administrator", r"administrator"),
)

_PERCENT = re.compile(r"(?P<pct>\d{1,3}(?:[.,]\d+)?)\s*%")
_SHARES = re.compile(
    r"(?P<count>\d[\d.]*)\s+(?:de\s+)?(?P<kind>p[aă]r[tț]i\s+sociale|ac[tț]iuni)\b", re.I
)
_TERM = re.compile(
    r"\bpe\s+(?:o\s+)?(?:perioad[aă]|durat[aă])\s+(?:de\s+)?(?P<years>\d{1,3})\s+(?:de\s+)?ani\b",
    re.I,
)
_CONTROL = re.compile(r"\blit\.?\s*(?P<lit>[a-f])\)\s*pct\.?\s*(?P<pct>\d)")
_CONTROL_TEXT = re.compile(r"\((?P<text>(?:de[tț]inere|control|exercit)[^()]{5,200})\)", re.I)


@dataclass
class _Person:
    fields: dict[str, str]
    order: int
    roles: set[str] = field(default_factory=set)
    percent: float | None = None
    shares: str | None = None
    term: str | None = None
    control: str | None = None
    control_text: str | None = None
    evidence: str = ""

    @property
    def key(self) -> str:
        cnp = self.fields.get("cnp")
        if cnp:
            return cnp
        return fold(f"{self.fields.get('last_name', '')} {self.fields.get('first_name', '')}")

    @property
    def is_associate(self) -> bool:
        return bool(self.roles & {"associate", "sole_associate"})

    @property
    def is_administrator(self) -> bool:
        return "administrator" in self.roles

    def merge(self, other: _Person) -> None:
        for name, value in other.fields.items():
            self.fields.setdefault(name, value)
        self.roles |= other.roles
        for attribute in ("percent", "shares", "term", "control", "control_text"):
            if getattr(self, attribute) is None:
                setattr(self, attribute, getattr(other, attribute))


def _field(
    name: str, value: str, confidence: float, evidence: str, document: str | None
) -> ExtractedField:
    return ExtractedField(
        name=name,
        value=value,
        confidence=confidence,
        source="pattern",
        evidence=clean_value(evidence)[:160],
        document=document,
    )


def _collapse(text: str) -> str:
    return " ".join(text.split())


def _firm_name(text: str) -> str:
    """``EXEMPLU VERDE S.R.L.— societate cu răspundere limitată`` -> ``EXEMPLU VERDE S.R.L.``;
    quotes around the name are dropped."""
    name = _collapse(re.split(r"\s*[—–]\s*|\s+-\s+", text)[0])
    return re.sub(r"[„“”\"«»]", "", name).strip(" ,;:")


def _search_folded(pattern: str, text: str, start: int = 0) -> re.Match[str] | None:
    """``pattern`` (accent-free, lower case) searched in ``text`` from ``start``: the match
    positions are valid in ``text`` (folding keeps the length)."""
    return re.compile(pattern).search(fold(text), start)


def _line_at(text: str, position: int) -> str:
    """The rest of the line from ``position``."""
    end = text.find("\n", position)
    return text[position : end if end >= 0 else len(text)]


# Abbreviations whose full stop does not end a sentence (addresses, titles; "BI" is "bl." read
# by OCR).
_ABBREVIATIONS = {
    "nr", "bl", "bi", "sc", "et", "ap", "cam", "str", "mun", "jud", "com", "sat", "or", "sect",
    "sec", "ale", "al", "bd", "sos", "spl", "intr", "loc", "lt", "col", "gen", "dr", "prof",
    "ing", "sf", "dl", "dna", "tel", "cod", "art", "alin", "lit", "pct",
}  # fmt: skip


def _sentence_from(text: str, position: int) -> str:
    """From ``position`` to the end of the sentence, lines joined (an address cut over two
    lines is one address). A full stop ends it at the end of a line or before a new sentence
    (``Camera 1. Spațiul va fi``), not after an abbreviation (``Mun. Timișoara``)."""
    rest = text[position : position + 600]
    end = len(rest)
    for match in re.finditer(r"\.(?=\s|$)|\n[ \t]*\n", rest):
        if match.group() != ".":  # a blank line
            end = match.start()
            break
        word = re.search(r"(\w+)$", rest[: match.start()])
        if word and fold(word.group(1)) in _ABBREVIATIONS:
            continue
        after = rest[match.end() :]
        if re.match(r"[ \t]*(?:\n|$)", after) or re.match(
            r"\s+[A-ZĂÂÎȘȚ][a-zăâîșț]{3,}\s+[a-zăâîșț]", after
        ):
            end = match.start()
            break
    return _collapse(rest[:end].replace("\n", " ")).rstrip(" .,;")


# --------------------------------------------------------------------------- the company


# The legal form written out after the name: "XXXXX — societate cu răspundere limitată/S.R.L".
_FORM_WORDS = (
    (r"\bS\.?\s?R\.?\s?L\.?\s?-\s?D\b|\bdebutant", "S.R.L.-D."),
    (r"\bS\.?\s?R\.?\s?L\b|raspundere\s+limitata", "S.R.L."),
    (r"\bS\.?\s?A\b|societate\s+pe\s+actiuni", "S.A."),
    (r"\bS\.?\s?N\.?\s?C\b|nume\s+colectiv", "S.N.C."),
    (r"\bS\.?\s?C\.?\s?S\b|comandita\s+simpla", "S.C.S."),
    (r"\bS\.?\s?C\.?\s?A\b|comandita\s+pe\s+actiuni", "S.C.A."),
)
_HAS_FORM = re.compile(rf"\b(?:{_LEGAL_FORM})$")


def _legal_form(text: str) -> str | None:
    folded = fold(text)
    for pattern, form in _FORM_WORDS:
        if re.search(pattern, text) or re.search(pattern, folded):
            return form
    return None


def _declared_name(line: str) -> str | None:
    """``EXEMPLU VERDE S.R.L.— societate cu răspundere limitată/S.R.L., conform ...`` or
    ``EXEMPLU VERDE — societate cu răspundere limitată/S.R.L``: the name, with its legal form
    (taken from the words after the dash when the name does not end with it)."""
    line = _collapse(line)
    parts = re.split(r"\s*([—–]|\s-\s)\s*", line, maxsplit=1)
    head, dash, tail = (parts + ["", ""])[:3]
    head = re.split(r",\s*(?:conform|potrivit|cu\s+sediul|avand)\b|;", head, maxsplit=1)[0]
    name = re.sub(r"[„“”\"«»]", "", head).strip(" ,.;:") if head else ""
    if found := re.match(_FIRM, head):
        return _firm_name(found["name"])
    if not name or not re.match(rf"[{_UPPER}0-9]", name) or len(name) > 100:
        return None
    if not _HAS_FORM.search(name) and (form := _legal_form(tail if dash else "")):
        name = f"{name} {form}"
    return name


def company_name(text: str) -> tuple[str, str] | None:
    """The firm name and the text it was read from: ``Denumirea societății este: X`` (followed
    by ``— societate cu răspundere limitată/S.R.L``), the title ``ACT CONSTITUTIV al Societății
    X S.R.L.``, ``societatea X S.R.L. (în curs de constituire)``."""
    folded = fold(text)
    for match in re.finditer(
        r"denumirea\s+(?:societatii|firmei)\s+(?:este|va\s+fi)\s*:?\s*", folded
    ):
        if name := _declared_name(_line_at(text, match.end())):
            return name, _line_at(text, match.start())
    for pattern in (
        r"\b(?:al|a)\s+societatii\s+(?:comerciale\s+)?",
        r"\bsocietat(?:ea|ii)\s+(?:comerciala\s+)?",
    ):
        for match in re.finditer(pattern, folded):
            line = _line_at(text, match.end())
            if found := re.match(_FIRM, line):
                name = _firm_name(found["name"])
                if len(name) > 6 and re.search(rf"[{_UPPER}0-9]", name[:1]):
                    return name, _line_at(text, match.start())
    return None


def company_address(text: str) -> tuple[str, str] | None:
    """``Sediul societății este în Mun. Timișoara, Ale. Teilor nr. 4, ... jud. Timiș.`` (or
    ``este : ...``, ``Sediul social: ...``)."""
    match = _search_folded(
        r"\bsediul\s+(?:social\b\s*)?(?:al\s+)?(?:societatii\s+|firmei\s+)?(?:(?:(?:este|va\s+fi|se"
        r"\s+afla|se\s+stabileste)\s+)?(?:stabilit\s+|situat\s+)?(?:in|la)\b\s*:?|(?:este|va\s+fi)"
        r"\s*:|:)\s*",
        text,
    )
    if not match:
        return None
    address = _sentence_from(text, match.end())
    return (address, _line_at(text, match.start())) if len(address) > 8 else None


def share_capital(text: str) -> tuple[str, str | None, str] | None:
    """``(capital, number of shares or None, evidence)``: ``capitalul social subscris ... este
    de 500 lei ... împărțit într-un număr de 50 de părți sociale`` (``este de : 500 lei``)."""
    pattern = (
        r"capitalul\s+social(?:\s+subscris)?(?:\s+si\s+varsat)?(?:\s+integral)?(?:\s+al\s+"
        r"societatii)?(?:\s+subscris)?\s+(?:este|va\s+fi)\s*(?:de|in\s+valoare\s+de|in\s+suma\s+"
        r"de)?\s*:?\s*"
    )
    for match in re.finditer(pattern, fold(text)):
        sentence = _sentence_from(text, match.end())
        amount = re.match(r"(?P<amount>\d[\d.,\s]*?)\s*(?P<unit>lei|ron)\b", sentence, re.I)
        if not amount or not parse_amount(amount["amount"]):
            continue
        shares = _SHARES.search(sentence)
        count = shares["count"].replace(".", "") if shares else None
        return f"{amount['amount'].strip()} lei", count, _line_at(text, match.start())
    return None


def name_reservation(text: str) -> tuple[str, str | None] | None:
    """``conform dovezii privind disponibilitatea firmei nr. 123456 din 01.09.2026``."""
    match = _search_folded(
        r"disponibilitat\w*\s+(?:a\s+)?(?:firmei|denumirii)[^.\n]{0,40}?\bnr\.?\s*:?\s*"
        rf"(?P<number>\d{{4,}})(?:\s*(?:din|/)\s*(?P<date>{_DATE}))?",
        text,
    )
    if not match:
        return None
    return match["number"], normalize_date(match["date"]) if match["date"] else None


def duration_years(text: str) -> str | None:
    """``Durata societății este de 10 ani`` -> ``10``; ``nelimitată`` / ``nedeterminată`` ->
    nothing (the form's empty value)."""
    match = _search_folded(
        r"\bdurata\s+(?:de\s+functionare\s+)?(?:a\s+)?societatii[^.\n]{0,40}", text
    )
    if not match:
        return None
    years = re.search(r"\b(\d{1,3})\s+(?:de\s+)?ani\b", match.group())
    return years.group(1) if years else None


def _activity_section(text: str) -> tuple[int, int]:
    start = _search_folded(r"\bobiect\w*\s+(?:principal\s+)?de\s+activitate", text)
    if not start:
        return 0, 0
    end = _search_folded(r"\bcapitolul\s+[ivxl]+\b|\bcapitalul\s+social", text, start.end())
    return start.start(), end.start() if end else len(text)


def _activity_name(text: str) -> str:
    name = re.sub(
        r"(?i)^\s*(?:[sș]i\s+)?(?:denumirea\s+(?:activit[aă][tț]ii\s*)?)?[:\-–—]?\s*", "", text
    )
    name = re.sub(r"(?i)\s*[,(]?\s*\(?activitate\s+principal[aă]\)?\s*$", "", name)
    name = re.sub(r"(?i)\s+principal[aă]?\s*$", "", name)  # "... tutun principal"
    return _collapse(name).strip(" ;,.-–—")


def caen_activities(text: str) -> list[str]:
    """``<class> <name>`` per activity, the main activity first: ``clasa CAEN 4711 și denumirea
    activității ...``, ``Cod CAEN 6201 - ...`` or, in the object of activity, ``6201 - ...``."""
    found: dict[str, str] = {}
    main: str | None = None
    start, end = _activity_section(text)
    position = 0
    for line in text.splitlines(keepends=True):
        here, position = position, position + len(line)
        folded = fold(line)
        match = re.search(
            r"\b(?:cod(?:ul)?\s+|clasa\s+)?caen(?:\s+rev\.?\s*\d)?\s*:?\s*(?P<code>\d{4})\b", folded
        )
        if match is None and start <= here < end:
            match = re.match(r"^[\s\-–—•*·\d.)]*?(?P<code>\d{4})\s*[-–—:]\s*(?=\w)", folded)
        if match is None:
            continue
        code = match["code"]
        name = _activity_name(line[match.end() :].rstrip("\n"))
        if len(name) < 4 and not re.search(r"clasa|cod", folded[: match.start("code")]):
            continue  # "grupei CAEN 471, căruia îi corespunde clasa CAEN 4711." keeps the class
        if not found.get(code):
            found[code] = name
        if main is None and re.search(r"principal", folded):
            main = code
    if not found:
        return []
    codes = list(found)
    if main and main in found:
        codes.remove(main)
        codes.insert(0, main)
    return [f"{code} {found[code]}".strip() for code in codes]


# --------------------------------------------------------------------------- the persons


def _cue(window: str) -> str | None:
    folded = fold(window)
    best: tuple[int, str] | None = None
    for role, pattern in _CUES:
        for match in re.finditer(pattern, folded):
            if best is None or match.end() > best[0]:
                best = (match.end(), role)
    return best[1] if best else None


def _persons(text: str) -> list[_Person]:
    """Every identification clause, with the role the words before it give."""
    persons: list[_Person] = []
    known: set[str] = set()  # the CNPs read so far
    list_cue: str | None = None  # "Asociați:" heads every person listed after it
    previous_end = 0
    for order, match in enumerate(_PERSON_START.finditer(text)):
        if match.start() < previous_end:  # inside the clause of the previous person
            continue
        tokens = [t for t in match["name"].split() if fold(t).strip(".") not in _NOT_NAMES]
        if len(tokens) < 2:
            continue
        rest = text[match.end() :]
        end = re.search(r"[.;]\s*\n|\n\s*\n", rest)  # one person per paragraph or list item
        sentence = rest[: end.start() + 1 if end else len(rest)]
        found = clause_fields(sentence)
        if (cnp := found.get("cnp")) and not check_cnp(cnp).valid:
            found.pop("cnp")
        # a name and a lone "CNP" is not an identification, unless it names a person again:
        # "POPESCU ION, CNP ..., deține 60 de părți sociale"
        if len(found) < 3 and not (found.get("cnp") and found["cnp"] in known):
            continue
        names = split_name(" ".join(tokens))
        if names:
            found.setdefault("first_name", names[0])
            found.setdefault("last_name", names[1])
        person = _Person(found, order, evidence=" ".join(tokens) + ", " + sentence[:120])
        window = text[max(previous_end, match.start() - _CUE_WINDOW) : match.start()]
        if cue := _cue(window):
            person.roles.add(cue)
        elif persons and not re.search(r"[^\W\d_]", window):  # the next item of the same list
            cue = list_cue
            if cue:
                person.roles.add(cue)
        list_cue = cue
        if capacity := found.pop("capacity", None):
            for role, pattern in _CAPACITY_ROLES:
                if re.search(pattern, fold(capacity)):
                    person.roles.add(role)
                    break
            found.pop("company_name", None)
        if percent := _PERCENT.search(sentence):
            person.percent = float(percent["pct"].replace(",", "."))
        if shares := _SHARES.search(sentence):
            person.shares = shares["count"].replace(".", "")
        if term := _TERM.search(sentence):
            person.term = term["years"]
        if "beneficial_owner" in person.roles:
            after = text[match.end() + len(sentence) : match.end() + len(sentence) + 800]
            after = after[: _cut_at_next_article(after)]
            if control := _CONTROL.search(fold(after)):
                person.control = f"art. 4 alin. (2) lit. {control['lit']}) pct. {control['pct']}"
            if described := _CONTROL_TEXT.search(after):
                person.control_text = _collapse(described["text"])
        previous_end = match.end() + len(sentence)
        persons.append(person)
        known.add(found.get("cnp", ""))
    return _merge_persons(persons)


def _cut_at_next_article(text: str) -> int:
    found = re.search(r"(?im)^\s*(?:capitolul|art\.?\s*\d)", text)
    return found.start() if found else len(text)


def _merge_persons(persons: list[_Person]) -> list[_Person]:
    """The same CNP (or name) is the same person, whatever the article naming them."""
    merged: dict[str, _Person] = {}
    for person in persons:
        if person.key in merged:
            merged[person.key].merge(person)
        else:
            merged[person.key] = person

    def rank(person: _Person) -> tuple[int, int]:
        if person.is_administrator and person.is_associate:
            return 0, person.order
        if person.is_administrator:
            return 1, person.order
        if person.is_associate:
            return 2, person.order
        return 3, person.order

    return sorted(merged.values(), key=rank)


def _control_option(control: str | None, percent: float | None) -> str | None:
    """The option of the beneficial owner declaration: the one the act cites, else direct
    ownership (lit. a) pct. 1) for more than 25% of the shares."""
    if control:
        return next((o for o in CONTROL_OPTIONS if o.startswith(control)), control)
    if percent and percent > 25:
        return CONTROL_OPTIONS[0]
    return None


def capacity_of(person: _Person) -> str | None:
    """``asociat unic și administrator``: the capacity in which person 1 signs."""
    words = []
    if "sole_associate" in person.roles:
        words.append("asociat unic")
    elif "associate" in person.roles:
        words.append("asociat")
    if person.is_administrator:
        words.append("administrator")
    return " și ".join(words) or None


# --------------------------------------------------------------------------- extractors


def extract_articles(text: str, document: str | None = None) -> list[ExtractedField]:
    """Everything an act constitutiv says: the company, its capital and activities, and its
    persons numbered for the forms (person 1 plain, then ``p2_``, ``p3_``)."""
    found: list[ExtractedField] = []

    def put(name: str, value: str | None, confidence: float, evidence: str) -> None:
        if value and value.strip():
            found.append(_field(name, value.strip(), confidence, evidence, document))

    if firm := company_name(text):
        put("company_name", firm[0], ARTICLES_CONFIDENCE, firm[1])
    if office := company_address(text):
        put("company_address", office[0], ARTICLES_CONFIDENCE, office[1])
    capital = share_capital(text)
    share_count = capital[1] if capital else None
    if capital:
        put("share_capital", capital[0], ARTICLES_CONFIDENCE, capital[2])
        put("share_count", share_count, ARTICLES_CONFIDENCE, capital[2])
    if reservation := name_reservation(text):
        put("name_reservation_number", reservation[0], ARTICLES_CONFIDENCE, reservation[0])
        put("name_reservation_date", reservation[1], ARTICLES_CONFIDENCE, reservation[0])
    put("company_duration", duration_years(text), ARTICLES_CONFIDENCE, "durata societății")
    if activities := caen_activities(text):
        put("caen_activities", "\n".join(activities), ARTICLES_CONFIDENCE, activities[0])

    persons = _persons(text)
    administrators = [p for p in persons if p.is_administrator]
    associates = [p for p in persons if p.is_associate]
    parts = (
        "acțiuni"
        if re.search(r"\bac[tț]iuni\b", text, re.I)
        and not re.search(r"p[aă]r[tț]i\s+sociale", text, re.I)
        else "părți sociale"
    )
    rows = []
    for person in associates:
        shares = person.shares
        if shares is None and share_count and person.percent is not None:
            shares = format_amount(parse_amount(share_count) * person.percent / 100)
        if shares is None and share_count and len(associates) == 1:
            shares = share_count
        person.shares = shares
        percent = person.percent
        if percent is None and shares and share_count:
            percent = parse_amount(shares) / parse_amount(share_count) * 100
        held = f"{shares} {parts}" if shares else ""
        if percent is not None:
            held = f"{held} ({format_amount(percent)}%)".strip()
        name = f"{person.fields.get('last_name', '')} {person.fields.get('first_name', '')}"
        row = (name.strip().upper(), person.fields.get("cnp"), held)
        rows.append(" | ".join(filter(None, row)))
    if rows:
        put("associates", "\n".join(rows), ARTICLES_CONFIDENCE, "asociații din actul constitutiv")
    if term := next((p.term for p in administrators if p.term), None):
        put("board_term_years", term, ROLE_CONFIDENCE, f"pe perioadă de {term} de ani")

    for prefix, person in zip(PERSON_PREFIXES, persons[:MAX_PERSONS], strict=False):
        evidence = person.evidence
        for name, value in person.fields.items():
            put(prefix + name, value, CLAUSE_CONFIDENCE, evidence)
        if person.is_associate:
            put(prefix + "associate", "x", ROLE_CONFIDENCE, evidence)
            put(prefix + "shares", person.shares, ROLE_CONFIDENCE, evidence)
        if person.is_administrator:
            role = "administrator unic" if len(administrators) == 1 else "administrator"
            put(prefix + "board_role", role, ROLE_CONFIDENCE, evidence)
        if "beneficial_owner" in person.roles:
            option = _control_option(person.control, person.percent)
            put(prefix + "beneficial_owner", option, ROLE_CONFIDENCE, evidence)
            put(prefix + "control_description", person.control_text, ROLE_CONFIDENCE, evidence)
    if persons and (capacity := capacity_of(persons[0])):
        put("capacity", capacity, ARTICLES_CONFIDENCE, persons[0].evidence)
    return found


def extract_name_reservation(text: str, document: str | None = None) -> list[ExtractedField]:
    """The proof that the firm name is available (ONRC, Formular nr. 17): the reserved name and
    the number / date of the proof. Nothing about persons: the ONRC letterhead is no address."""
    found: list[ExtractedField] = []
    pattern = r"disponibilitat\w*\s+(?:si\s+rezerv\w+\s+)?(?:a\s+)?denumirii\s+firmei[ \t]+"
    for match in re.finditer(pattern, fold(text)):  # not the title: "privind disponibilitatea..."
        line = _collapse(_line_at(text, match.end()))
        if firm := re.match(_FIRM, line):
            name = _firm_name(firm["name"])
            found.append(_field("company_name", name, OFFICIAL_CONFIDENCE, line, document))
            break
    if number := re.search(rf"\bNr\.?\s*:?\s*(?P<number>\d{{5,}})\s*/\s*(?P<date>{_DATE})", text):
        evidence = number.group()
        found.append(_field("name_reservation_number", number["number"], 0.9, evidence, document))
        if date := normalize_date(number["date"]):
            found.append(_field("name_reservation_date", date, 0.9, evidence, document))
    return found


def _capital_i(text: str) -> str:
    """``Ale. lancu Jianu`` -> ``Ale. Iancu Jianu``: a word OCR started with a lowercase l
    that, with a capital I, is a name or a street word the lists know (and is not one as read)."""

    def known(word: str) -> bool:
        return any(words.known(word) for words in (given_names(), street_words(), surnames()))

    def fix(match: re.Match[str]) -> str:
        word = match.group()
        return "I" + word[1:] if known("I" + word[1:]) and not known(word) else word

    return re.sub(r"(?<![\w])l[a-zăâîșț]{2,}", fix, text)


def extract_premises(text: str, document: str | None = None) -> list[ExtractedField]:
    """The proof of the registered office (comodat, lease, owner's statement): the address of
    the premises and the company it is lent to. The owner is not a person of the request."""
    found: list[ExtractedField] = []
    match = _search_folded(
        r"\b(?:imobil\w*|spati\w*|apartament\w*|sediu\w*|cladir\w*|incaper\w*)\s+(?:\w+\s+){0,3}?"
        r"situat\w*\s+(?:in|la)\s*:?\s*",
        text,
    )
    if match:
        address = _capital_i(_sentence_from(text, match.end()))
        if len(address) > 8:
            evidence = _line_at(text, match.start())
            found.append(
                _field("company_address", address, PREMISES_CONFIDENCE, evidence, document)
            )
    if pending := _search_folded(r"\bin\s+curs\s+de\s+(?:infiintare|constituire)", text):
        line_start = text.rfind("\n", 0, pending.start()) + 1
        if firm := _FIRM_BEFORE.search(text[line_start : pending.start()]):
            evidence = text[line_start : pending.end()]
            name = _firm_name(firm["name"])
            found.append(_field("company_name", name, PREMISES_CONFIDENCE, evidence, document))
    return found


# Document types read by their own extractor, instead of the general one (labels, patterns,
# a single identification clause), which would take their persons for the applicant.
EXTRACTORS: dict[str, Callable[[str, str | None], list[ExtractedField]]] = {
    "act_constitutiv": extract_articles,
    "dovada_denumire": extract_name_reservation,
    "dovada_sediu": extract_premises,
}

__all__ = [
    "EXTRACTORS",
    "caen_activities",
    "company_address",
    "company_name",
    "extract_articles",
    "extract_name_reservation",
    "extract_premises",
]
