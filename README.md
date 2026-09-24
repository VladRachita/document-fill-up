# document-fill-up (`docfill`)

A Python machine-learning project that **reads documents** (PDF, Word, PNG, JPEG), **cleans**
them, **detects what kind of document** each one is, **extracts the data** a standard document
needs and **fills the standard documents stored in a database**, exporting **new PDFs**. Every
document you check in the wizard **teaches it**, so errors decrease over time.

It is built for **Romanian trade register work**: opening, changing and closing companies
(**SRL, SRL-D, SA**) and authorised natural persons (**PFA, II, IF**). A **legal knowledge base**
knows each procedure (forms, documents of the file, legal checks with the article they come
from) and can be **fed and corrected over time**: laws, rules, procedures and document types,
each versioned and verified by a named person (see [Legal knowledge](#legal-knowledge-romania)).

The source is typically the **Romanian identity card (CI)** (a birth certificate, an act
constitutiv or an already filled form can be added); the results are the official **ONRC
Anexa 2a** (cerere de înregistrare) and **ONRC Anexa 4** (declarație condiții de funcționare)
PDF forms.

```
 CI photo / scan, birth certificate, filled forms, PDF / DOCX / PNG / JPG
          │
   ┌──────▼──────┐   text layer, Word body + tables, values of filled PDF forms,
   │ 1. Read     │   OCR (Tesseract, several preprocessing variants, Romanian + English)
   └──────┬──────┘
   ┌──────▼──────┐   fix unicode, drop page numbers / repeated headers / form blanks /
   │ 2. Clean    │   OCR noise, redact card numbers and IBANs
   └──────┬──────┘
   ┌──────▼──────┐   ML classifier (scikit-learn) or exact match of a filled form's fields:
   │ 3. Detect   │   identity card, birth certificate, Anexa 2a, Anexa 4, other
   └──────┬──────┘
   ┌──────▼──────┐   labels (incl. "Nume/Nom/Last name"), CNP, SERIA/NR, MRZ with check
   │ 4. Extract  │   digits, address parts, spaCy NER; derivations and cross-checks
   └──────┬──────┘
   ┌──────▼──────┐   the stored standard documents, used verbatim (checksum-protected);
   │ 5. Review   │   wizard: see and correct every value live; the procedure's legal checks
   └──────┬──────┘   (capital, associates, firm name, age...) with the article they cite
   ┌──────▼──────┐   PDF forms filled with an embedded Unicode font (ș, ț, ă displayed in
   │ 6. Export   │   every viewer), text documents rendered with ReportLab
   └──────┬──────┘
   ┌──────▼──────┐   calibrate confidence, learn labels and spellings, retrain the document
   │ 7. Learn    │   classifier, remember the filer's own details, flag rules people override
   └─────────────┘
```

## Tech stack

| Stage | Libraries |
|---|---|
| Reading | `pypdf`, `pypdfium2`, `python-docx`, `Pillow`, `pytesseract` + Tesseract OCR |
| Cleaning | `ftfy`, regular expressions, Luhn / IBAN mod-97 checks |
| Machine learning | `scikit-learn` (document type classifier, TF-IDF search of laws), `spaCy` NER, `pycountry` |
| Legal knowledge | versioned YAML entries (legal forms, procedures, declarative rules, document types), fed laws split into articles |
| Validation | CNP control digit, ICAO 9303 MRZ check digits, date/IBAN/e-mail checks |
| Standard documents | `SQLAlchemy` 2 (SQLite by default, PostgreSQL via URL), `PyYAML`, `pydantic` |
| PDF export | `pypdf` (AcroForm filling) + `ReportLab` (Unicode appearances, text documents) |
| Interfaces | Web wizard (FastAPI + one self-contained HTML page), `Typer` CLI, REST API |

## Installation

System packages: **Tesseract OCR with Romanian** and a TrueType font with full Unicode coverage
(DejaVu is auto-detected) so names like *Ștefănescu* or *Țară* render.

```bash
# Debian / Ubuntu
sudo apt install tesseract-ocr tesseract-ocr-ron tesseract-ocr-eng fonts-dejavu-core
# macOS
brew install tesseract tesseract-lang

python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m spacy download en_core_web_sm       # ML model for free text
docfill templates seed                        # load the standard documents (ONRC forms...)
docfill knowledge seed                        # load the legal knowledge (procedures, rules...)
```

Or with Docker (OCR, fonts and the model included; wizard and API on http://localhost:8000):

```bash
docker build -t docfill .
docker run -p 8000:8000 -v docfill-data:/data docfill
```

## The wizard

```bash
docfill serve                 # open http://127.0.0.1:8000
```

1. **Documents.** Choose **what you are doing**: the legal form (SRL, SRL-D, SA, PFA, II, IF)
   and the operation (înființare, modificare, radiere). docfill ticks the forms of that
   procedure (e.g. *Anexa 2a* and *Anexa 4* together: they are filled from the same data), lists
   the **documents of the file** (dosar) and its legal checks, and shows which knowledge is not
   verified yet. Then drop the source files (identity card, birth certificate, act constitutiv,
   an already filled form...). *Fill in by hand* is also possible.
2. **Scan & clean.** For each file: the **detected document type** with its confidence (change
   it if wrong; the classifier learns from it), the text read from the file next to the
   cleaned text (**fix OCR mistakes there, fields are re-extracted as you type**), what was
   removed or redacted.
3. **Review fields.** Fields are grouped (Person, Birth, Domicile, Identity card, Company,
   Request, Contact person, Billing, Filed by). **Needs attention** shows only what has to be
   looked at: missing required values, validation problems, low confidence. Every value shows
   its confidence, how it was found (label, machine-readable zone, pattern, filled form, ML
   model, derived, remembered, default) and **where** it was found; other candidates are one
   click away. **Problems are shown live** (invalid CNP, date of birth that does not match the
   CNP, expired identity card, bad IBAN...) and derivable values are filled as you type (date
   of birth and sex from the CNP, trade register office from the company county...). A **live
   preview** of each reference document follows every keystroke. The procedure's **legal
   checks** run live too (minimum capital of an SA, number of associates, the legal form in the
   firm name, the holder's age...), each with its legal basis and, when the law was fed to
   docfill, the article's text.
4. **Save PDF.** The **file checklist** shows the forms docfill created, those still to prepare
   and the supporting documents (ticked when an uploaded file was recognised as that document).
   A failed legal check of level *error* must be fixed or explicitly acknowledged. Choose the
   file name; one PDF per reference document is saved in the output folder
   (`DOCFILL_OUTPUT_DIR`, never overwritten) and downloaded. The page shows what docfill learned
   from the review, including the checks you overrode (recorded so rules get reviewed).

The same review runs in the terminal: `docfill wizard ci.jpg -t onrc-anexa-2a -t onrc-anexa-4`
(procedures and legal checks are in the web wizard; in the terminal use
`docfill knowledge check PROCEDURE -s FIELD=VALUE`).

## Romanian documents

**Identity card (CI).** Multilingual labels with the value underneath (`Nume/Nom/Last name`),
`SERIA AX NR 123456`, the CNP, the validity range `22.06.22-14.11.2032`, the domicile in card
style (`Jud.CJ Mun.Cluj-Napoca` / `Str.Florilor nr.5 bl.A2 sc.1 et.3 ap.10`, split into
street, number, block, staircase, floor, apartment, locality, county) and the **machine
readable zone** (`IDROU...`, TD1/TD2/TD3). MRZ values are only trusted when their check digit
matches; OCR confusions (O/0, I/1, B/8) are repaired when that makes the check digit match. The
printed names are confirmed against the MRZ.

**CNP.** The control digit is verified; a CNP that fails is not filled automatically but offered
for review. A valid CNP gives the date of birth, sex and issuing county, and cross-checks them.

**Birth certificate.** Detected and read (labels, parents, place of birth). Note: handwritten
values (old certificates) cannot be read by Tesseract; the wizard asks for them.

**Place names.** Diacritics lost by OCR are restored for county seats and major towns
("Fagaras" -> "Făgăraș", "Focşani" -> "Focșani"); others are learned from your corrections.

**Filled forms as a source.** A filled Anexa 2a (or any registered PDF form) is recognised from
its form fields and read back through its field map, so a finished Anexa 2a fills Anexa 4.

## Learning: fewer errors over time

docfill never invents values: every value is copied from a document, derived from copied values
(and marked as such), remembered, a default of the standard document, or typed by you. What it
learns from each saved review:

| What | Effect |
|---|---|
| **Confidence calibration** | Each extraction method's confidence is corrected by its track record per field and document type. A method that is often wrong stops auto-filling that field (it is only proposed); one that is always right is trusted more. |
| **Learned labels** | A value you typed that was printed in the document after a label nobody knew teaches that label. |
| **Spelling fixes** | Corrections that only change a place name's spelling are applied next time. |
| **Document types** | Every confirmed document type is a training example for the classifier. |
| **Your details** | Fields marked `remember` (lawyer / filer, contact person, billing, communication, submitted documents) are proposed again. |

```bash
docfill learn stats                     # accuracy per field and method, learned labels...
docfill learn import-filled done.pdf    # learn your own details from forms you already filled
docfill evaluate -t onrc-anexa-4 --expected done_anexa4.pdf ci.jpg   # measure errors
docfill learn reset --yes               # forget everything learned
```

Privacy: review outcomes store no values, learned labels store label words only, document-type
examples have every reviewed value and every digit removed; only `remember` fields keep values
(your own details). Everything stays in your database.

## Legal knowledge (Romania)

docfill is used for Romanian trade register procedures. What it knows about them lives in a
**knowledge base** (in the database, next to the standard documents) that people feed, correct
and verify over time. Open it at **http://127.0.0.1:8000/knowledge**, or use
`docfill knowledge ...` and the `/knowledge/...` API.

| Kind | What it holds | Bundled |
|---|---|---|
| `entity` | a legal form | SRL, SRL-D, SA (societăți), PFA, II, IF (persoane fizice) |
| `procedure` | a legal form × an operation: the official forms (and which docfill fills), the documents of the file, extra fields to collect, legal basis | 18: every form × înființare / modificare / radiere |
| `rule` | a legal check with its legal basis | 22, e.g. SA capital ≥ 90.000 lei and ≥ 2 shareholders (Legea 31/1990 art. 10), SRL ≤ 50 associates (art. 12), legal form in the firm name, PFA / II holder ≥ 18 years, IF ≥ 2 members, CAEN classes per PFA / II / IF |
| `doc_type` | a document the classifier learns to recognise | act constitutiv, dovada sediului, hotărâre AGA / decizie asociat unic, certificat de înregistrare, declarație beneficiar real, specimen de semnătură, acord de constituire IF |

**Verified by people.** Everything starts as `draft`. A lawyer, notary or expert checks an
entry against the law in force and marks it `verified` (who and when are recorded); any later
change makes it `draft` again. The wizard shows which knowledge behind a procedure is not
verified yet. **The bundled knowledge is a starting point written from the laws it cites; it
must be verified before it is relied on** (values that changed recently, such as the minimum
share capital of an SRL or the SRL-D regime, say so in their notes).

**How it improves over time:**

| Fed by | Effect |
|---|---|
| **Knowledge (YAML)** | Add or correct legal forms, procedures, rules, document types. Every change is a new version kept in the history; `export` gives it all back as YAML (review it, keep it under version control). |
| **Laws** | Feed the text of a law or ONRC guide (TXT, HTML saved from legislatie.just.ro, PDF, DOCX): it is split into articles, searchable (accents optional), and the cited article is shown next to each rule. |
| **Document examples** | `teach` a document type with examples; every document type confirmed in the wizard is an example too. |
| **Saved files** | Each file saved with a procedure records which checks passed, failed or were overridden, and which documents were provided. Rules often overridden are flagged for review; documents often provided but missing from a procedure are suggested. |
| **docfill updates** | `docfill knowledge seed` brings new bundled knowledge; entries you changed or retired are never overwritten. |

Rules are declarative (no code), so they can be written by non-programmers:

```yaml
rules:
  - key: sa-capital-minim
    title: Capitalul social minim al unei SA
    applies_to: {entities: [sa], operations: [infiintare]}
    severity: error            # error: must be fixed or acknowledged; warning; info
    check: {type: min_amount, field: share_capital, value: 90000}
    message: Capitalul social al unei societăți pe acțiuni nu poate fi mai mic de 90.000 lei.
    legal_basis:
      - {citation: Legea nr. 31/1990 privind societățile, article: art. 10 alin. (1)}
```

Checks: `required`, `min_amount` / `max_amount` (`90.000 lei`), `min_count` / `max_count` (one
item per line), `contains_any` (`S.R.L.` == `SRL`), `contains_field`, `min_age` (from the CNP),
`pattern`, `lines_pattern`, `one_of`. A procedure lists its forms (`template:` the docfill
standard document, or none when docfill cannot fill it yet) and its documents (`doc_type:` to
tick them off automatically); see `src/docfill/knowledge/bundled/` for complete examples.

```bash
docfill knowledge seed                                   # bundled knowledge (keeps your changes)
docfill knowledge list --kind rule --status draft        # what still needs verifying
docfill knowledge show rule/sa-capital-minim --history   # YAML + every change
docfill knowledge verify rule/sa-capital-minim --by "Av. Maria Ionescu" --note "forma în vigoare"
docfill knowledge add corrections.yaml                   # add / correct (draft again)
docfill knowledge retire rule/firma-sa --by "Av. Maria Ionescu" --note "abrogat"
docfill knowledge ingest legea31.html --citation "Legea nr. 31/1990" --url https://legislatie.just.ro/...
docfill knowledge search capital social minim SA
docfill knowledge teach act_constitutiv examples/*.pdf
docfill knowledge check sa.infiintare -s share_capital="50.000 lei" -s associates="A"   # exit 1 on errors
docfill knowledge stats                                  # rules to review, suggested documents
docfill knowledge export knowledge.yaml
```

## Standard documents

Standard documents are stored in the database and **used 100% as stored**; each has a SHA-256
checksum and a document changed outside docfill is refused. Two kinds:

**Existing PDF forms** (like the ONRC forms): the official PDF is kept untouched; its fields are
filled and stay editable. Values are drawn with an embedded Unicode font so diacritics display in
every viewer.

```yaml
name: onrc-anexa-4
title: ONRC Anexa 4 - Declarație condiții de funcționare
kind: pdf_form
doc_type: onrc_anexa_4                 # a filled copy uploaded as a source is recognised
pdf_file: onrc-anexa-4.pdf
field_map:                             # PDF field -> docfill field (filters allowed)
  SubNume: last_name | upper
  SubCNP: cnp
  InmFirma: company_name | upper
  DataCerere: today
  # composed: "pg. 4 text 27": "{{ last_name | upper }} {{ first_name | upper }}"
lists:                                 # repeated rows, one per line in the wizard
  caen_activities: {rows: 18, columns: ["clasa_caen.0.{i}", "clasa_caen_desc.0.{i}"]}
choices:                               # option buttons: value -> button state
  # CheckBox90_2: {poștă: /v1, curier: /v2, mijloace electronice: /v3}
defaults: {id_type: CI, country: România}
remember: [represented_by, billing_iban]   # the filer's own details
optional_fields: [building, entrance, floor, apartment]
```

**Text documents** with `{{ placeholders }}` (`# ` headings, `---` rules, filters `upper`,
`lower`, `title`, `{{ today }}`) rendered to PDF with ReportLab.

Adding a new official form:

```bash
docfill forms blank filled_example.pdf blank.pdf   # remove every value (safe to share)
docfill forms inspect blank.pdf --suggest          # fields, printed labels, suggested mapping
docfill templates add my-form.yaml
```

## Command line

```bash
python examples/make_samples.py                 # sample documents (fictitious people)
docfill extract examples/samples/ci_popescu.jpg # what was found, with confidence
docfill fill ci.jpg -t onrc-anexa-2a -o out/anexa2a.pdf --set company_name="Exemplu SRL"
docfill wizard ci.jpg -t onrc-anexa-2a -t onrc-anexa-4
docfill templates list | show NAME | add FILE.yaml | seed | remove NAME
docfill knowledge seed | list | show | add | verify | retire | export | ingest | texts | search | teach | check | stats
```

`fill` stops and lists required fields it could not find; provide them with `--set` or use
`--allow-missing`. The output is always a PDF.

## REST API

`docfill serve`, interactive docs at `/docs`. No authentication: keep it on a private network.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health`, `/fields`, `/doctypes` | Status; usable fields; recognised document types |
| GET / POST / DELETE | `/templates`, `/templates/{name}` | Standard documents |
| POST | `/extract`, `/fill` | Extract fields as JSON / fill one standard document |
| GET | `/wizard` | The web wizard (`/` redirects here) |
| POST | `/wizard/analyze`, `/wizard/reextract` | Read, detect type, extract; re-extract after corrections |
| POST | `/wizard/preview` | Live previews, validation problems, derived values |
| POST | `/wizard/export` | Save one PDF per reference document and learn from the review |
| GET | `/wizard/files/{name}`, `/learning/stats` | Download a saved PDF; what was learned |
| GET | `/knowledge` | The knowledge page (search, feed, verify, feedback) |
| GET | `/knowledge/cases`, `/knowledge/procedures/{key}` | Legal forms, operations, procedures (forms, documents, rules) |
| POST | `/knowledge/check` | Run a procedure's legal checks on values |
| GET / POST | `/knowledge/entries`, `/knowledge/entries/{kind}/{key}` | List, read (YAML + history), add or correct entries |
| POST | `/knowledge/entries/{kind}/{key}/verify`, `.../retire` | Verify (by a named person) or retire an entry |
| GET / POST | `/knowledge/export`, `/knowledge/seed` | All knowledge as YAML; load the bundled knowledge |
| GET / POST / DELETE | `/knowledge/texts` | Laws and guides fed to docfill |
| GET | `/knowledge/search?q=` | Search the laws and the knowledge |
| POST | `/knowledge/doctypes/{name}/examples` | Teach the classifier with example documents |
| GET | `/knowledge/stats` | Status, rules to review, suggested documents |

The wizard endpoints accept an optional `procedure` (e.g. `srl.infiintare`): its fields are
asked for, its legal checks are returned by `/wizard/preview`, and `/wizard/export` refuses
failed error-level checks unless `legal_acknowledged` is set (overrides are recorded).

## Configuration

Environment variables (or `.env`, see `.env.example`):

| Variable | Default | |
|---|---|---|
| `DOCFILL_DATABASE_URL` | `sqlite:///./docfill.db` | Any SQLAlchemy URL (standard documents and learning data) |
| `DOCFILL_OCR_LANGUAGES` | `auto` | Tesseract languages; `auto` = Romanian + English when installed |
| `DOCFILL_OCR_DPI` | `300` | Resolution for rasterising scanned PDF pages |
| `DOCFILL_TESSERACT_CMD` | – | Path to `tesseract` if it is not on `PATH` |
| `DOCFILL_SPACY_MODEL` | `en_core_web_sm` | NER model (empty disables NER) |
| `DOCFILL_MIN_CONFIDENCE` | `0.5` | Minimum (calibrated) confidence for a value to be filled in |
| `DOCFILL_MAX_FILE_SIZE` | `26214400` | Upload limit in bytes |
| `DOCFILL_OUTPUT_DIR` | `output` | Where the wizard saves the PDFs |
| `DOCFILL_PDF_FONT_PATH` | auto | TrueType font for exported PDFs |

## Project layout

```
src/docfill/
  readers/             PDF (text, form values, OCR of scans), DOCX, images; multi-variant OCR
  sanitize.py          text cleaning and redaction
  doctypes.py          document type classifier (scikit-learn), incl. types taught as knowledge
  knowledge/           legal knowledge: specs, declarative rules, versioned store, laws + search,
    bundled/           Romanian legal forms, procedures, rules and document types (YAML)
  extraction/          field catalog, label rules, patterns (CNP, MRZ...), NER, derivations
  ro.py, mrz.py        Romanian knowledge (counties, CNP, addresses, places) and MRZ parsing
  validation.py        cross-checks and live validation
  learning.py          review outcomes, calibration, learned labels, memory
  templates/           standard documents: placeholders, DB model, repository, YAML loader
  standard_documents/  bundled standard documents (ONRC Anexa 2a / 4 blank forms + YAML)
  export/              PDF forms (fill, blank, inspect) and text rendering
  pipeline.py, app.py  read -> clean -> detect -> extract -> fill; wiring with learning
  wizard.py, web/      review logic, the web wizard and the knowledge page
  samples.py           synthetic documents (fictitious Romanian identity card)
  cli.py, api.py       Typer CLI and FastAPI app
tests/                 pytest suite (fictitious data only)
```

## Development

```bash
pytest                                  # OCR / NER tests are skipped if Tesseract / the model are missing
ruff check src tests examples && ruff format --check src tests examples
```

## Limitations and next steps

* **Forms not in docfill yet.** docfill fills the ONRC Anexa 2a (înmatriculare) and Anexa 4.
  The procedures list the other official forms they need (Anexa 2a for mențiuni / radiere, the
  forms for PFA / II / IF, the beneficial owner declaration) as "not in docfill yet"; add each
  with `docfill forms inspect` + `docfill templates add`, then set its `template` in the
  procedure. Until then, those procedures cannot produce PDFs.
* **The bundled legal knowledge is draft** and must be verified by a legal professional against
  the law in force; it has no expert opinion beyond the rules written in it. docfill does not
  use a large language model: it checks values against the rules and searches the laws you feed
  it, locally.

* Handwriting (old birth certificates) is not readable by Tesseract; a handwriting OCR model
  (or a cloud OCR service, if sending the data out is acceptable) would be needed.
* The identity card reader is tested on synthetic cards built from the official layout; real
  phone photos (glare, angle) may need better image straightening - try yours and correct in
  the wizard, the corrections are learned.
* CAEN activity names are typed (or read from a filled Anexa 4); a CAEN Rev. 3 list could fill
  the name from the code.
* Legacy `.doc` files are not supported (save as `.docx`).
