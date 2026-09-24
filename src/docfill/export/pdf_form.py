"""Existing PDF forms (AcroForm): inspect, blank, read and fill them.

Filling keeps the form editable (values are stored in the fields) *and* draws every value with
an embedded Unicode font, so Romanian diacritics (ă, â, î, ș, ț) display correctly in every
viewer. The forms' own ``/Helv`` font cannot encode ș/ț, which is why relying on the viewer to
regenerate appearances loses them.
"""

from __future__ import annotations

import io
import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    TextStringObject,
)

from docfill.errors import TemplateError

CHECKED_WORDS = {"x", "1", "yes", "da", "true", "on", "checked", "✓", "bifat"}
_MULTILINE_FLAG = 1 << 12


@dataclass
class FormField:
    """One field of a PDF form, with the printed text found to its left (its label)."""

    name: str
    kind: str  # "text" | "checkbox" | "radio" | "other"
    page: int
    rect: tuple[float, float, float, float]
    label: str = ""
    value: str = ""
    on_state: str | None = None


def _open(pdf_data: bytes) -> PdfReader:
    try:
        return PdfReader(io.BytesIO(pdf_data))
    except PdfReadError as exc:
        raise TemplateError(f"Invalid PDF form: {exc}") from exc


def _qualified_name(annotation: DictionaryObject) -> str:
    parts: list[str] = []
    node: DictionaryObject | None = annotation
    while node is not None:
        if node.get("/T") is not None:
            parts.append(str(node["/T"]))
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None else None
    return ".".join(reversed(parts))


def _inherited(annotation: DictionaryObject, key: str):
    node: DictionaryObject | None = annotation
    while node is not None:
        if node.get(key) is not None:
            return node[key]
        parent = node.get("/Parent")
        node = parent.get_object() if parent is not None else None
    return None


def _kind(annotation: DictionaryObject) -> str:
    field_type = _inherited(annotation, "/FT")
    if field_type == "/Tx":
        return "text"
    if field_type == "/Btn":
        flags = int(_inherited(annotation, "/Ff") or 0)
        return "radio" if flags & (1 << 15) else "checkbox"
    return "other"


def _on_state(annotation: DictionaryObject) -> str | None:
    appearance = annotation.get("/AP")
    if not appearance or "/N" not in appearance:
        return None
    states = [str(key) for key in appearance["/N"].get_object() if key != "/Off"]
    return states[0] if states else None


def _in_group(annotation: DictionaryObject) -> bool:
    """Several widgets sharing one field (a group of buttons)."""
    parent = annotation.get("/Parent")
    kids = parent.get_object().get("/Kids") if parent is not None else None
    return bool(kids) and len(kids) > 1 and annotation.get("/T") is None


def _field_value(annotation: DictionaryObject) -> str:
    value = _inherited(annotation, "/V")
    if value is None:
        return ""
    text = str(value)
    return "" if text in ("/Off", "Off") else text


def _widgets(reader: PdfReader):
    for page_index, page in enumerate(reader.pages):
        for reference in page.get("/Annots") or []:
            annotation = reference.get_object()
            if annotation.get("/Subtype") == "/Widget":
                yield page_index, annotation


def _clean_label(text: str) -> str:
    """Keep the words right before a field: drop dot leaders and earlier sentence parts."""
    text = re.sub(r"[.…_]{2,}", " ", text.replace("\r\n", " ").replace("\n", " "))
    text = re.split(r"[,;:]\s", text)[-1]
    text = re.sub(r"\(\d+\)|\s+", " ", text)
    return text.strip(" .,:;…")


def inspect_form(pdf_data: bytes) -> list[FormField]:
    """List every widget with its kind, position, current value and the label to its left."""
    import pypdfium2 as pdfium

    reader = _open(pdf_data)
    document = pdfium.PdfDocument(pdf_data)
    text_pages: dict[int, object] = {}
    fields: list[FormField] = []
    try:
        for page_index, annotation in _widgets(reader):
            x0, y0, x1, y1 = (float(v) for v in annotation["/Rect"])
            if page_index not in text_pages:
                text_pages[page_index] = document[page_index].get_textpage()
            text_page = text_pages[page_index]
            left = text_page.get_text_bounded(max(0.0, x0 - 170), y0 - 1, x0 + 1, y1 + 1)
            fields.append(
                FormField(
                    name=_qualified_name(annotation),
                    kind=_kind(annotation),
                    page=page_index + 1,
                    rect=(x0, y0, x1, y1),
                    label=_clean_label(left),
                    value=_field_value(annotation),
                    on_state=_on_state(annotation) if _kind(annotation) != "text" else None,
                )
            )
    finally:
        document.close()
    fields.sort(key=lambda f: (f.page, -round(f.rect[3] / 4), f.rect[0]))
    return fields


