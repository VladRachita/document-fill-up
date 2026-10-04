"""The electronic identity card (carte electronică de identitate, CEI) as the "RO CEI Reader"
application of the Ministry of Internal Affairs exports it.

The export is a PDF with a text layer that prints one label per line::

    Nume de familie: POPESCU
    Prenume: ION-ANDREI
    Cetățenie: ROU
    ...
    Locul nașterii: Jud.CJ Mun.Cluj-Napoca
    Număr document: CJ1234567
    Data emiterii: 01.02.2026
    Data expirării: 31.01.2036
    Autoritatea
    emitentă:
    SPCLEP Cluj-Napoca
    Domiciliu: Jud.CJ Mun.Cluj-Napoca Str.Exemplului nr.1 ap.2

The labels differ from the printed card ("Număr document" holds the series and the number,
"Autoritatea emitentă" may wrap over two lines), so the export has its own reader. The text is
digital, not read by OCR: every value is taken as printed.
"""

from __future__ import annotations

import re

from docfill.extraction.fields import FIELDS
from docfill.extraction.rules import validate
from docfill.extraction.text_utils import collapse, fold
from docfill.models import ExtractedField

ID_TYPE = "CEI"
CONFIDENCE = 0.97

# The footer every export carries.
SIGNATURE = re.compile(r"\bro\s+cei\s+reader\b")

# label (accent-free, lower case, words separated by any blank) -> field
_LABELS = {
    "nume de familie": "last_name",
    "prenume": "first_name",
    "cetatenie": "citizenship",
    "sex": "sex",
    "cnp": "cnp",
    "data nasterii": "date_of_birth",
    "locul nasterii": "place_of_birth",
    "numar document": "id_document",
    "data emiterii": "id_issue_date",
    "data expirarii": "id_expiry_date",
    "autoritatea emitenta": "id_issued_by",
    "domiciliu": "full_address",
}
_LABEL = re.compile(
    r"^[ \t]*(?P<label>"
    + "|".join(label.replace(" ", r"\s+") for label in sorted(_LABELS, key=len, reverse=True))
    + r")[ \t]*:",
    re.M,
)
# What follows the last value: the photo and the footer.
_END = re.compile(r"^[ \t]*(?:document\s+foto|acest\s+document\s+este\s+generat)", re.M)
# "CJ1234567": the series (two letters) and the number of the card.
_DOCUMENT = re.compile(r"^(?P<series>[A-Z]{2})\s*(?P<number>\d{6,9})$")
# The parts of a place or an address the card prints as "Jud.CJ Mun.X Str.Y nr.1 Bl.A sc.1":
# some PDF readers lose the blank before them ("Jud.CJMun.X", "Str.Ynr.1Bl.A").
_GLUED_PART = re.compile(
    r"(?<=[^\s.])(?=(?:Jud|Mun|Ora[sșş]|Com|Sat|Sec|Str|Bd|Ale|Aleea|Calea|[SȘŞ]os|nr|Nr|Bl|bl"
    r"|Sc|sc|Et|et|Ap|ap)\.)"
)


def is_export(text: str) -> bool:
    """Whether ``text`` is a "RO CEI Reader" export: its footer, or the labels only the export
    prints (when the footer is cut off)."""
    folded = fold(text)
    if SIGNATURE.search(collapse(folded)):
        return True
    labels = {_LABELS[collapse(match["label"])] for match in _LABEL.finditer(folded)}
    return {"id_document", "id_issued_by", "id_expiry_date"} <= labels


def read_labels(text: str) -> dict[str, str]:
    """The value printed after every label, up to the next label (values may wrap)."""
    folded = fold(text)
    end = _END.search(folded)
    stop = end.start() if end else len(text)
    matches = [match for match in _LABEL.finditer(folded) if match.start() < stop]
    values: dict[str, str] = {}
    for match, following in zip(matches, [*matches[1:], None], strict=True):
        value = collapse(text[match.end() : following.start() if following else stop])
        values.setdefault(_LABELS[collapse(match["label"])], value)
    return values


def extract(text: str, document: str | None = None) -> list[ExtractedField]:
    found: list[ExtractedField] = []

    def put(name: str, value: str, evidence: str) -> None:
        found.append(
            ExtractedField(
                name=name,
                value=value,
                confidence=CONFIDENCE,
                source="label",
                evidence=evidence,
                document=document,
            )
        )

    for name, raw in read_labels(text).items():
        if not raw:
            continue
        evidence = f"{name}: {raw}"
        if name == "id_document":
            if match := _DOCUMENT.match(raw.replace(" ", "").upper()):
                put("id_series", match["series"], evidence)
                put("id_number", match["number"], evidence)
                put("id_type", ID_TYPE, evidence)
            continue
        if name in ("place_of_birth", "full_address"):
            raw = _GLUED_PART.sub(" ", raw)
        if (value := validate(FIELDS[name], raw)) is not None:
            put(name, value, evidence)
    return found
