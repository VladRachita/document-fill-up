"""Exception hierarchy used across the package."""

from __future__ import annotations


class DocFillError(Exception):
    """Base class for all docfill errors."""


class UnsupportedDocumentError(DocFillError):
    """The input file type cannot be read."""


class DocumentReadError(DocFillError):
    """The input file is corrupt, too large or otherwise unreadable."""


class OCRUnavailableError(DocFillError):
    """OCR was required but Tesseract is not installed."""


class TemplateError(DocFillError):
    """A standard document is invalid or cannot be found."""


class TemplateNotFoundError(TemplateError):
    pass


class TemplateIntegrityError(TemplateError):
    """The stored standard document no longer matches its checksum."""


class MissingFieldsError(DocFillError):
    """Required fields of a standard document could not be filled."""

    def __init__(self, missing: list[str]):
        self.missing = missing
        super().__init__("Missing values for required fields: " + ", ".join(missing))


class KnowledgeError(DocFillError):
    """A knowledge entry (legal form, procedure, rule, document type) is invalid."""


class KnowledgeNotFoundError(KnowledgeError):
    pass
