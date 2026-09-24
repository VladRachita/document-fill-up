"""Command line interface: ``docfill --help``."""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import click
import typer
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from docfill.app import build_app
from docfill.config import Settings, get_settings
from docfill.errors import DocFillError, MissingFieldsError
from docfill.export.pdf_form import blank_form, inspect_form, read_form_values
from docfill.extraction import field_labels
from docfill.learning import Review, ReviewedDocument, reviewed_fields_from_rows
from docfill.models import ExtractionResult
from docfill.templates import (
    TemplateRepository,
    bundled_specs_dir,
    load_directory,
    load_spec,
    make_session_factory,
)
from docfill.templates.models import StandardDocument
from docfill.templates.placeholders import apply, render_body
from docfill.validation import validate_values
from docfill.wizard import (
    Candidate,
    FieldRow,
    compare_forms,
    field_rows,
    safe_filename,
    save_output,
    suggest_field,
    suggest_filename,
)

app = typer.Typer(
    help="Scan documents, extract personal data and fill standard documents as PDF.",
    no_args_is_help=True,
)
templates_app = typer.Typer(help="Manage the standard documents database.", no_args_is_help=True)
app.add_typer(templates_app, name="templates")

console = Console()
err_console = Console(stderr=True)


def _settings(ctx: typer.Context) -> Settings:
    return ctx.obj if isinstance(ctx.obj, Settings) else get_settings()


@contextmanager
def _repository(ctx: typer.Context) -> Iterator[TemplateRepository]:
    session = make_session_factory(_settings(ctx).database_url)()
    try:
        yield TemplateRepository(session)
    finally:
        session.close()


def _fail(message: str) -> None:
    err_console.print(f"[bold red]Error:[/] {message}")
    raise typer.Exit(code=1)


def _parse_overrides(pairs: list[str]) -> dict[str, str]:
    overrides: dict[str, str] = {}
    for pair in pairs:
        name, sep, value = pair.partition("=")
        if not sep or not name.strip():
            _fail(f"--set expects FIELD=VALUE, got {pair!r}")
        overrides[name.strip()] = value.strip()
    return overrides


def _extraction_table(result: ExtractionResult, title: str) -> Table:
    labels = field_labels()
    table = Table(title=title)
    for column in ("Field", "Value", "Confidence", "Source"):
        table.add_column(column)
    for name, field in sorted(result.fields.items()):
        table.add_row(
            f"{labels.get(name, name)} [dim]({name})[/]",
            field.value,
            f"{field.confidence:.2f}",
            field.source,
        )
    return table


