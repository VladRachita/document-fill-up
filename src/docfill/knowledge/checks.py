"""Evaluate the legal rules of a procedure on the values being filled in."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

from docfill.knowledge.specs import Check, RuleSpec
from docfill.ro import check_cnp, parse_date


def fold(text: str) -> str:
    """Accent-free, lowercase; ``ş``/``ţ`` (cedilla) and ``ș``/``ț`` (comma) are the same."""
    decomposed = unicodedata.normalize("NFD", text)
    return "".join(c for c in decomposed if unicodedata.category(c) != "Mn").casefold()


def words(text: str) -> str:
    """Comparable words: accents, case and dots ignored (``S.R.L.`` -> ``srl``)."""
    return " ".join(re.sub(r"[^\w]+", " ", fold(text).replace(".", "")).split())


def parse_amount(text: str) -> float | None:
    """``90.000 lei``, ``90 000``, ``1.500,50 RON``, ``200`` -> a number (Romanian notation:
    ``.`` groups thousands, ``,`` marks decimals)."""
    match = re.search(r"\d[\d .,]*", text or "")
    if not match:
        return None
    number = match.group().strip().replace(" ", "").rstrip(".,")
    if "." in number and "," in number:
        decimal = "," if number.rfind(",") > number.rfind(".") else "."
        thousands = "." if decimal == "," else ","
        number = number.replace(thousands, "").replace(decimal, ".")
    elif re.fullmatch(r"\d{1,3}([.,]\d{3})+", number):
        number = re.sub(r"[.,]", "", number)
    else:
        number = number.replace(",", ".")
    try:
        return float(number)
    except ValueError:
        return None


def lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def age_on(birth: date, today: date) -> int:
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def _birth_date(value: str) -> date | None:
    digits = re.sub(r"\s", "", value)
    if re.fullmatch(r"\d{13}", digits):
        info = check_cnp(digits)
        return info.birth_date if info.valid else None
    return parse_date(value)


def _number(value: float) -> str:
    text = f"{value:,.2f}".rstrip("0").rstrip(".")
    return text.replace(",", " ").replace(".", ",").replace(" ", ".")


@dataclass
class Outcome:
    status: str  # passed | failed | skipped
    detail: str = ""


def run_check(check: Check, values: Mapping[str, str], today: date | None = None) -> Outcome:
    """Apply one declarative check. Never raises on odd values: they fail with a detail."""
    today = today or date.today()

    def get(name: str | None) -> str:
        return (values.get(name or "") or "").strip()

    if check.type == "required":
        missing = [name for name in (check.fields or [check.field or ""]) if not get(name)]
        if missing:
            return Outcome("failed", "missing: " + ", ".join(missing))
        return Outcome("passed")

    if check.type == "required_any":
        names = check.fields or [check.field or ""]
        if any(get(name) for name in names):
            return Outcome("passed")
        return Outcome("failed", "none of: " + ", ".join(names))

    value = get(check.field)
    if not value:
        return Outcome("skipped", f"{check.field} is empty")
    limit = check.value if check.value is not None else 0.0

    if check.type in ("min_amount", "max_amount"):
        amount = parse_amount(value)
        if amount is None:
            return Outcome("failed", f"not an amount: {value}")
        ok = amount >= limit if check.type == "min_amount" else amount <= limit
        return Outcome("passed" if ok else "failed", f"{_number(amount)} (limit {_number(limit)})")
    if check.type in ("min_count", "max_count"):
        count = len(lines(value))
        ok = count >= limit if check.type == "min_count" else count <= limit
        return Outcome("passed" if ok else "failed", f"{count} (limit {_number(limit)})")
    if check.type == "contains_any":
        text = f" {words(value)} "
        ok = any(f" {words(phrase)} " in text for phrase in check.any_of if words(phrase))
        return Outcome("passed" if ok else "failed", value)
    if check.type == "contains_field":
        other = get(check.other)
        if not other:
            return Outcome("skipped", f"{check.other} is empty")
        ok = f" {words(other)} " in f" {words(value)} "
        return Outcome("passed" if ok else "failed", f"{value} / {other}")
    if check.type == "min_age":
        birth = _birth_date(value)
        if birth is None:
            return Outcome("skipped", "no valid date of birth")
        age = age_on(birth, today)
        return Outcome("passed" if age >= limit else "failed", f"{age} years")
    if check.type == "pattern":
        ok = re.fullmatch(check.pattern or "", value) is not None
        return Outcome("passed" if ok else "failed", value)
    if check.type == "lines_pattern":
        bad = [line for line in lines(value) if not re.match(check.pattern or "", line)]
        return Outcome("failed", "; ".join(b[:40] for b in bad[:3])) if bad else Outcome("passed")
    if check.type == "one_of":
        ok = words(value) in {words(option) for option in check.any_of}
        return Outcome("passed" if ok else "failed", value)
    return Outcome("skipped", f"unknown check {check.type}")  # pragma: no cover


@dataclass
class RuleResult:
    """One rule evaluated on the current values, ready for the wizard."""

    rule: str
    title: str
    severity: str
    status: str  # passed | failed | skipped
    message: str
    detail: str
    field: str | None
    legal_basis: list[dict[str, Any]] = field(default_factory=list)
    knowledge_status: str = "draft"  # draft | verified: has a person checked the rule?
    version: int = 1
    law_text: str | None = None  # the article's text, when that law was fed to docfill

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_ORDER = {"failed": 0, "passed": 1, "skipped": 2}
_SEVERITY = {"error": 0, "warning": 1, "info": 2}


def evaluate(
    rules: Iterable[tuple[RuleSpec, str, int]],
    values: Mapping[str, str],
    today: date | None = None,
) -> list[RuleResult]:
    """``rules`` yields (spec, knowledge status, version). Failed rules come first, the most
    severe first."""
    results = []
    for spec, knowledge_status, version in rules:
        outcome = run_check(spec.check, values, today)
        targets = [name for name in spec.check.targets() if name]
        results.append(
            RuleResult(
                rule=spec.key,
                title=spec.title,
                severity=spec.severity,
                status=outcome.status,
                message=spec.message,
                detail=outcome.detail,
                field=targets[0] if targets else None,
                legal_basis=[ref.model_dump(exclude_none=True) for ref in spec.legal_basis],
                knowledge_status=knowledge_status,
                version=version,
            )
        )
    results.sort(key=lambda r: (_ORDER.get(r.status, 3), _SEVERITY.get(r.severity, 3), r.rule))
    return results
