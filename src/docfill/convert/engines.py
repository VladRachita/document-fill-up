"""The engines that convert: Word (.docx, .doc) to PDF and PDF to Word (.docx).

=============  ===========  ===================================================================
Engine         Direction    How
=============  ===========  ===================================================================
libreoffice    Word → PDF   LibreOffice Writer lays the document out and exports it, fonts
                            embedded. With the metric-compatible fonts installed (Liberation for
                            Times New Roman / Arial / Courier New, Carlito for Calibri, Caladea
                            for Cambria) lines and pages break where Word breaks them.
pdf2docx       PDF → Word   rebuilds paragraphs, tables, images and page margins from the PDF
                            (PyMuPDF): a Word document that can be edited as usual.
ocr            PDF → Word   for scans: the text of every page read with Tesseract and written
                            as paragraphs, titles, lists and bold words, like a document typed
                            in Word (see :mod:`docfill.convert.ocr`). The other engines can only
                            put the picture of a scanned page in the Word document.
libreoffice    PDF → Word   LibreOffice's PDF import: every line in a frame of its own, where
                            the PDF prints it. The text is all there, hard to edit.
=============  ===========  ===================================================================

Every engine runs in a process of its own (Tesseract: one per page), with a time limit: a
damaged file cannot hang or crash docfill, and pdf2docx's logging set-up stays out of docfill's.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docfill.config import Settings, get_settings
from docfill.errors import ConversionError, ConverterUnavailableError

PDF, WORD = "pdf", "docx"  # the two targets
INSTALL_LIBREOFFICE = "apt install libreoffice-writer-nogui, or brew install --cask libreoffice"


def libreoffice() -> str | None:
    """The LibreOffice command on this machine, or ``None``."""
    for tool in ("soffice", "libreoffice"):
        if path := shutil.which(tool):
            return path
    return None


def libreoffice_missing() -> str | None:
    """Why LibreOffice cannot convert Word documents here, or ``None`` when it can."""
    tool = libreoffice()
    if tool is None:
        return f"LibreOffice is not installed ({INSTALL_LIBREOFFICE})"
    program = Path(os.path.realpath(tool)).parent
    # Distributions split LibreOffice in packages: the core alone cannot open a Word document.
    if (program / "soffice.bin").exists() and not any(program.glob("*swlo*")):
        return "LibreOffice Writer is not installed (apt install libreoffice-writer-nogui)"
    return None


def pdf2docx_missing() -> str | None:
    if importlib.util.find_spec("pdf2docx") is None:
        return 'pdf2docx is not installed (pip install "docfill[convert]")'
    return None


def _message(output: bytes | str) -> str:
    """The line of a tool's output that says what went wrong."""
    text = output.decode("utf-8", "replace") if isinstance(output, bytes) else output
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    errors = [line for line in lines if re.search(r"error|exception", line, re.IGNORECASE)]
    return (errors or lines or ["no output"])[-1][:300]


def run_libreoffice(
    data: bytes,
    suffix: str,
    convert_to: str,
    timeout: int,
    infilter: str | None = None,
) -> bytes:
    """Convert ``data`` (a file ending in ``suffix``) with LibreOffice: ``convert_to`` is its
    ``--convert-to`` argument (``pdf``, ``docx:MS Word 2007 XML``)."""
    if missing := libreoffice_missing():
        raise ConverterUnavailableError(missing)
    with tempfile.TemporaryDirectory(prefix="docfill-convert-") as folder:
        work = Path(folder)
        source = work / f"document{suffix}"
        source.write_bytes(data)
        command = [
            str(libreoffice()),
            # A profile of its own: conversions can run side by side, nothing is left behind.
            f"-env:UserInstallation={(work / 'profile').as_uri()}",
            "--headless",
            "--norestore",
            "--nolockcheck",
        ]
        if infilter:
            command.append(f"--infilter={infilter}")
        command += ["--convert-to", convert_to, "--outdir", str(work / "out"), str(source)]
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=timeout,
                env={**os.environ, "HOME": folder},
            )
        except subprocess.TimeoutExpired as exc:
            raise ConversionError(f"LibreOffice did not finish within {timeout} s") from exc
        except OSError as exc:
            raise ConversionError(f"LibreOffice could not be started ({exc})") from exc
        produced = sorted((work / "out").glob("document.*"))
        if not produced:
            output = completed.stderr + completed.stdout
            raise ConversionError(f"LibreOffice could not convert the file ({_message(output)})")
        return produced[0].read_bytes()


# Runs in a process of its own (see the module's documentation).
_PDF2DOCX = """
import sys
from pdf2docx import Converter

converter = Converter(sys.argv[1])
try:
    converter.convert(sys.argv[2])
finally:
    converter.close()
"""


@dataclass
class Produced:
    """What an engine made: the converted document, what to know about it and, read with OCR,
    how sure the reading is."""

    data: bytes
    warnings: list[str] = field(default_factory=list)
    ocr: dict[str, Any] | None = None


