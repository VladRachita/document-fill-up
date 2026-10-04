"""Synthetic sample documents (fictitious people only) for tests, demos and evaluation.

The Romanian identity card follows the layout of the real card: multilingual labels with the
value underneath, ``SERIA AX NR 123456``, the CNP, two columns, a tinted guilloche background
and a two-line machine readable zone with valid check digits.
"""

from __future__ import annotations

import io
import math
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from PIL import Image, ImageDraw, ImageFont

from docfill.mrz import make_td2
from docfill.ro import CNP_COUNTIES, cnp_control_digit, county_name

_FONT_CANDIDATES = {
    "sans": ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans.ttf", "arial.ttf"),
    "bold": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "DejaVuSans-Bold.ttf",
        "arialbd.ttf",
    ),
    "mono": (
        "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
        "DejaVuSansMono.ttf",
        "cour.ttf",
    ),
}


def _font(kind: str, size: int) -> ImageFont.ImageFont:
    for path in _FONT_CANDIDATES[kind]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _cnp_county_digits(code: str) -> str:
    """The two county digits (JJ) of a CNP for a county code printed on cards (``SB`` -> ``32``);
    Sibiu when the code is unknown."""
    name = county_name(code)
    for number, county in CNP_COUNTIES.items():
        if county == name:
            return f"{number:02d}"
    return "32"


