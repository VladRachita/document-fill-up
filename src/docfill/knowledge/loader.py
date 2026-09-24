"""Read and write knowledge as YAML (or JSON, which is valid YAML)."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from docfill.errors import KnowledgeError
from docfill.knowledge.specs import AnySpec, KnowledgeFile


def _readable(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors(include_url=False)[:15]:
        where = ".".join(str(part) for part in error["loc"])
        lines.append(f"  {where}: {error['msg']}")
    return "\n".join(lines)


def parse_knowledge(data: Any, origin: str = "knowledge") -> list[AnySpec]:
    if data is None:
        return []
    if not isinstance(data, dict):
        raise KnowledgeError(f"{origin}: expected a mapping with entities / procedures / rules")
    try:
        return list(KnowledgeFile.model_validate(data).entries())
    except ValidationError as exc:
        raise KnowledgeError(f"{origin}: invalid knowledge\n{_readable(exc)}") from exc


def load_text(text: str, origin: str = "knowledge") -> list[AnySpec]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise KnowledgeError(f"{origin}: not valid YAML ({exc})") from exc
    return parse_knowledge(data, origin)


def load_file(path: str | Path) -> list[AnySpec]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise KnowledgeError(f"{path}: cannot read ({exc})") from exc
    return load_text(text, str(path))


def load_directory(directory: str | Path) -> list[AnySpec]:
    directory = Path(directory)
    files = sorted([*directory.glob("*.yaml"), *directory.glob("*.yml")])
    return [spec for path in files for spec in load_file(path)]


def bundled_dir() -> Path:
    return Path(str(resources.files("docfill") / "knowledge" / "bundled"))


def dump_yaml(specs: list[AnySpec]) -> str:
    return yaml.safe_dump(
        KnowledgeFile.of(specs).dump(), allow_unicode=True, sort_keys=False, width=100
    )
