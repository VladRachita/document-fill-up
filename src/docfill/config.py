"""Application settings, overridable through ``DOCFILL_*`` environment variables or a ``.env``."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "C:/Windows/Fonts/arial.ttf",
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DOCFILL_", env_file=".env", extra="ignore")

    # Storage of the standard documents. Any SQLAlchemy URL works, e.g.
    # postgresql+psycopg://user:pass@host/db
    database_url: str = "sqlite:///./docfill.db"

    # OCR (Tesseract). ``ocr_languages`` uses Tesseract codes joined by '+', e.g. "eng+ron".
    tesseract_cmd: str | None = None
    ocr_languages: str = "eng"
    ocr_dpi: int = 300
    # A PDF page with fewer extractable characters than this is treated as scanned and OCR'd.
    pdf_min_text_chars: int = 20

    # Machine learning (spaCy NER). Set to "" to disable the NER extractor.
    spacy_model: str = "en_core_web_sm"

    # Extracted values below this confidence are ignored when filling documents.
    min_confidence: float = 0.5

    # Uploads larger than this are rejected (bytes).
    max_file_size: int = 25 * 1024 * 1024

    # TrueType font used for exported PDFs. Needed for non Latin-1 characters (e.g. ă, ș, ț).
    # When unset, a few common system locations are probed and Helvetica is the fallback.
    pdf_font_path: Path | None = None

    def resolve_font_path(self) -> Path | None:
        if self.pdf_font_path:
            return self.pdf_font_path if self.pdf_font_path.is_file() else None
        for candidate in _DEFAULT_FONT_CANDIDATES:
            path = Path(candidate)
            if path.is_file():
                return path
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
