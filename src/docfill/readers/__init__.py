"""Document readers. The file type is sniffed from its content, not trusted from its name."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from docfill.config import Settings, get_settings
from docfill.errors import DocumentReadError, UnsupportedDocumentError
from docfill.models import DocumentType, RawDocument

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".png", ".jpg", ".jpeg"}


def detect_type(data: bytes, filename: str = "") -> DocumentType:
    suffix = Path(filename).suffix.lower()
    if suffix and suffix not in SUPPORTED_EXTENSIONS:
        raise UnsupportedDocumentError(
            f"{filename}: unsupported file type '{suffix}'. "
            f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}"
        )
    if b"%PDF-" in data[:1024]:
        return DocumentType.PDF
    if data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff"):
        return DocumentType.IMAGE
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                if "word/document.xml" in archive.namelist():
                    return DocumentType.DOCX
        except zipfile.BadZipFile:
            pass
    if data.startswith(b"\xd0\xcf\x11\xe0"):
        raise UnsupportedDocumentError(
            f"{filename}: legacy Word .doc files are not supported, save the file as .docx"
        )
    raise UnsupportedDocumentError(f"{filename or 'document'}: unrecognised file content")


def read_bytes(data: bytes, filename: str, settings: Settings | None = None) -> RawDocument:
    settings = settings or get_settings()
    if not data:
        raise DocumentReadError(f"{filename}: file is empty")
    if len(data) > settings.max_file_size:
        raise DocumentReadError(
            f"{filename}: file is larger than the {settings.max_file_size} bytes limit"
        )
    doc_type = detect_type(data, filename)
    if doc_type is DocumentType.PDF:
        from docfill.readers.pdf import read_pdf

        return read_pdf(data, filename, settings)
    if doc_type is DocumentType.DOCX:
        from docfill.readers.docx import read_docx

        return read_docx(data, filename)
    from docfill.readers.image import read_image

    return read_image(data, filename, settings)


def read_document(path: str | Path, settings: Settings | None = None) -> RawDocument:
    settings = settings or get_settings()
    path = Path(path)
    if not path.is_file():
        raise DocumentReadError(f"{path}: file not found")
    if path.stat().st_size > settings.max_file_size:
        raise DocumentReadError(
            f"{path.name}: file is larger than the {settings.max_file_size} bytes limit"
        )
    return read_bytes(path.read_bytes(), path.name, settings)


__all__ = ["SUPPORTED_EXTENSIONS", "detect_type", "read_bytes", "read_document"]
