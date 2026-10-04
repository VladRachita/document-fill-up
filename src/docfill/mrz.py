"""Machine readable zone (MRZ) of identity documents, ICAO 9303.

Supports TD1 (3 x 30, new Romanian electronic identity card), TD2 (2 x 36, Romanian identity
card ``IDROU...``) and TD3 (2 x 44, passports). Every number and date carries a check digit, so
a value is only trusted when its check digit matches; common OCR confusions in numeric positions
(O/0, I/1, B/8...) are repaired when that makes the check digit match.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field
from datetime import date

from docfill.ro import check_cnp

_LAYOUTS = {3: 30, 2: 36}  # lines -> length (TD1, TD2); TD3 handled separately (2 x 44)
_TO_DIGIT = str.maketrans(
    {
        "O": "0",
        "Q": "0",
        "D": "0",
        "I": "1",
        "L": "1",
        "Z": "2",
        "S": "5",
        "B": "8",
        "G": "6",
        "T": "7",
    }
)


# Digits OCR reads where the zone has a letter (document code, state, nationality).
_TO_ALPHA = str.maketrans({"0": "O", "1": "I", "2": "Z", "5": "S", "6": "G", "8": "B"})
# The letters of a series that OCR mixes up with digits or with each other (capital I, 1, l).
_SERIES_SWAPS = {
    "L": "I",
    "I": "L",
    "1": "I",
    "0": "O",
    "5": "S",
    "8": "B",
    "2": "Z",
    "6": "G",
    "7": "T",
}


def _alpha(text: str) -> str:
    return text.translate(_TO_ALPHA)


def check_digit(data: str) -> str:
    total = 0
    for index, char in enumerate(data):
        if char.isdigit():
            value = int(char)
        elif char.isalpha():
            value = ord(char.upper()) - 55
        else:
            value = 0
        total += value * (7, 3, 1)[index % 3]
    return str(total % 10)


def _checked(data: str, digit: str, numeric: bool = True) -> tuple[str, bool]:
    """Return (value, valid), repairing OCR letter/digit confusions in numeric positions.

    Check digits are always digits. Document numbers are alphanumeric, so only the part after
    a leading letter prefix (the Romanian series, e.g. ``AX``) is repaired."""
    digit = digit.translate(_TO_DIGIT)
    if check_digit(data) == digit:
        return data, True
    if numeric:
        repaired = data.translate(_TO_DIGIT)
        if check_digit(repaired) == digit:
            return repaired, True
        return data, False
    for prefix in _series_readings(data[:2]):
        repaired = prefix + data[2:].translate(_TO_DIGIT)
        if check_digit(repaired) == digit:
            return repaired, True
    return data, False


def _series_readings(prefix: str) -> list[str]:
    """The series (two letters) as read, then with the letters OCR mixes up swapped: ``LF`` may
    be ``IF`` (Ilfov), ``1S`` may be ``IS``. Only a reading whose check digit matches is used."""
    options = [(char, _SERIES_SWAPS[char]) if char in _SERIES_SWAPS else (char,) for char in prefix]
    return ["".join(reading) for reading in itertools.product(*options)]


def _date(yymmdd: str, future: bool) -> date | None:
    if not re.fullmatch(r"\d{6}", yymmdd):
        return None
    yy, mm, dd = int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:])
    current = date.today().year % 100
    century = 2000 if (future or yy <= current) else 1900
    try:
        return date(century + yy, mm, dd)
    except ValueError:
        return None


@dataclass
class MRZ:
    layout: str
    document_code: str
    issuing_state: str
    surname: str
    given_names: str
    document_number: str
    nationality: str
    birth_date: date | None
    sex: str | None
    expiry_date: date | None
    optional: str = ""
    valid: dict[str, bool] = field(default_factory=dict)

    @property
    def fully_valid(self) -> bool:
        return all(self.valid.values())


# "<<<<" fillers that OCR read as letters: a run of these letters is not a name.
_FILLER_TOKEN = re.compile(r"[CKELSX]{3,}")


def _names(zone: str) -> tuple[str, str]:
    surname, _, given = zone.partition("<<")

    def words(part: str) -> str:
        found = [w for w in part.replace("<", " ").split() if not _FILLER_TOKEN.fullmatch(w)]
        # a letter left on its own is a "<" filler OCR read as a letter: no name is one letter
        return " ".join(w for w in found if len(w) > 1 or len(found) == 1)

    return words(surname), words(given)


def _clean(line: str) -> str:
    line = line.upper().replace(" ", "")
    line = re.sub(r"[«‹(\[{]", "<", line)
    line = line.replace("$", "S").replace("§", "S").replace("|", "I").replace("!", "I")
    return re.sub(r"[^A-Z0-9<]", "", line)


def find_mrz_lines(text: str) -> list[str] | None:
    """Find 2 or 3 consecutive MRZ-looking lines in OCR text and normalise their length."""
    lines = [_clean(line) for line in text.splitlines()]
    candidates = [(i, line) for i, line in enumerate(lines) if len(line) >= 25 and "<" in line]
    for count, width in ((2, 44), (2, 36), (3, 30)):
        for position in range(len(candidates) - count + 1):
            group = candidates[position : position + count]
            if group[-1][0] - group[0][0] > count + 1:  # must be (almost) adjacent lines
                continue
            if all(abs(len(line) - width) <= 2 for _, line in group):
                return [line[:width].ljust(width, "<") for _, line in group]
    return None


def parse_mrz(lines: list[str]) -> MRZ | None:
    if len(lines) == 2 and len(lines[0]) == 44:
        first, second = lines
        number, ok_number = _checked(second[0:9], second[9], numeric=False)
        birth, ok_birth = _checked(second[13:19], second[19])
        expiry, ok_expiry = _checked(second[21:27], second[27])
        surname, given = _names(first[5:44])
        return MRZ(
            "TD3",
            _alpha(first[0:2]).strip("<"),
            _alpha(first[2:5]),
            surname,
            given,
            number.strip("<"),
            _alpha(second[10:13]),
            _date(birth, False),
            second[20].strip("<") or None,
            _date(expiry, True),
            second[28:42].strip("<"),
            {"document_number": ok_number, "birth_date": ok_birth, "expiry_date": ok_expiry},
        )
    if len(lines) == 2 and len(lines[0]) == 36:
        return _parse_td2(*lines)
    if len(lines) == 3 and len(lines[0]) == 30:
        first, second, third = lines
        number, ok_number = _checked(first[5:14], first[14], numeric=False)
        birth, ok_birth = _checked(second[0:6], second[6])
        expiry, ok_expiry = _checked(second[8:14], second[14])
        surname, given = _names(third)
        return MRZ(
            "TD1",
            _alpha(first[0:2]).strip("<"),
            _alpha(first[2:5]),
            surname,
            given,
            number.strip("<"),
            _alpha(second[15:18]),
            _date(birth, False),
            second[7].strip("<") or None,
            _date(expiry, True),
            (first[15:30] + second[18:29]).strip("<"),
            {"document_number": ok_number, "birth_date": ok_birth, "expiry_date": ok_expiry},
        )
    return None


def _parse_td2(first: str | None, second: str) -> MRZ:
    """A TD2 zone (the Romanian identity card). ``first`` (the names) is ``None`` when only the
    second line, the one with the check digits, could be read."""
    number, ok_number = _checked(second[0:9], second[9], numeric=False)
    birth, ok_birth = _checked(second[13:19], second[19])
    expiry, ok_expiry = _checked(second[21:27], second[27])
    nationality = _alpha(second[10:13])
    surname, given = _names(first[5:36]) if first else ("", "")
    return MRZ(
        "TD2",
        _alpha(first[0:2]).strip("<") if first else "",
        _alpha(first[2:5]) if first else nationality,
        surname,
        given,
        number.strip("<"),
        nationality,
        _date(birth, False),
        second[20].strip("<") or None,
        _date(expiry, True),
        second[28:35].strip("<"),
        {"document_number": ok_number, "birth_date": ok_birth, "expiry_date": ok_expiry},
    )


def _td2_valid(line: str) -> bool:
    """Do all three check digits of a 36 character second line match?"""
    return (
        line[20] in "MF<"
        and _checked(line[0:9], line[9], numeric=False)[1]
        and _checked(line[13:19], line[19])[1]
        and _checked(line[21:27], line[27])[1]
    )


def _realigned(line: str) -> list[str]:
    """``line`` as a 36 character TD2 line, or the lines it becomes when OCR dropped a character
    (a ``<`` is put back anywhere) or added one (each character removed in turn)."""
    if len(line) == 36:
        return [line]
    if len(line) == 35:
        return [line[:i] + "<" + line[i:] for i in range(36)]
    if len(line) == 37:
        return [line[:i] + line[i + 1 :] for i in range(37)]
    return []


def find_td2_data_line(text: str) -> str | None:
    """The second line of a TD2 zone read on its own. On photographs the first line (names and
    many ``<``) is often split by OCR, but the second line is usually intact. It is accepted
    only if all three check digits match, and, when a dropped or added character has to be
    guessed, only if exactly one reading matches: a stray line is never taken for it."""
    found: set[str] = set()
    for raw in text.splitlines():
        line = _clean(raw)
        if sum(char.isdigit() for char in line) < 14:
            continue
        found.update(candidate for candidate in _realigned(line) if _td2_valid(candidate))
    if len(found) > 1:
        # A lost or extra character inside the document number leaves the rest aligned the same
        # way, so the check digit alone cannot place it: keep the readings that are a series
        # (two letters) and a number (6 or 7 digits), padded with "<" at the end only.
        found = {
            line
            for line in found
            if split_ro_document_number(_checked(line[:9], line[9], numeric=False)[0].rstrip("<"))
        } or found
    return found.pop() if len(found) == 1 else None


def read_mrz(text: str) -> MRZ | None:
    lines = find_mrz_lines(text)
    mrz = parse_mrz(lines) if lines else None
    if mrz is not None and mrz.fully_valid:
        return mrz
    if second := find_td2_data_line(text):
        names = lines[0] if lines and len(lines) == 2 and len(lines[0]) == 36 else None
        return _parse_td2(names, second)
    return mrz


def cnp_from_mrz(mrz: MRZ) -> str | None:
    """The CNP carried by the zone of a Romanian identity card, or ``None``.

    On the card with two lines the optional field holds the CNP without its date of birth (sex
    digit, county, serial number, control digit); the date comes from the zone itself, whose
    check digit has been verified. Electronic cards keep the 13 digits in the optional data. A
    number is returned only when it is a valid CNP of the date of birth in the zone."""
    if mrz.issuing_state != "ROU":
        return None
    candidates = re.findall(r"(?<!\d)\d{13}(?!\d)", mrz.optional)
    tail = mrz.optional.translate(_TO_DIGIT)
    td2 = mrz.layout == "TD2" and mrz.birth_date and mrz.valid.get("birth_date")
    if td2 and re.fullmatch(r"\d{7}", tail):
        candidates.append(tail[0] + f"{mrz.birth_date:%y%m%d}" + tail[1:])
    for cnp in candidates:
        info = check_cnp(cnp)
        if info.valid and (
            mrz.birth_date is None or f"{info.birth_date:%y%m%d}" == f"{mrz.birth_date:%y%m%d}"
        ):
            return cnp
    return None


def split_ro_document_number(number: str) -> tuple[str, str] | None:
    """Romanian identity card numbers are a 2-letter series plus 6-7 digits: ``AX123456``."""
    match = re.fullmatch(r"([A-Z]{2})(\d{6,7})", number)
    return (match.group(1), match.group(2)) if match else None


def make_td2(
    surname: str,
    given_names: str,
    number: str,
    nationality: str,
    birth: str,
    sex: str,
    expiry: str,
    optional: str = "",
    code: str = "ID",
    state: str = "ROU",
) -> list[str]:
    """Build a TD2 MRZ (used by tests and sample generation). Dates are YYMMDD."""
    names = surname.replace(" ", "<") + "<<" + given_names.replace(" ", "<").replace("-", "<")
    first = (code.ljust(2, "<") + state + names)[:36].ljust(36, "<")
    number = number.ljust(9, "<")
    optional = optional.ljust(7, "<")
    body = (
        number
        + check_digit(number)
        + nationality
        + birth
        + check_digit(birth)
        + sex
        + expiry
        + check_digit(expiry)
        + optional
    )
    composite = check_digit(body[0:10] + body[13:20] + body[21:35])
    return [first, body + composite]
