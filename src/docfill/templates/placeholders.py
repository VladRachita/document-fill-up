"""Placeholder syntax of standard documents.

``{{ first_name }}`` inserts a value, ``{{ last_name | upper }}`` applies a formatting filter.
Rendering is a single regex substitution over the stored text: everything outside the
placeholders is copied verbatim, and inserted values are never re-interpreted, so the output
is always exactly the standard document plus the filled values.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping

from docfill.errors import TemplateError

PLACEHOLDER_RE = re.compile(
    r"\{\{\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*(?:\|\s*(?P<filter>[A-Za-z_]+)\s*)?\}\}"
)
FILTERS: dict[str, Callable[[str], str]] = {
    "upper": str.upper,
    "lower": str.lower,
    "title": str.title,
}
BLANK = "_" * 20
MAX_VALUE_LENGTH = 300


def parse_expression(expression: str) -> tuple[str, str | None]:
    """Parse ``name`` or ``name | filter`` (the inside of a placeholder)."""
    match = PLACEHOLDER_RE.fullmatch("{{" + expression + "}}")
    if not match:
        raise TemplateError(f"Invalid field expression: {expression!r}")
    if match["filter"] and match["filter"] not in FILTERS:
        raise TemplateError(f"Unknown filter '{match['filter']}' (available: {', '.join(FILTERS)})")
    return match["name"], match["filter"]


def validate_body(body: str) -> list[str]:
    """Check the placeholder syntax and return the referenced field names (in order)."""
    names: list[str] = []
    for match in PLACEHOLDER_RE.finditer(body):
        parse_expression(match.group()[2:-2])
        if match["name"] not in names:
            names.append(match["name"])
    leftover = PLACEHOLDER_RE.sub("", body)
    if "{{" in leftover or "}}" in leftover:
        raise TemplateError("Malformed placeholder: every '{{' needs a matching '}}'")
    return names


def clean_fill_value(value: object) -> str:
    """Values are single-line plain text: no control characters, collapsed whitespace."""
    text = "".join(
        " " if unicodedata.category(char).startswith(("C", "Z")) else char for char in str(value)
    )
    return " ".join(text.split())[:MAX_VALUE_LENGTH]


def apply(expression: str, values: Mapping[str, str]) -> str | None:
    name, filter_name = parse_expression(expression)
    value = values.get(name)
    if not value:
        return None
    return FILTERS[filter_name](value) if filter_name else value


def render_body(body: str, values: Mapping[str, str], blank: str = BLANK) -> str:
    """Substitute placeholders; unresolved ones become a blank line to fill in by hand."""

    def replace(match: re.Match[str]) -> str:
        return apply(match.group()[2:-2], values) or blank

    return PLACEHOLDER_RE.sub(replace, body)
