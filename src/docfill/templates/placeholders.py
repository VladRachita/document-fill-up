"""Placeholder syntax of standard documents.

``{{ first_name }}`` inserts a value, ``{{ last_name | upper }}`` applies a formatting filter.
Rendering is a single regex substitution over the stored text: everything outside the
placeholders is copied verbatim, and inserted values are never re-interpreted, so the output
is always exactly the standard document plus the filled values.

A line can start with a condition; it is kept only when the condition holds (the marker itself
is removed), so one document covers the variants of a model (e.g. a board of directors or a sole
administrator):

* ``[[p2_shares]] ...`` - the field has a value;
* ``[[general_director|p2_general_director]] ...`` - any of the fields has a value;
* ``[[administration=administrator unic]] ...`` - the field has this value (case and accents
  ignored);
* ``[[!name]]`` / ``[[!name=value]]`` - the opposite.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable, Mapping

from docfill.errors import TemplateError
from docfill.templates.markup import classify_line

# Placeholders stay on one line (spaces/tabs only inside the braces).
PLACEHOLDER_RE = re.compile(
    r"\{\{[ \t]*(?P<name>[A-Za-z_][A-Za-z0-9_]*)[ \t]*(?:\|[ \t]*(?P<filter>[A-Za-z_]+)[ \t]*)?\}\}"
)
FILTERS: dict[str, Callable[[str], str]] = {
    "upper": str.upper,
    "lower": str.lower,
    "title": str.title,
}
CONDITION_RE = re.compile(
    r"^(?P<indent>[ \t]*)\[\[(?P<negate>!?)(?P<names>[A-Za-z_][A-Za-z0-9_]*"
    r"(?:\|[A-Za-z_][A-Za-z0-9_]*)*)(?:=(?P<value>[^\]\n]*))?\]\][ \t]?"
)
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
    """Check the placeholder and condition syntax and return the referenced field names (in
    order, those of conditions included)."""
    for line in body.splitlines():
        if line.lstrip().startswith("[[") and not CONDITION_RE.match(line):
            raise TemplateError(f"Malformed condition: {line.strip()[:60]!r}")
        match = CONDITION_RE.match(line)
        if match and match["value"] is not None and "|" in match["names"]:
            raise TemplateError(f"A condition with '=' takes one field: {line.strip()[:60]!r}")
    for match in PLACEHOLDER_RE.finditer(body):
        parse_expression(match.group()[2:-2])
    leftover = PLACEHOLDER_RE.sub("", body)
    if "{{" in leftover or "}}" in leftover:
        raise TemplateError("Malformed placeholder: every '{{' needs a matching '}}'")
    return referenced_names(body)


def referenced_names(body: str) -> list[str]:
    """Field names used by the placeholders and the line conditions, in document order."""
    names: list[str] = []
    for line in body.splitlines():
        found = []
        if match := CONDITION_RE.match(line):
            found += match["names"].split("|")
        found += [m["name"] for m in PLACEHOLDER_RE.finditer(line)]
        names += [name for name in dict.fromkeys(found) if name not in names]
    return names


def line_condition(line: str, values: Mapping[str, str]) -> tuple[bool, str]:
    """Whether ``line`` is kept, and the line without its condition marker."""
    match = CONDITION_RE.match(line)
    if not match:
        return True, line
    names = match["names"].split("|")
    if match["value"] is not None:
        holds = _fold(values.get(names[0]) or "") == _fold(match["value"])
    else:
        holds = any((values.get(name) or "").strip() for name in names)
    if match["negate"]:
        holds = not holds
    return holds, match["indent"] + line[match.end() :]


def select_lines(body: str, values: Mapping[str, str]) -> str:
    """The lines whose condition holds, without the markers."""
    kept = []
    for line in body.splitlines():
        visible, text = line_condition(line, values)
        if visible:
            kept.append(text)
    return "\n".join(kept)


def clean_fill_value(value: object) -> str:
    """Values are single-line plain text: no control characters, collapsed whitespace."""
    text = "".join(
        " " if unicodedata.category(char).startswith(("C", "Z")) else char for char in str(value)
    )
    return " ".join(text.split())[:MAX_VALUE_LENGTH]


def clean_list_value(value: object) -> str:
    """List values keep one row per line; each row is cleaned like any value."""
    rows = (clean_fill_value(row) for row in str(value).splitlines())
    return "\n".join(row for row in rows if row)


def split_row(row: str, columns: int) -> list[str]:
    """``6201 Activități de realizare a soft-ului`` -> ["6201", "Activități de ..."]."""
    if columns == 1:
        return [row]
    if "|" in row:  # "Act constitutiv | 1/01.10.2026 | 5"
        parts = [part.strip() for part in row.split("|", columns - 1)]
    else:
        parts = re.split(r"\s*[-–;]\s+|\s+", row, maxsplit=columns - 1)
    return parts + [""] * (columns - len(parts))


def choose(mapping: Mapping[str, str], value: str) -> str | None:
    """Map a choice ("electronic", "poștă") to its PDF button state; states pass through."""
    if value.startswith("/"):
        return value
    wanted = _fold(value)
    if not wanted:
        return None
    for key, state in mapping.items():
        if _fold(key) == wanted:
            return state
    for key, state in mapping.items():
        if _fold(key).startswith(wanted):
            return state
    # "prin opțiune" for "4.3 prin opțiune (art. 316 ...)": only when a single option matches
    contained = [state for key, state in mapping.items() if wanted in _fold(key)]
    return contained[0] if len(contained) == 1 else None


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn").lower().strip()


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

    if "[[" in body:
        body = select_lines(body, values)
    return PLACEHOLDER_RE.sub(replace, body)


def preview_lines(body: str, values: Mapping[str, str], blank: str = BLANK) -> list[dict]:
    """Structured rendering for live previews: one entry per line with its kind (see
    :func:`classify_line`) and segments marking which text comes from which field."""
    lines: list[dict] = []
    for line in select_lines(body, values).splitlines():
        kind, text = classify_line(line)
        segments: list[dict] = []
        position = 0
        for match in PLACEHOLDER_RE.finditer(text):
            if match.start() > position:
                segments.append({"text": text[position : match.start()]})
            value = apply(match.group()[2:-2], values)
            segments.append({"text": value or blank, "field": match["name"], "filled": bool(value)})
            position = match.end()
        if position < len(text):
            segments.append({"text": text[position:]})
        lines.append({"kind": kind, "segments": segments})
    return lines