@app.callback()
def main(
    ctx: typer.Context,
    database_url: Annotated[
        str | None,
        typer.Option("--db", help="Database URL (default: DOCFILL_DATABASE_URL or sqlite)."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v", help="Verbose logging.")] = False,
) -> None:
    logging.basicConfig(level=logging.INFO if verbose else logging.WARNING)
    settings = get_settings()
    if database_url:
        settings = settings.model_copy(update={"database_url": database_url})
    ctx.obj = settings


# ----------------------------------------------------------------------------- templates


@app.command("init-db")
def init_db(ctx: typer.Context) -> None:
    """Create the database tables."""
    with _repository(ctx):
        pass
    console.print(f"Database ready: {_settings(ctx).database_url}")


@templates_app.command("list")
def templates_list(ctx: typer.Context) -> None:
    """List the stored standard documents."""
    with _repository(ctx) as repo:
        documents = repo.list()
    if not documents:
        console.print("No standard documents yet. Try: docfill templates seed")
        return
    table = Table(title="Standard documents")
    table.add_column("Name", no_wrap=True)
    for column in ("Title", "Kind", "Version", "Fields"):
        table.add_column(column)
    for doc in documents:
        table.add_row(doc.name, doc.title, doc.kind, str(doc.version), ", ".join(doc.field_names()))
    console.print(table)


@templates_app.command("show")
def templates_show(ctx: typer.Context, name: str) -> None:
    """Show a standard document and the fields it needs."""
    try:
        with _repository(ctx) as repo:
            doc = repo.get(name)
    except DocFillError as exc:
        _fail(str(exc))
    console.print(f"[bold]{doc.title}[/] ({doc.name} v{doc.version}, {doc.kind})")
    if doc.description:
        console.print(doc.description)
    console.print(f"Required fields: {', '.join(doc.required_fields()) or '-'}")
    optional = [n for n in doc.field_names() if n not in doc.required_fields()]
    console.print(f"Optional fields: {', '.join(optional) or '-'}")
    console.print(f"Checksum: {doc.checksum}")
    if doc.kind == "text":
        console.rule()
        console.print(doc.body, markup=False, highlight=False)
    else:
        for pdf_field, expression in doc.field_map.items():
            console.print(f"  {pdf_field} <- {expression}")


@templates_app.command("add")
def templates_add(
    ctx: typer.Context,
    spec_files: Annotated[list[Path], typer.Argument(help="Standard document YAML file(s).")],
) -> None:
    """Register or update standard documents from YAML files."""
    try:
        specs = [load_spec(path) for path in spec_files]
        with _repository(ctx) as repo:
            for spec in specs:
                doc, changed = repo.save(spec)
                state = f"saved as v{doc.version}" if changed else "unchanged"
                console.print(f"{doc.name}: {state}")
    except DocFillError as exc:
        _fail(str(exc))


@templates_app.command("seed")
def templates_seed(
    ctx: typer.Context,
    directory: Annotated[
        Path | None, typer.Argument(help="Directory of YAML files (default: bundled).")
    ] = None,
) -> None:
    """Load every standard document of a directory (the bundled examples by default)."""
    try:
        specs = load_directory(directory or bundled_specs_dir())
        with _repository(ctx) as repo:
            for spec in specs:
                doc, changed = repo.save(spec)
                state = f"saved as v{doc.version}" if changed else "unchanged"
                console.print(f"{doc.name}: {state}")
    except DocFillError as exc:
        _fail(str(exc))


@templates_app.command("remove")
def templates_remove(ctx: typer.Context, name: str) -> None:
    """Delete a standard document."""
    try:
        with _repository(ctx) as repo:
            repo.delete(name)
    except DocFillError as exc:
        _fail(str(exc))
    console.print(f"Removed {name}")


# ----------------------------------------------------------------------------- processing


@app.command()
def extract(
    ctx: typer.Context,
    files: Annotated[list[Path], typer.Argument(help="PDF, DOCX, PNG or JPEG files.")],
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
    show_text: Annotated[bool, typer.Option(help="Also print the sanitized text.")] = False,
    ner: Annotated[bool, typer.Option(help="Use the ML named-entity model.")] = True,
) -> None:
    """Scan documents and show the extracted fields."""
    settings = _settings(ctx)
    docfill = build_app(settings, use_ner=ner).docfill
    try:
        analyses = [docfill.analyze_file(path) for path in files]
    except DocFillError as exc:
        _fail(str(exc))
    combined = docfill.combine(analyses)

    if as_json:
        payload = {
            "documents": [
                {
                    "source": a.raw.source,
                    "type": a.raw.doc_type.value,
                    "ocr": a.raw.used_ocr,
                    "warnings": a.raw.warnings,
                    "redactions": a.sanitized.redactions,
                    **({"text": a.sanitized.text} if show_text else {}),
                }
                for a in analyses
            ],
            "fields": {name: f.model_dump() for name, f in combined.fields.items()},
        }
        typer.echo(json.dumps(payload, indent=2, ensure_ascii=False))
        return

    for analysis in analyses:
        raw, sanitized = analysis.raw, analysis.sanitized
        info = f"{raw.doc_type.value}, {len(raw.pages)} page(s)"
        if raw.used_ocr:
            info += ", OCR"
        console.print(f"[bold]{raw.source}[/] ({info})")
        for warning in raw.warnings:
            console.print(f"  [yellow]warning:[/] {warning}")
        if sanitized.redactions:
            console.print(f"  redacted: {sanitized.redactions}")
        if show_text:
            console.rule("sanitized text")
            console.print(sanitized.text, markup=False, highlight=False)
            console.rule()
    if docfill.extractor.ner and not docfill.extractor.ner_available:
        console.print(f"[yellow]NER disabled:[/] {docfill.extractor.ner.error}")
    console.print(_extraction_table(combined, "Extracted fields"))


@app.command()
def fill(
    ctx: typer.Context,
    template: Annotated[str, typer.Option("--template", "-t", help="Standard document name.")],
    files: Annotated[
        list[Path] | None, typer.Argument(help="Source documents to extract data from.")
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output PDF path (.pdf is added if missing)."),
    ] = None,
    set_values: Annotated[
        list[str] | None,
        typer.Option("--set", "-s", help="Provide or override a value: FIELD=VALUE."),
    ] = None,
    allow_missing: Annotated[
        bool, typer.Option(help="Leave unknown required fields blank instead of failing.")
    ] = False,
    ner: Annotated[bool, typer.Option(help="Use the ML named-entity model.")] = True,
) -> None:
    """Fill a standard document with data extracted from FILES and export it as PDF."""
    files = files or []
    overrides = _parse_overrides(set_values or [])
    if not files and not overrides:
        _fail("give at least one source document or --set value")
    settings = _settings(ctx)
    docfill = build_app(settings, use_ner=ner).docfill
    try:
        with _repository(ctx) as repo:
            standard = repo.get(template)
        analyses = [docfill.analyze_file(path) for path in files]
        extraction = docfill.combine(analyses) if analyses else None
        result = docfill.fill(standard, extraction, overrides, allow_missing)
    except MissingFieldsError as exc:
        _fail(f"{exc}\nProvide them with --set FIELD=VALUE or use --allow-missing.")
    except DocFillError as exc:
        _fail(str(exc))

    if output is None:
        stem = files[0].stem if files else "manual"
        output = Path(f"{template}-{stem}.pdf")
    elif output.suffix.lower() != ".pdf":
        # The output is always a PDF: never write it under another extension.
        output = output.with_name(output.name + ".pdf")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(result.pdf)

    for analysis in analyses:
        for warning in analysis.raw.warnings:
            console.print(f"[yellow]warning:[/] {warning}")
    table = Table(title=f"{standard.title} (v{result.template_version})")
    for column in ("Field", "Value", "Source"):
        table.add_column(column)
    for name in standard.field_names():
        value = result.values.get(name)
        table.add_row(name, value or "[red]- blank -[/]", result.sources.get(name, ""))
    console.print(table)
    console.print(f"[green]Written[/] {output}")


_SOURCE_NAMES = {
    "label": "label rule",
    "pattern": "pattern",
    "ner": "ML model",
    "derived": "derived",
    "system": "system",
}


def _describe(candidate: Candidate) -> str:
    return f"{_SOURCE_NAMES.get(candidate.source, candidate.source)} {candidate.confidence:.0%}"


def _review_row(row: FieldRow) -> None:
    """Show what was found for one field and let the user keep, correct or clear it."""
    tag = "[red]required[/]" if row.required else "[dim]optional[/]"
    console.print(f"\n[bold]{escape(row.label)}[/] [dim]({row.name})[/] {tag}")
    if row.found:
        detail = _describe(row.found)
        if row.found.document:
            detail += f" · from {row.found.document}"
        console.print(f"  found: [dim]{escape(detail)}[/]")
        if row.found.evidence:
            console.print(f"  where: [dim]{escape(row.found.evidence)}[/]")
    elif row.required:
        console.print("  [red]not found in the documents[/]")
    for number, alternative in enumerate(row.alternatives, 1):
        console.print(
            f"  #{number} {escape(alternative.value)} [dim]({escape(_describe(alternative))})[/]"
        )
    answer = typer.prompt("  value", default=row.value, show_default=bool(row.value)).strip()
    if answer == "-":
        row.value = ""
    elif re.fullmatch(r"#\d+", answer) and 1 <= int(answer[1:]) <= len(row.alternatives):
        row.value = row.alternatives[int(answer[1:]) - 1].value
    else:
        row.value = answer


def _preview(standard: StandardDocument, values: dict[str, str]) -> Panel:
    if standard.kind == "text":
        body = Text(render_body(standard.body or "", values))
    else:
        body = Text(
            "\n".join(
                f"{pdf_field}: {apply(expression, values) or '(blank)'}"
                for pdf_field, expression in standard.field_map.items()
            )
        )
    return Panel(body, title=f"{standard.title} (v{standard.version})", expand=False)


def _fields_table(rows: list[FieldRow], issues: dict[str, list[str]]) -> Table:
    table = Table(title="Fields", show_lines=False)
    for column in ("#", "Field", "Value", "Found by", "Problems"):
        table.add_column(column, overflow="fold")
    for number, row in enumerate(rows, 1):
        value = (
            row.value.replace("\n", " / ")
            if row.value
            else ("[red]missing[/]" if row.required else "[dim]-[/]")
        )
        found = (
            _describe(row.found)
            if row.found and row.value == row.found.value
            else ("you" if row.value else "")
        )
        table.add_row(
            str(number),
            escape(row.label),
            escape(value) if row.value else value,
            found,
            escape("; ".join(issues.get(row.name, []))),
        )
    return table


@app.command()
def wizard(
    ctx: typer.Context,
    files: Annotated[
        list[Path] | None, typer.Argument(help="Source documents (asked for when omitted).")
    ] = None,
    templates: Annotated[
        list[str] | None,
        typer.Option("--template", "-t", help="Reference document name (repeat for several)."),
    ] = None,
    output_dir: Annotated[
        Path | None, typer.Option(help="Folder for the PDFs (default: DOCFILL_OUTPUT_DIR).")
    ] = None,
    ner: Annotated[bool, typer.Option(help="Use the ML named-entity model.")] = True,
) -> None:
    """Step by step: pick the reference documents, scan the sources, check and correct every
    value, then save the filled documents under a new file name. Each review teaches docfill."""
    settings = _settings(ctx)
    context = build_app(settings, use_ner=ner)
    docfill = context.docfill

    # Step 1 - reference documents and sources
    console.rule("[bold]Step 1/4 · Reference documents")
    try:
        with _repository(ctx) as repo:
            standards = repo.list()
            chosen = [repo.get(name) for name in templates or []]
    except DocFillError as exc:
        _fail(str(exc))
    if not standards:
        _fail("No standard documents yet. Run: docfill templates seed")
    if not chosen:
        for number, doc in enumerate(standards, 1):
            console.print(f"  {number}. {escape(doc.title)} [dim]({doc.name})[/]")
        answer = typer.prompt("Reference document number(s), e.g. 1 or 1,2", default="1")
        numbers = [int(n) for n in re.findall(r"\d+", answer) if 1 <= int(n) <= len(standards)]
        chosen = [standards[n - 1] for n in dict.fromkeys(numbers)] or [standards[0]]
    for doc in chosen:
        console.print(f"Using [bold]{escape(doc.title)}[/] v{doc.version}")

    paths = list(files or [])
    if not paths:
        while True:
            answer = typer.prompt(
                "Source document (empty when done)", default="", show_default=False
            ).strip()
            if not answer:
                break
            path = Path(answer).expanduser()
            if path.is_file():
                paths.append(path)
            else:
                console.print(f"[red]Not found:[/] {escape(answer)}")

    # Step 2 - scan, clean, detect the document type
    console.rule("[bold]Step 2/4 · Scan & clean")
    analyses = []
    for path in paths:
        try:
            analysis = docfill.analyze_file(path)
        except DocFillError as exc:
            console.print(f"[red]{escape(str(exc))}[/] (skipped)")
            continue
        raw, sanitized, kind = analysis.raw, analysis.sanitized, analysis.doc_type
        info = f"{raw.doc_type.value}, {len(raw.pages)} page(s)" + (", OCR" if raw.used_ocr else "")
        console.print(f"[bold]{escape(raw.source)}[/] ({info})")
        console.print(
            f"  detected: [bold]{escape(kind.label)}[/] ({kind.confidence:.0%}, {kind.method})"
        )
        for warning in raw.warnings:
            console.print(f"  [yellow]warning:[/] {escape(warning)}")
        if sanitized.redactions:
            console.print(f"  redacted: {sanitized.redactions}")
        if typer.confirm(f"  Show the text read from {raw.source}?", default=False):
            console.print(Panel(Text(sanitized.text or "(no text)"), expand=False))
            if typer.confirm("  Correct the text in an editor?", default=False):
                edited = click.edit(sanitized.text)
                if edited is not None:
                    sanitized.text = edited
                    analysis.extraction = docfill.extract_texts(
                        [(raw.source, edited, kind.doc_type)]
                    )
        analyses.append(analysis)
    if docfill.extractor.ner and not docfill.extractor.ner_available:
        console.print(f"[yellow]ML model disabled:[/] {escape(docfill.extractor.ner.error or '')}")
    extraction = docfill.combine(analyses) if analyses else None
    rows = field_rows(chosen, extraction, settings.min_confidence, context.learning.remembered())
    shown = [row.as_dict() for row in rows]  # what was proposed, for learning

    # Step 3 - review: missing required fields first, then any field the user picks
    console.rule("[bold]Step 3/4 · Review fields")
    console.print(
        "[dim]Enter keeps a value · type to correct it · '-' clears it · #N picks candidate N[/]"
    )
    for row in rows:
        if row.required and not row.value:
            _review_row(row)
    while True:
        values, _ = docfill.collect_values(None, {row.name: row.value for row in rows})
        console.print(_fields_table(rows, validate_values(values)))
        answer = typer.prompt(
            "Field numbers to change (e.g. 3,7), Enter to continue",
            default="",
            show_default=False,
        )
        numbers = [int(n) for n in re.findall(r"\d+", answer) if 1 <= int(n) <= len(rows)]
        for number in numbers:
            _review_row(rows[number - 1])
        if numbers:
            continue
        values, _ = docfill.collect_values(None, {row.name: row.value for row in rows})
        for doc in chosen:
            console.print(_preview(doc, values))
        missing = [row.label for row in rows if row.required and row.name not in values]
        if missing:
            console.print(f"[red]Still missing:[/] {escape(', '.join(missing))}")
        if typer.confirm("Is everything correct?", default=not missing):
            break

    # Step 4 - save under a new name and learn from the review
    console.rule("[bold]Step 4/4 · Save")
    if missing and not typer.confirm("Create the PDFs with those fields left blank?"):
        console.print("Nothing saved.")
        raise typer.Exit(code=1)
    base = typer.prompt("New file name", default=suggest_filename(chosen, values))
    base = safe_filename(base).removesuffix(".pdf")
    for doc in chosen:
        try:
            result = docfill.fill(doc, None, values, allow_missing=bool(missing))
        except DocFillError as exc:
            _fail(str(exc))
        name = base if len(chosen) == 1 else f"{base}_{doc.name}"
        path = save_output(output_dir or settings.output_dir, name, result.pdf)
        console.print(f"[green]Saved[/] {path}")
    learned = context.learning.record_review(
        Review(
            templates=[doc.name for doc in chosen],
            fields=reviewed_fields_from_rows(shown, values),
            documents=[
                ReviewedDocument(
                    a.raw.source, a.sanitized.text, a.doc_type.doc_type, a.doc_type.doc_type
                )
                for a in analyses
            ],
            remember={name for doc in chosen for name in doc.remember},
        )
    )
    summary = ", ".join(f"{count} {kind}" for kind, count in learned.outcomes.items())
    console.print(
        f"[dim]Learned from this review: {summary or 'nothing new'}"
        + (f"; new labels: {', '.join(learned.new_labels)}" if learned.new_labels else "")
        + "[/]"
    )


# ----------------------------------------------------------------------------- forms


forms_app = typer.Typer(help="Tools for PDF forms (AcroForm).", no_args_is_help=True)
app.add_typer(forms_app, name="forms")


@forms_app.command("inspect")
def forms_inspect(
    pdf: Annotated[Path, typer.Argument(help="A PDF form.")],
    suggest: Annotated[bool, typer.Option(help="Print a suggested field_map (YAML).")] = False,
) -> None:
    """List the fields of a PDF form with the label printed next to each, and optionally
    suggest which docfill field each one is."""
    fields = inspect_form(pdf.read_bytes())
    table = Table(title=f"{pdf.name}: {len(fields)} fields")
    for column in ("Page", "Field", "Kind", "Printed label", "Filled", "Suggested"):
        table.add_column(column, overflow="fold")
    suggestions = {}
    for f in fields:
        suggestion = suggest_field(f.label) if f.kind == "text" else None
        if suggestion:
            suggestions.setdefault(f.name, suggestion)
        table.add_row(
            str(f.page),
            escape(f.name),
            f.kind,
            escape(f.label[-50:]),
            "yes" if f.value else "",
            suggestion or "",
        )
    console.print(table)
    if suggest:
        typer.echo("field_map:")
        for name, field_name in suggestions.items():
            typer.echo(f"  {json.dumps(name, ensure_ascii=False)}: {field_name}")


@forms_app.command("blank")
def forms_blank(
    source: Annotated[Path, typer.Argument(help="A filled PDF form.")],
    output: Annotated[Path, typer.Argument(help="Where to write the blank form.")],
) -> None:
    """Remove every filled value, drawn value and the metadata from a PDF form, so a filled
    example can be registered as a blank standard document."""
    data = blank_form(source.read_bytes())
    output.write_bytes(data)
    left = read_form_values(data)
    console.print(f"[green]Written[/] {output} ({len(left)} values left)")


# ----------------------------------------------------------------------------- learning


learn_app = typer.Typer(help="What docfill learned from reviewed documents.", no_args_is_help=True)
app.add_typer(learn_app, name="learn")


@learn_app.command("stats")
def learn_stats(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON.")] = False,
) -> None:
    """Accuracy per field and method, learned labels, examples and remembered fields."""
    stats = build_app(_settings(ctx), use_ner=False).learning.stats()
    if as_json:
        typer.echo(json.dumps(stats, indent=2, ensure_ascii=False))
        return

    def pct(value: float | None) -> str:
        return "-" if value is None else f"{value:.0%}"

    console.print(
        f"Reviewed documents: [bold]{stats['reviews']}[/] · accuracy of proposed "
        f"values: {pct(stats['accuracy_all'])} overall, "
        f"{pct(stats['accuracy_last_10_reviews'])} in the last 10 reviews"
    )
    table = Table(title="Per field and method")
    for column in ("Field", "Found by", "Accepted", "Corrected", "Cleared", "Added", "Accuracy"):
        table.add_column(column)
    for row in stats["fields"]:
        table.add_row(
            row["field"],
            row["source"],
            str(row.get("accepted", 0)),
            str(row.get("corrected", 0)),
            str(row.get("cleared", 0)),
            str(row.get("added", 0)),
            pct(row["accuracy"]),
        )
    console.print(table)
    console.print(
        f"Learned labels: {len(stats['learned_labels'])} · spelling fixes: "
        f"{stats['spelling_fixes']} · document examples: {stats['examples'] or 0} · "
        f"remembered fields: {stats['remembered_fields']}"
    )
    for label in stats["learned_labels"]:
        console.print(
            f"  [dim]{escape(label['label'])} → {label['field']} "
            f"({label['doc_type'] or 'any document'}, seen {label['hits']}x)[/]"
        )


@learn_app.command("reset")
def learn_reset(
    ctx: typer.Context,
    yes: Annotated[bool, typer.Option("--yes", help="Do not ask for confirmation.")] = False,
) -> None:
    """Forget everything learned (reviews, labels, examples, remembered values)."""
    if not yes and not typer.confirm("Forget everything docfill learned?"):
        raise typer.Exit(code=1)
    build_app(_settings(ctx), use_ner=False).learning.reset()
    console.print("Learning data cleared.")


@learn_app.command("import-filled")
def learn_import_filled(
    ctx: typer.Context,
    files: Annotated[list[Path], typer.Argument(help="Already filled PDF forms.")],
) -> None:
    """Learn from documents you already filled: remembers the filer's own details (fields
    marked ``remember``) and adds them as examples for document type detection."""
    context = build_app(_settings(ctx), use_ner=False)
    for path in files:
        try:
            analysis = context.docfill.analyze_file(path)
        except DocFillError as exc:
            _fail(str(exc))
        kind = analysis.doc_type
        template = next((t for t in context.templates() if t.doc_type == kind.doc_type), None)
        if template is None or kind.method != "form fields":
            console.print(
                f"[yellow]{escape(path.name)}: not a filled form of a registered "
                "standard document, skipped[/]"
            )
            continue
        values = analysis.extraction.values(0.0)
        remembered = {name: values[name] for name in template.remember if values.get(name)}
        context.learning.remember_values(remembered)
        context.learning.record_review(
            Review(
                templates=[template.name],
                fields=[],
                documents=[ReviewedDocument(path.name, analysis.sanitized.text, kind.doc_type)],
            )
        )
        console.print(
            f"{escape(path.name)}: {kind.label}; remembered {len(remembered)} fields "
            f"({', '.join(remembered)})"
        )


# ----------------------------------------------------------------------------- evaluation


@app.command()
def evaluate(
    ctx: typer.Context,
    template: Annotated[str, typer.Option("--template", "-t", help="Standard document name.")],
    expected: Annotated[Path, typer.Option(help="The same document filled correctly.")],
    sources: Annotated[
        list[Path] | None, typer.Argument(help="Source documents (default: the expected one).")
    ] = None,
    ner: Annotated[bool, typer.Option(help="Use the ML named-entity model.")] = True,
) -> None:
    """Measure errors: fill TEMPLATE from SOURCES and compare every field with a correctly
    filled copy. Run it on the same cases over time to see the error rate go down."""
    context = build_app(_settings(ctx), use_ner=ner)
    try:
        with _repository(ctx) as repo:
            standard = repo.get(template)
        analyses = [context.docfill.analyze_file(path) for path in sources or [expected]]
        result = context.docfill.fill(
            standard, context.docfill.combine(analyses), allow_missing=True
        )
    except DocFillError as exc:
        _fail(str(exc))
    report = compare_forms(
        read_form_values(expected.read_bytes()),
        read_form_values(result.pdf),
        standard.pdf_field_names(),
    )
    table = Table(
        title=f"{standard.title}: {report['matched']}/{report['expected']} values "
        f"match ({report['accuracy']:.0%})"
    )
    for column in ("PDF field", "Problem"):
        table.add_column(column)
    for field_name, problem in report["differences"]:
        table.add_row(escape(field_name), problem)
    console.print(table)


@app.command()
def serve(
    ctx: typer.Context,
    host: Annotated[str, typer.Option(help="Bind address.")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port.")] = 8000,
) -> None:
    """Run the REST API (see /docs for the OpenAPI UI)."""
    import uvicorn

    # The API builds its own settings from the environment; forward a --db override.
    os.environ["DOCFILL_DATABASE_URL"] = _settings(ctx).database_url
    get_settings.cache_clear()
    uvicorn.run("docfill.api:create_app", host=host, port=port, factory=True)


if __name__ == "__main__":
    app()