@dataclass
class Person:
    """A fictitious person; defaults produce a consistent, valid identity."""

    last_name: str = "POPESCU"
    first_name: str = "ION-ANDREI"
    sex: str = "M"
    birth: date = date(1987, 11, 14)
    birth_county_code: str = "SB"
    birth_locality: str = "Mun.Sibiu"
    county_code: str = "CJ"
    locality: str = "Mun.Cluj-Napoca"
    street_line: str = "Str.Florilor nr.5 bl.A2 sc.1 et.3 ap.10"
    series: str = "AX"
    number: str = "123456"
    issued_by: str = "SPCLEP Cluj-Napoca"
    issued: date = date(2022, 6, 22)
    expires: date = date(2032, 11, 14)
    serial: str = "123"
    extra: dict[str, str] = field(default_factory=dict)
    # The lines exactly as printed, for layouts the fields above cannot express (Bucharest prints
    # "Mun.București Sec.2" without a county); they replace the generated lines.
    birth_line: str | None = None
    domicile_lines: tuple[str, ...] | None = None

    @property
    def birth_place_line(self) -> str:
        return self.birth_line or f"Jud.{self.birth_county_code} {self.birth_locality}"

    @property
    def domicile_card_lines(self) -> list[str]:
        if self.domicile_lines is not None:
            return list(self.domicile_lines)
        return [f"Jud.{self.county_code} {self.locality}", self.street_line]

    @property
    def cnp(self) -> str:
        century_digit = {("M", 19): "1", ("F", 19): "2", ("M", 20): "5", ("F", 20): "6"}[
            (self.sex, self.birth.year // 100)
        ]
        county = _cnp_county_digits(self.birth_county_code)
        first12 = f"{century_digit}{self.birth:%y%m%d}{county}{self.serial}"
        return first12 + cnp_control_digit(first12)

    def mrz(self) -> list[str]:
        optional = self.cnp[0] + self.cnp[7:13]

        def latin(name: str) -> str:  # the MRZ has no diacritics: ȘTEFĂNESCU -> STEFANESCU
            decomposed = unicodedata.normalize("NFD", name.replace("-", " "))
            return "".join(c for c in decomposed if unicodedata.category(c) != "Mn")

        return make_td2(
            latin(self.last_name),
            latin(self.first_name),
            self.series + self.number,
            "ROU",
            f"{self.birth:%y%m%d}",
            self.sex,
            f"{self.expires:%y%m%d}",
            optional,
        )


def ro_id_card(person: Person | None = None, noise: bool = True) -> Image.Image:
    person = person or Person()
    width, height = 1720, 1100
    image = Image.new("RGB", (width, height), (214, 236, 232))
    draw = ImageDraw.Draw(image)
    if noise:  # guilloche-like background lines, as on the real card
        for k in range(0, height, 9):
            points = [(x, k + 6 * math.sin(x / 37 + k / 50)) for x in range(0, width, 12)]
            draw.line(points, fill=(190, 222, 218), width=2)
    draw.ellipse((60, 230, 460, 760), fill=(245, 245, 245))  # photo placeholder

    sans, bold, label, mono = (
        _font("sans", 38),
        _font("bold", 52),
        _font("sans", 26),
        _font("mono", 50),
    )
    ink, label_ink = (20, 20, 20), (40, 60, 120)
    draw.text((40, 30), "ROUMANIE", font=bold, fill=ink)
    draw.text((720, 30), "ROMÂNIA", font=bold, fill=(30, 60, 160))
    draw.text((1330, 30), "ROMANIA", font=bold, fill=ink)
    draw.text((560, 110), "CARTE DE IDENTITATE", font=_font("bold", 40), fill=label_ink)
    draw.text((1330, 110), "IDENTITY CARD", font=_font("bold", 30), fill=label_ink)
    draw.text((640, 165), f"SERIA {person.series}  NR {person.number}", font=sans, fill=ink)
    draw.text((500, 220), f"CNP {person.cnp}", font=sans, fill=ink)

    x, y = 500, 285

    def item(title: str, lines: list[str], at: tuple[int, int]) -> None:
        tx, ty = at
        draw.text((tx, ty), title, font=label, fill=label_ink)
        for index, line in enumerate(lines):
            draw.text((tx, ty + 34 + index * 46), line, font=sans, fill=ink)

    item("Nume/Nom/Last name", [person.last_name], (x, y))
    item("Prenume/Prenom/First name", [person.first_name], (x, y + 95))
    item("Cetățenie/Nationalite/Nationality", ["Română / ROU"], (x, y + 190))
    item("Sex/Sexe/Sex", [person.sex], (1400, y + 190))
    item(
        "Loc naștere/Lieu de naissance/Place of birth",
        [person.birth_place_line],
        (x, y + 285),
    )
    item("Domiciliu/Adresse/Address", person.domicile_card_lines, (x, y + 380))
    item("Emisă de/Delivree par/Issued by", [person.issued_by], (x, y + 520))
    item(
        "Valabilitate/Validite/Validity",
        [f"{person.issued:%d.%m.%y}-{person.expires:%d.%m.%Y}"],
        (1250, y + 520),
    )
    for index, line in enumerate(person.mrz()):
        draw.text((60, 940 + index * 70), line, font=mono, fill=ink)
    return image


def ro_id_card_jpeg(person: Person | None = None, quality: int = 88) -> bytes:
    buffer = io.BytesIO()
    ro_id_card(person).save(buffer, format="JPEG", quality=quality)
    return buffer.getvalue()


# --------------------------------------------------------------------------- sworn statement


def _spaced(text: str) -> str:
    """Card style ``Str.Florilor nr.5 bl.A2`` -> document style ``Str. Florilor nr. 5, bl. A2``."""
    text = re.sub(r"\.(?=\S)", ". ", text)
    return re.sub(r"\s+(?=(?:bl|sc|et|ap)\.)", ", ", text)


def sworn_statement_text(
    person: Person | None = None,
    company: str = "EXEMPLU INVEST S.A.",
    capacity: str = "administrator",
    signed: date = date(2026, 9, 25),
) -> list[str]:
    """The paragraphs of an administrator's sworn statement (declarație pe propria răspundere)
    written the way the filers of the trade register write it: the person is identified in one
    sentence (CNP, domicile, birth, identity card), then the declaration."""
    person = person or Person()
    county = county_name(person.county_code) or person.county_code
    birth_county = county_name(person.birth_county_code) or person.birth_county_code
    a = "ă" if person.sex == "F" else ""  # "născut" / "născută"...
    name = f"{person.last_name} {person.first_name}"
    return [
        "DECLARAȚIE PE PROPRIE RĂSPUNDERE",
        f"{name}, CNP {person.cnp}, cu domiciliul în {_spaced(person.locality)}, "
        f"{_spaced(person.street_line)}, jud. {county}, țara România, cetățenia Română, "
        f"născut{a} în {_spaced(person.birth_locality)}, jud. {birth_county}, țara România, "
        f"la data de {person.birth:%d.%m.%Y}, identificat{a} prin CI, seria {person.series}, "
        f"nr. {person.number}, emisă de {person.issued_by}, la data de {person.issued:%d.%m.%Y}, "
        f"valabilă până la data de {person.expires:%d.%m.%Y}, în calitate de {capacity} numit "
        f"al societății {company} (în curs de constituire).",
        "Declar pe proprie răspundere, cunoscând prevederile articolului 326 din Codul Penal "
        "privind falsul în declarații, că îndeplinesc toate condițiile legale pentru a deține "
        f"calitatea de {capacity}, aşa cum sunt acestea prevăzute de Legea societăților nr. "
        "31/1990, republicată, cu modificările şi completările ulterioare, şi nu mă aflu în "
        "nicio situație de incapacitate sau incompatibilitate prevăzută de lege.",
        "Dau prezenta declarație fiindu-mi necesară la Oficiul Național al Registrului "
        "Comerțului pentru înmatricularea societății mai sus-menționate.",
        f"Data: {signed:%d.%m.%Y}",
        f"Nume şi prenume: {name}",
        "Semnătura:",
    ]


def sworn_statement_docx(person: Person | None = None, **kwargs) -> bytes:
    """:func:`sworn_statement_text` as a Word document."""
    from docx import Document

    document = Document()
    for paragraph in sworn_statement_text(person, **kwargs):
        document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()
