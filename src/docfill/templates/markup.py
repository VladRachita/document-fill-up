"""The small layout language of text standard documents, shared by the PDF renderer and the
wizard's live preview so both always agree."""

from __future__ import annotations

from typing import Literal

LineKind = Literal["h1", "h2", "hr", "blank", "text"]


def classify_line(line: str) -> tuple[LineKind, str]:
    """``# Title`` -> h1, ``## Section`` -> h2, ``---`` -> rule, empty -> blank, else text.

    Text lines keep their leading indentation; headings are returned without the marker.
    """
    stripped = line.strip()
    if not stripped:
        return "blank", ""
    if stripped.startswith("## "):
        return "h2", stripped[3:].strip()
    if stripped.startswith("# "):
        return "h1", stripped[2:].strip()
    if stripped == "---":
        return "hr", ""
    return "text", line.rstrip()
