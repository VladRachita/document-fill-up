"""Generate sample input documents to try docfill with.

python examples/make_samples.py
docfill extract examples/samples/*
"""

from __future__ import annotations

import io
from pathlib import Path

from docx import Document
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent / "samples"


def _font(size: int) -> ImageFont.ImageFont:
    for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def id_card_image() -> Image.Image:
    lines = [
        "IDENTITY CARD",
        "Surname: SMITH",
        "Given names: John Michael",
        "Place of birth: Manchester",
        "Address: 12 Baker Street, London NW1 6XE, United Kingdom",
    ]
    image = Image.new("RGB", (1200, 90 + 70 * len(lines)), "white")
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((40, 40 + 70 * index), line, fill="black", font=_font(28))
    return image


def main() -> None:
    OUT.mkdir(exist_ok=True)

    # 1. A photographed / scanned ID card (JPEG) -> OCR
    id_card_image().save(OUT / "id_card.jpg", quality=90)

    # 2. The same card inside a PDF without a text layer (scanner output) -> OCR
    buffer = io.BytesIO()
    id_card_image().save(buffer, format="PNG")
    pdf = canvas.Canvas(str(OUT / "scanned_id_card.pdf"), pagesize=A4)
    pdf.drawImage(ImageReader(io.BytesIO(buffer.getvalue())), 20, 450, width=560, height=205)
    pdf.save()

    # 3. A Romanian application form in Word with a key/value table
    document = Document()
    document.add_heading("Cerere / Application", 1)
    document.add_paragraph("Vă rog să completați datele de mai jos.")
    table = document.add_table(rows=0, cols=2)
    for key, value in [
        ("Nume", "POPESCU"),
        ("Prenume", "Ion Andrei"),
        ("Domiciliu", "Str. Florilor nr. 5, 400001 Cluj-Napoca, jud. Cluj, România"),
        ("Locul nașterii", "Brașov"),
        ("IBAN", "RO49 AAAA 1B31 0075 9384 0000"),
    ]:
        cells = table.add_row().cells
        cells[0].text, cells[1].text = key, value
    document.save(OUT / "application_ro.docx")

    # 4. A free-text letter (no labels) -> ML named-entity recognition
    pdf = canvas.Canvas(str(OUT / "letter.pdf"), pagesize=A4)
    text = pdf.beginText(60, 780)
    for line in [
        "Dear Sir or Madam,",
        "",
        "My name is Maria Garcia and I was born in Madrid. I currently live in Berlin,",
        "Germany, at Hauptstraße 5. Please update your records accordingly.",
        "",
        "Kind regards,",
        "Maria Garcia",
    ]:
        text.textLine(line)
    pdf.drawText(text)
    pdf.save()

    for path in sorted(OUT.iterdir()):
        print(path)


if __name__ == "__main__":
    main()
