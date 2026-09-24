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

from docfill.config import Settings, get_settings
from docfill.errors import DocFillError, MissingFieldsError
from docfill.extraction import FieldExtractor, field_labels
from docfill.models import ExtractionResult
from docfill.pipeline import DocFill
from docfill.templates import (
    TemplateRepository,
    bundled_specs_dir,
    load_directory,
    load_spec,
    make_session_factory,
)
from docfill.templates.models import StandardDocument
from docfill.templates.placeholders import apply, render_body
from docfill.wizard import Candidate, FieldRow, field_rows, save_output, suggest_filename

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
    docfill = DocFill(settings, FieldExtractor(settings, use_ner=ner))
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
    docfill = DocFill(settings, FieldExtractor(settings, use_ner=ner))
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


@app.command()
def wizard(
    ctx: typer.Context,
    files: Annotated[
        list[Path] | None, typer.Argument(help="Source documents (asked for when omitted).")
    ] = None,
    template: Annotated[
        str | None, typer.Option("--template", "-t", help="Reference document name.")
    ] = None,
    output_dir: Annotated[
        Path | None, typer.Option(help="Folder for the PDF (default: DOCFILL_OUTPUT_DIR).")
    ] = None,
    ner: Annotated[bool, typer.Option(help="Use the ML named-entity model.")] = True,
) -> None:
    """Step by step: pick the reference document, scan the sources, check and correct every
    value, then save the filled document under a new file name."""
    settings = _settings(ctx)
    docfill = DocFill(settings, FieldExtractor(settings, use_ner=ner))

    # Step 1 - reference document and sources
    console.rule("[bold]Step 1/4 · Reference document")
    try:
        with _repository(ctx) as repo:
            standards = repo.list()
            standard = repo.get(template) if template else None
    except DocFillError as exc:
        _fail(str(exc))
    if not standards:
        _fail("No standard documents yet. Run: docfill templates seed")
    if standard is None:
        for number, doc in enumerate(standards, 1):
            console.print(f"  {number}. {escape(doc.title)} [dim]({doc.name})[/]")
        choice = typer.prompt(
            "Reference document", default=1, type=click.IntRange(1, len(standards))
        )
        standard = standards[choice - 1]
    console.print(f"Using [bold]{escape(standard.title)}[/] v{standard.version}")
    console.print(f"[dim]Needs: {', '.join(standard.field_names())}[/]")

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

    # Step 2 - scan and clean
    console.rule("[bold]Step 2/4 · Scan & clean")
    analyses = []
    for path in paths:
        try:
            analysis = docfill.analyze_file(path)
        except DocFillError as exc:
            console.print(f"[red]{escape(str(exc))}[/] (skipped)")
            continue
        raw, sanitized = analysis.raw, analysis.sanitized
        info = f"{raw.doc_type.value}, {len(raw.pages)} page(s)" + (", OCR" if raw.used_ocr else "")
        console.print(f"[bold]{escape(raw.source)}[/] ({info})")
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
                    analysis.extraction = docfill.extract_texts([(raw.source, edited)])
        analyses.append(analysis)
    if docfill.extractor.ner and not docfill.extractor.ner_available:
        console.print(f"[yellow]ML model disabled:[/] {escape(docfill.extractor.ner.error or '')}")
    extraction = docfill.combine(analyses) if analyses else None
    rows = field_rows(standard, extraction, settings.min_confidence)

    # Step 3 - review every value, with a preview, until the user is happy
    console.rule("[bold]Step 3/4 · Review fields")
    console.print(
        "[dim]Enter keeps a value · type to correct it · '-' clears it · #N picks candidate N[/]"
    )
    while True:
        for row in rows:
            _review_row(row)
        values, _ = docfill.collect_values(None, {row.name: row.value for row in rows})
        console.print(_preview(standard, values))
        missing = [row.label for row in rows if row.required and row.name not in values]
        if missing:
            console.print(f"[red]Still missing:[/] {escape(', '.join(missing))}")
        if typer.confirm("Is everything correct?", default=not missing):
            break

    # Step 4 - save under a new name
    console.rule("[bold]Step 4/4 · Save")
    if missing and not typer.confirm("Create the PDF with those fields left blank?"):
        console.print("Nothing saved.")
        raise typer.Exit(code=1)
    filename = typer.prompt("New file name", default=suggest_filename(standard, values))
    try:
        result = docfill.fill(standard, None, values, allow_missing=bool(missing))
    except DocFillError as exc:
        _fail(str(exc))
    path = save_output(output_dir or settings.output_dir, filename, result.pdf)
    console.print(f"[green]Saved[/] {path}")


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