def list_text_fields(pdf_data: bytes) -> list[str]:
    fields = _open(pdf_data).get_fields() or {}
    return [name for name, field in fields.items() if field.get("/FT") == "/Tx"]


def list_fields(pdf_data: bytes) -> dict[str, str]:
    """All fillable fields: name -> "text" | "checkbox" | "radio"."""
    kinds: dict[str, str] = {}
    for _, annotation in _widgets(_open(pdf_data)):
        kind = _kind(annotation)
        if kind != "other":
            kinds.setdefault(_qualified_name(annotation), kind)
    return kinds


def read_form_values(pdf_data: bytes) -> dict[str, str]:
    """Values stored in the form's fields (text and ticked boxes); empty fields are omitted."""
    values: dict[str, str] = {}
    try:
        widgets = list(_widgets(_open(pdf_data)))
    except TemplateError:
        return values
    for _, annotation in widgets:
        name = _qualified_name(annotation)
        kind = _kind(annotation)
        value = _field_value(annotation).strip()
        if kind == "radio" or kind == "checkbox" and _in_group(annotation):
            # Option buttons (or check boxes sharing one field): the chosen button's state is
            # the group's value; some forms do not update each widget's /AS.
            state = _field_value(annotation) or str(annotation.get("/AS") or "")
            if state and state != "/Off":
                values[name] = state if state.startswith("/") else f"/{state}"
        elif kind == "checkbox":
            state = str(annotation.get("/AS") or "")
            if state and state != "/Off":
                values[name] = "x"
        elif kind == "text" and value:
            values[name] = value
    return values


def blank_form(pdf_data: bytes) -> bytes:
    """Remove every filled value (and its drawn appearance) and the document metadata, so a
    filled example can be stored as a reusable blank standard document."""
    writer = PdfWriter(clone_from=_open(pdf_data))
    for page in writer.pages:
        for reference in page.get("/Annots") or []:
            annotation = reference.get_object()
            if annotation.get("/Subtype") != "/Widget":
                continue
            kind = _kind(annotation)
            node: DictionaryObject | None = annotation
            while node is not None:  # the value may live on a parent field
                for key in ("/V", "/DV", "/RV"):
                    if key in node:
                        del node[key]
                parent = node.get("/Parent")
                node = parent.get_object() if parent is not None else None
            if kind == "text":
                annotation.pop("/AP", None)
            elif kind in ("checkbox", "radio"):
                annotation[NameObject("/AS")] = NameObject("/Off")
    root = writer._root_object
    for key in ("/Metadata", "/Outlines", "/PieceInfo"):
        root.pop(key, None)
    info = writer._info.get_object() if writer._info is not None else None
    if info is not None:  # drop author, producer, dates...
        for key in list(info.keys()):
            del info[key]
    writer.add_metadata({"/Producer": "docfill"})
    writer._root_object["/AcroForm"][NameObject("/NeedAppearances")] = _false()
    # The detached appearance streams still hold the old values: drop unreachable objects.
    writer.compress_identical_objects(remove_duplicates=False, remove_unreferenced=True)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _false():
    from pypdf.generic import BooleanObject

    return BooleanObject(False)


# --------------------------------------------------------------------------- appearances


@lru_cache
def _unicode_font(font_path: str | None) -> str:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if not font_path:
        return "Helvetica"
    pdfmetrics.registerFont(TTFont("DocFillForm", font_path))
    return "DocFillForm"


def _font_size(annotation: DictionaryObject, height: float) -> float:
    default_appearance = str(_inherited(annotation, "/DA") or "")
    match = re.search(r"([\d.]+)\s+Tf", default_appearance)
    size = float(match.group(1)) if match else 0.0
    return size if size > 0 else max(6.0, min(10.0, height * 0.72))


