"""Machine readable zone (MRZ) of identity documents, ICAO 9303.

Supports TD1 (3 x 30, new Romanian electronic identity card), TD2 (2 x 36, Romanian identity
card ``IDROU...``) and TD3 (2 x 44, passports). Every number and date carries a check digit, so
a value is only trusted when its check digit matches; common OCR confusions in numeric positions
(O/0, I/1, B/8...) are repaired when that makes the check digit match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

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
    else:
        prefix = re.match(r"[A-Z]{0,2}", data).group()
        repaired = prefix + data[len(prefix) :].translate(_TO_DIGIT)
    if check_digit(repaired) == digit:
        return repaired, True
    return data, False


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


def _names(zone: str) -> tuple[str, str]:
    surname, _, given = zone.partition("<<")
    return surname.replace("<", " ").strip(), given.replace("<", " ").strip()


def _clean(line: str) -> str:
    line = line.upper().replace(" ", "")
    line = re.sub(r"[«‹(\[{]", "<", line)
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
            first[0:2].strip("<"),
            first[2:5],
            surname,
            given,
            number.strip("<"),
            second[10:13],
            _date(birth, False),
            second[20].strip("<") or None,
            _date(expiry, True),
            second[28:42].strip("<"),
            {"document_number": ok_number, "birth_date": ok_birth, "expiry_date": ok_expiry},
        )
    if len(lines) == 2 and len(lines[0]) == 36:
        first, second = lines
        number, ok_number = _checked(second[0:9], second[9], numeric=False)
        birth, ok_birth = _checked(second[13:19], second[19])
        expiry, ok_expiry = _checked(second[21:27], second[27])
        surname, given = _names(first[5:36])
        return MRZ(
            "TD2",
            first[0:2].strip("<"),
            first[2:5],
            surname,
            given,
            number.strip("<"),
            second[10:13],
            _date(birth, False),
            second[20].strip("<") or None,
            _date(expiry, True),
            second[28:35].strip("<"),
            {"document_number": ok_number, "birth_date": ok_birth, "expiry_date": ok_expiry},
        )
    if len(lines) == 3 and len(lines[0]) == 30:
        first, second, third = lines
        number, ok_number = _checked(first[5:14], first[14], numeric=False)
        birth, ok_birth = _checked(second[0:6], second[6])
        expiry, ok_expiry = _checked(second[8:14], second[14])
        surname, given = _names(third)
        return MRZ(
            "TD1",
            first[0:2].strip("<"),
            first[2:5],
            surname,
            given,
            number.strip("<"),
            second[15:18],
            _date(birth, False),
            second[7].strip("<") or None,
            _date(expiry, True),
            (first[15:30] + second[18:29]).strip("<"),
            {"document_number": ok_number, "birth_date": ok_birth, "expiry_date": ok_expiry},
        )
    return None


def read_mrz(text: str) -> MRZ | None:
    lines = find_mrz_lines(text)
    return parse_mrz(lines) if lines else None


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
