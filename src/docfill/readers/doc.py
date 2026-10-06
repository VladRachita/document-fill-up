"""Legacy Word (.doc) reader.

A .doc file is converted with a tool installed on the machine: ``antiword`` (small, reads the
text) or LibreOffice (``soffice``, converts to .docx, then read like any .docx so tables are
kept). Without either, the file is refused with a message saying how to proceed.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from docfill.errors import DocumentReadError, UnsupportedDocumentError
from docfill.models import DocumentType, Page, RawDocument

TIMEOUT_SECONDS = 120


def doc_converter() -> str | None:
    """The tool that will read .doc files: ``antiword``, ``soffice`` or ``None``."""
    for tool in ("antiword", "soffice", "libreoffice"):
        if shutil.which(tool):
            return tool
    return None


def _with_libreoffice(tool: str, data: bytes, source: str) -> RawDocument:
    from docfill.readers.docx import read_docx

    with tempfile.TemporaryDirectory(prefix="docfill-doc-") as folder:
        path = Path(folder) / "document.doc"
        path.write_bytes(data)
        command = [tool, "--headless", "--norestore", "--convert-to", "docx", "--outdir", folder]
        # LibreOffice needs a writable profile: keep it in the temporary folder.
        environment = {"HOME": folder, "PATH": _path()}
        try:
            subprocess.run(
                [*command, str(path)],
                capture_output=True,
                timeout=TIMEOUT_SECONDS,
                check=True,
                env=environment,
            )
            converted = (Path(folder) / "document.docx").read_bytes()
        except (subprocess.SubprocessError, OSError) as exc:
            raise DocumentReadError(
                f"{source}: fișierul .doc nu a putut fi convertit ({exc})"
            ) from exc
    raw = read_docx(converted, source)
    return raw.model_copy(update={"doc_type": DocumentType.DOC})


def _path() -> str:
    import os

    return os.environ.get("PATH", "/usr/bin:/bin")


def _with_antiword(data: bytes, source: str) -> RawDocument:
    try:
        completed = subprocess.run(
            ["antiword", "-w", "0", "-m", "UTF-8.txt", "-"],
            input=data,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise DocumentReadError(f"{source}: fișierul .doc nu a putut fi citit ({exc})") from exc
    text = completed.stdout.decode("utf-8", errors="replace")
    lines = [line.rstrip() for line in text.splitlines()]
    return RawDocument(
        source=source,
        doc_type=DocumentType.DOC,
        pages=[Page(number=1, text="\n".join(lines).strip())],
    )


def read_doc(data: bytes, source: str) -> RawDocument:
    tool = doc_converter()
    if tool is None:
        raise UnsupportedDocumentError(
            f"{source}: citirea fișierelor Word vechi (.doc) necesită antiword sau LibreOffice "
            "(de exemplu, apt install antiword); alternativ, salvați fișierul ca .docx"
        )
    if tool == "antiword":
        return _with_antiword(data, source)
    return _with_libreoffice(tool, data, source)
