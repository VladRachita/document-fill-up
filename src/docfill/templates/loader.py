"""Load standard document specs from YAML files."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import yaml
from pydantic import ValidationError

from docfill.errors import TemplateError
from docfill.templates.models import StandardDocumentSpec


def load_spec(path: str | Path) -> StandardDocumentSpec:
    """Read a ``.yaml`` spec. A ``pdf_file`` key is resolved relative to the YAML file."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise TemplateError(f"{path}: cannot read standard document ({exc})") from exc
    if not isinstance(data, dict):
        raise TemplateError(f"{path}: expected a YAML mapping")
    if pdf_file := data.pop("pdf_file", None):
        pdf_path = (path.parent / pdf_file).resolve()
        try:
            data["pdf_data"] = pdf_path.read_bytes()
        except OSError as exc:
            raise TemplateError(f"{path}: cannot read pdf_file {pdf_path} ({exc})") from exc
    try:
        return StandardDocumentSpec.model_validate(data)
    except ValidationError as exc:
        raise TemplateError(f"{path}: invalid standard document\n{exc}") from exc


def bundled_specs_dir() -> Path:
    return Path(str(resources.files("docfill") / "standard_documents"))


def load_directory(directory: str | Path) -> list[StandardDocumentSpec]:
    directory = Path(directory)
    files = sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")])
    return [load_spec(path) for path in files]