def _pdf2docx(data: bytes, suffix: str, settings: Settings) -> Produced:
    timeout = settings.convert_timeout
    with tempfile.TemporaryDirectory(prefix="docfill-convert-") as folder:
        source, target = Path(folder) / "document.pdf", Path(folder) / "document.docx"
        source.write_bytes(data)
        try:
            completed = subprocess.run(
                [sys.executable, "-c", _PDF2DOCX, str(source), str(target)],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise ConversionError(f"pdf2docx did not finish within {timeout} s") from exc
        if completed.returncode != 0 or not target.is_file():
            raise ConversionError(
                f"pdf2docx could not convert the file ({_message(completed.stderr)})"
            )
        # A page pdf2docx cannot rebuild is left out, and only logged: say so.
        warnings = [
            f"pdf2docx: {message}" for message in re.findall(r"\[ERROR\]\s*(.+)", completed.stderr)
        ]
        return Produced(target.read_bytes(), warnings)


def _word_to_pdf(data: bytes, suffix: str, settings: Settings) -> Produced:
    return Produced(run_libreoffice(data, suffix, "pdf", settings.convert_timeout))


def _pdf_import(data: bytes, suffix: str, settings: Settings) -> Produced:
    output = run_libreoffice(
        data,
        suffix,
        "docx:MS Word 2007 XML",
        settings.convert_timeout,
        infilter="writer_pdf_import",
    )
    return Produced(
        output,
        [
            "LibreOffice puts every line of the PDF in a frame of its own: the text is "
            "where the PDF prints it, but it is hard to edit"
        ],
    )


def _ocr(data: bytes, suffix: str, settings: Settings) -> Produced:
    from docfill.convert.ocr import scan_to_word

    result = scan_to_word(data, settings)
    warnings = []
    if result.uncertain:
        warnings.append(
            f"{len(result.uncertain)} word{'s were' if len(result.uncertain) != 1 else ' was'} "
            "read with little confidence: check them in the Word document (listed with the "
            "text score)"
        )
    if result.marks:
        warnings.append(
            f"{result.marks} stamp{'s' if result.marks != 1 else ''}, signatures or handwritten "
            "notes kept as pictures where they are on the page (handwriting is not read as text)"
        )
    elif not settings.ocr_keep_marks:
        warnings.append("Stamps, signatures and handwriting were left out (a clean copy)")
    stats = {
        "words": result.words,
        "confidence": round(result.confidence, 1),
        "uncertain": result.uncertain,
        "dropped": result.dropped,
        "marks": result.marks,
    }
    return Produced(result.data, warnings, stats)


def ocr_missing() -> str | None:
    from docfill.convert.ocr import ocr_missing as missing

    return missing(get_settings())


@dataclass(frozen=True)
class Engine:
    name: str
    target: str  # PDF or WORD
    title: str
    description: str
    missing: Callable[[], str | None]  # why it cannot run here, None when it can
    run: Callable[[bytes, str, Settings], Produced]  # data, suffix (.docx, .doc, .pdf)

    def as_dict(self) -> dict[str, object]:
        reason = self.missing()
        return {
            "name": self.name,
            "target": self.target,
            "title": self.title,
            "description": self.description,
            "available": reason is None,
            "missing": reason,
        }


# In order of preference: "auto" takes the first one installed.
ENGINES: dict[str, list[Engine]] = {
    PDF: [
        Engine(
            "libreoffice",
            PDF,
            "LibreOffice",
            "Lays the Word document out like Word and exports it with its fonts embedded.",
            libreoffice_missing,
            _word_to_pdf,
        ),
    ],
    WORD: [
        Engine(
            "pdf2docx",
            WORD,
            "pdf2docx",
            "Rebuilds paragraphs, tables and images: a Word document you can edit.",
            pdf2docx_missing,
            _pdf2docx,
        ),
        Engine(
            "ocr",
            WORD,
            "OCR (scans)",
            "Reads the text of scanned pages and writes it as paragraphs, titles and lists: "
            "a Word document you can edit. Chosen by Auto for a scan.",
            ocr_missing,
            _ocr,
        ),
        Engine(
            "libreoffice",
            WORD,
            "LibreOffice (PDF import)",
            "Every line in a frame where the PDF prints it: the text is all there, hard to edit.",
            libreoffice_missing,
            _pdf_import,
        ),
    ],
}


def engines_for(target: str | None = None) -> list[Engine]:
    if target is None:
        return [engine for found in ENGINES.values() for engine in found]
    if target not in ENGINES:
        raise ConversionError(f"cannot convert to '{target}': choose one of {list(ENGINES)}")
    return ENGINES[target]


def choose_engine(target: str, name: str = "auto") -> Engine:
    """The engine called ``name`` (``auto``: the best one installed) converting to ``target``."""
    candidates = engines_for(target)
    if name != "auto":
        candidates = [engine for engine in candidates if engine.name == name]
        if not candidates:
            names = ", ".join(["auto", *(engine.name for engine in engines_for(target))])
            raise ConversionError(f"no engine '{name}' converts to {target}: choose {names}")
    for engine in candidates:
        if engine.missing() is None:
            return engine
    reasons = "; ".join(f"{engine.name}: {engine.missing()}" for engine in candidates)
    raise ConverterUnavailableError(f"no engine can convert to {target} here ({reasons})")
