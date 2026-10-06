"""Read and write knowledge as YAML (or JSON, which is valid YAML)."""

from __future__ import annotations

from importlib import resources
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from docfill.errors import KnowledgeError
from docfill.knowledge.specs import AnySpec, KnowledgeFile

# The usual validation problems in Romanian, for the people who write the knowledge (any
# other one as pydantic words it).
_MESSAGES = {
    "missing": "câmp obligatoriu lipsă",
    "extra_forbidden": "câmp necunoscut (nu este permis aici)",
    "string_type": "trebuie să fie un text",
    "list_type": "trebuie să fie o listă",
    "dict_type": "trebuie să fie o structură cu chei și valori",
    "model_type": "trebuie să fie o structură cu chei și valori",
    "int_type": "trebuie să fie un număr întreg",
    "int_parsing": "trebuie să fie un număr întreg",
    "float_type": "trebuie să fie un număr",
    "float_parsing": "trebuie să fie un număr",
    "bool_type": "trebuie să fie true sau false",
    "bool_parsing": "trebuie să fie true sau false",
    "string_too_short": "textul este prea scurt",
    "string_too_long": "textul este prea lung",
    "string_pattern_mismatch": "formatul nu este valid",
}


def _message(error: Any) -> str:
    kind, context = error["type"], error.get("ctx") or {}
    if kind == "value_error" and "error" in context:
        return str(context["error"])
    if kind == "literal_error" and "expected" in context:
        return f"valoare nepermisă; se acceptă: {context['expected'].replace(' or ', ' sau ')}"
    return _MESSAGES.get(kind, error["msg"])


def _readable(exc: ValidationError) -> str:
    lines = []
    for error in exc.errors(include_url=False)[:15]:
        where = ".".join(str(part) for part in error["loc"])
        lines.append(f"  {where}: {_message(error)}")
    return "\n".join(lines)


def parse_knowledge(data: Any, origin: str = "knowledge") -> list[AnySpec]:
    if data is None:
        return []
    if not isinstance(data, dict):
        raise KnowledgeError(f"{origin}: se aștepta o structură cu entities / procedures / rules")
    try:
        return list(KnowledgeFile.model_validate(data).entries())
    except ValidationError as exc:
        raise KnowledgeError(f"{origin}: cunoștințe invalide\n{_readable(exc)}") from exc


def load_text(text: str, origin: str = "knowledge") -> list[AnySpec]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise KnowledgeError(f"{origin}: YAML invalid ({exc})") from exc
    return parse_knowledge(data, origin)


def load_file(path: str | Path) -> list[AnySpec]:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise KnowledgeError(f"{path}: nu poate fi citit ({exc})") from exc
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
