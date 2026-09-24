"""Word (.docx) reader.

Body paragraphs and tables are read in document order. Page headers/footers are skipped on
purpose: they usually carry letterhead boilerplate (company address, logos) that would only
confuse field extraction.
"""

from __future__ import annotations

import io
import zipfile

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from docfill.errors import DocumentReadError
from docfill.models import DocumentType, Page, RawDocument


def _row_cells(row) -> list[str]:
    cells: list[str] = []
    for cell in row.cells:
        text = " ".join(cell.text.split())
        # Merged cells are repeated by python-docx; keep one copy.
        if not cells or cells[-1] != text:
            cells.append(text)
    return cells


def table_to_lines(table: Table) -> list[str]:
    """Flatten a table into ``label: value`` lines the field extractor understands.

    * two-column tables are treated as key/value rows;
    * wider tables with a header row emit ``header: value`` for every data row.
    """
    rows = [_row_cells(row) for row in table.rows]
    rows = [row for row in rows if any(row)]
    if not rows:
        return []
    lines: list[str] = []
    width = max(len(row) for row in rows)
    if width == 2:
        for row in rows:
            key, value = (row + [""])[:2]
            lines.append(f"{key}: {value}" if key and value else key or value)
    elif len(rows) > 1 and all(rows[0]):
        header = rows[0]
        for row in rows[1:]:
            pairs = zip(header, row, strict=False)
            lines.extend(f"{key}: {value}" for key, value in pairs if key and value)
            lines.append("")
    else:
        lines.extend(" | ".join(cell for cell in row if cell) for row in rows)
    return lines


def read_docx(data: bytes, source: str) -> RawDocument:
    try:
        document = Document(io.BytesIO(data))
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise DocumentReadError(f"{source}: invalid Word document ({exc})") from exc

    lines: list[str] = []
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph):
            lines.append(block.text)
        elif isinstance(block, Table):
            lines.extend(table_to_lines(block))
    return RawDocument(
        source=source,
        doc_type=DocumentType.DOCX,
        pages=[Page(number=1, text="\n".join(lines))],
    )