def _appearance_stream(
    writer: PdfWriter,
    text: str,
    width: float,
    height: float,
    size: float,
    multiline: bool,
    font_path: str | None,
) -> DecodedStreamObject:
    """Draw ``text`` with an embedded TrueType font and return it as a form XObject."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    font = _unicode_font(font_path)
    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(max(width, 1), max(height, 1)))
    lines = [text]
    if multiline:
        lines, current = [], ""
        for word in text.split():
            candidate = f"{current} {word}".strip()
            if current and stringWidth(candidate, font, size) > width - 4:
                lines.append(current)
                current = word
            else:
                current = candidate
        lines.append(current)
    else:
        while size > 5 and stringWidth(text, font, size) > width - 4:
            size -= 0.5  # shrink long values to fit the box
    pdf.setFont(font, size)
    if multiline:
        y = height - size - 1
        for line in lines:
            pdf.drawString(2, y, line)
            y -= size * 1.15
    else:
        pdf.drawString(2, max(1.0, (height - size) / 2 + size * 0.22), text)
    pdf.save()

    page = PdfReader(io.BytesIO(buffer.getvalue())).pages[0]
    stream = DecodedStreamObject()
    stream.set_data(page.get_contents().get_data())
    stream.update(
        {
            NameObject("/Type"): NameObject("/XObject"),
            NameObject("/Subtype"): NameObject("/Form"),
            NameObject("/BBox"): ArrayObject(
                [FloatObject(0), FloatObject(0), FloatObject(width), FloatObject(height)]
            ),
            NameObject("/Resources"): writer._add_object(page["/Resources"].clone(writer)),
        }
    )
    return stream


def fill_pdf_form(
    pdf_data: bytes,
    values: Mapping[str, str],
    metadata: Mapping[str, str] | None = None,
    font_path: str | Path | None = None,
) -> bytes:
    """Write ``values`` (PDF field name -> text; for check boxes a truthy word ticks them)
    into the form. The layout is untouched and the fields stay editable."""
    writer = PdfWriter(clone_from=_open(pdf_data))
    font = str(font_path) if font_path else None
    for page in writer.pages:
        for reference in page.get("/Annots") or []:
            annotation = reference.get_object()
            if annotation.get("/Subtype") != "/Widget":
                continue
            name = _qualified_name(annotation)
            if name not in values:
                continue
            kind = _kind(annotation)
            value = str(values[name])
            target = annotation if annotation.get("/T") is not None else annotation["/Parent"]
            target = target.get_object()
            if kind == "radio" or value.startswith("/"):  # state of the chosen button, "/v3"
                on_state = _on_state(annotation)
                chosen = on_state is not None and on_state == value.strip()
                annotation[NameObject("/AS")] = NameObject(on_state if chosen else "/Off")
                if chosen:
                    target[NameObject("/V")] = NameObject(on_state)
            elif kind == "checkbox":
                on_state = _on_state(annotation)
                checked = value.strip().lower() in CHECKED_WORDS and on_state
                state = NameObject(on_state if checked else "/Off")
                annotation[NameObject("/AS")] = state
                target[NameObject("/V")] = state
            elif kind == "text":
                target[NameObject("/V")] = TextStringObject(value)
                x0, y0, x1, y1 = (float(v) for v in annotation["/Rect"])
                width, height = abs(x1 - x0), abs(y1 - y0)
                multiline = bool(int(_inherited(annotation, "/Ff") or 0) & _MULTILINE_FLAG)
                stream = _appearance_stream(
                    writer, value, width, height, _font_size(annotation, height), multiline, font
                )
                annotation[NameObject("/AP")] = DictionaryObject(
                    {NameObject("/N"): writer._add_object(stream)}
                )
    acroform = writer._root_object.get("/AcroForm")
    if acroform is not None:
        acroform.get_object()[NameObject("/NeedAppearances")] = _false()
    if metadata:
        writer.add_metadata(dict(metadata))
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


__all__ = [
    "CHECKED_WORDS",
    "FormField",
    "blank_form",
    "fill_pdf_form",
    "inspect_form",
    "list_fields",
    "list_text_fields",
    "read_form_values",
]
