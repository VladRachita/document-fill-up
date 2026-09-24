"""Command line interface: ``docfill --help``."""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

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
    output: Annotated[Path | None, typer.Option("--output", "-o", help="Output PDF path.")] = None,
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
