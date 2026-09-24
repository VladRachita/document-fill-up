# document-fill-up (`docfill`)

A Python machine-learning project that **scans documents** (PDF, Word, PNG, JPEG), **sanitizes**
them, **extracts personal information** (first/last name, home and place details) and
**fills standard documents stored in a database**, exporting the result as a **new PDF**.

```
 PDF / DOCX / PNG / JPG
          │
   ┌──────▼──────┐   text layer (pypdf), Word body + tables (python-docx),
   │ 1. Read     │   OCR for images and scanned pages (Tesseract)
   └──────┬──────┘
   ┌──────▼──────┐   fix unicode (ftfy), drop page numbers, repeated headers/footers,
   │ 2. Sanitize │   form blanks and OCR noise, redact card numbers / IBANs
   └──────┬──────┘
   ┌──────▼──────┐   labelled values ("Surname: SMITH", "Nume: POPESCU"), address
   │ 3. Extract  │   patterns, ML named-entity recognition (spaCy) for free text
   └──────┬──────┘
   ┌──────▼──────┐   standard document from the database (SQLAlchemy), used verbatim:
   │ 4. Fill     │   only its {{ placeholders }} are replaced, integrity-checked
   └──────┬──────┘
   ┌──────▼──────┐   ReportLab for text documents, pypdf for existing PDF forms
   │ 5. Export   │
   └─────────────┘
          │
        PDF
```

## Tech stack

| Stage | Libraries |
|---|---|
| Reading | `pypdf`, `pypdfium2` (render scanned pages), `python-docx`, `Pillow`, `pytesseract` + Tesseract OCR |
| Sanitizing | `ftfy`, regular expressions, Luhn / IBAN mod-97 checks |
| ML extraction | `spaCy` NER (`en_core_web_sm` by default), `pycountry` |
| Standard documents | `SQLAlchemy` 2 (SQLite by default, PostgreSQL etc. via URL), `PyYAML`, `pydantic` |
| PDF export | `ReportLab`, `pypdf` (AcroForm filling) |
| Interfaces | `Typer` CLI, `FastAPI` REST API |

## Installation

System packages: **Tesseract OCR** (for images and scanned PDFs) and a TrueType font with
full unicode coverage (DejaVu is auto-detected) so names like *Ștefan* or *Țară* render.

```bash
# Debian / Ubuntu
sudo apt install tesseract-ocr tesseract-ocr-eng fonts-dejavu-core   # + tesseract-ocr-ron for Romanian
# macOS
brew install tesseract

python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m spacy download en_core_web_sm       # the ML model used for free text
```

Or with Docker (OCR, fonts and the model included; wizard and API on http://localhost:8000):

```bash
docker build -t docfill .
docker run -p 8000:8000 -v docfill-data:/data docfill
```

## Step-by-step wizard (recommended)

The wizard shows every decision the pipeline makes so errors can be corrected as they
appear, and saves the filled reference document under a new file name.

```bash
docfill templates seed        # once: load the bundled standard documents
docfill serve                 # then open http://127.0.0.1:8000
```

1. **Documents.** Pick the reference (standard) document and drop the source files: PDF, Word,
   PNG or JPEG. The fields the reference document needs are listed, required ones highlighted.
   There is also a *fill in by hand* option.
2. **Scan & clean.** For each file, the text read from it (OCR for photos and scans) sits next
   to the cleaned text, with what was removed or redacted. **Fix OCR mistakes directly in the
   cleaned text:** fields are re-extracted as you type.
3. **Review fields.** Every field of the reference document with its value, confidence, how it
   was found (label rule, pattern, ML model, derived) and the line it came from ("Show where").
   Other candidates can be picked with one click. Missing required fields are flagged. A
   **live preview** of the reference document updates on every keystroke. Click a
   highlighted value in the preview to jump to its field.
4. **Save PDF.** Choose the new file name (a name like
   `residence-declaration_Popescu_Ion_2026-09-24` is suggested). The PDF is saved in the
   output folder (`DOCFILL_OUTPUT_DIR`, default `./output`) and downloaded. Existing files are
   never overwritten.

The same flow is available in the terminal:

```bash
docfill wizard examples/samples/id_card.jpg -t residence-declaration
```

For each field it shows what was found, where and how confidently. Press Enter to keep a
value, type to correct it, `-` to clear it or `#N` to pick candidate N. It then shows the
filled document and asks for the new file name.

## Quick start (CLI)

```bash
python examples/make_samples.py              # creates examples/samples/*
docfill templates seed                       # load the bundled standard documents
docfill templates list

docfill extract examples/samples/id_card.jpg             # what was found, with confidence
docfill extract examples/samples/letter.pdf --show-text  # free text -> ML NER

docfill fill examples/samples/application_ro.docx -t residence-declaration -o out/residence.pdf
```

`fill` prints each field with its value and where it came from. If a required field cannot be
found the command stops and lists it. You can then provide values yourself or leave blanks:

```bash
docfill fill scan.pdf -t residence-declaration -o out.pdf \
    --set street_address="1 Main Road" --set city="Iași"   # provide / override values
docfill fill scan.pdf -t residence-declaration -o out.pdf --allow-missing   # blank lines
```

Several source documents can be combined (e.g. an ID card for the name and a utility bill for the
address); the most confident value per field wins:

```bash
docfill fill id_card.jpg utility_bill.pdf -t residence-declaration -o out.pdf
```

## Standard documents

Standard documents are stored in the database and are **used 100% as stored**. Rendering is a
single substitution of the `{{ placeholders }}`. Every other character is copied verbatim, and
inserted values are never re-interpreted. Each document has a SHA-256 checksum. A document that
was changed outside docfill (e.g. directly in SQL) fails the integrity check and is refused.
Re-registering a changed document bumps its version. The exported PDF records the template name,
version and checksum in its metadata.

### Text documents

```yaml
# my-declaration.yaml
name: my-declaration            # unique id: lower-case letters, digits, - and _
title: Declaration of Residence
description: Sworn statement of a person's home address.
kind: text
optional_fields: [region]       # may stay blank; all other placeholders are required
body: |
  # DECLARATION OF RESIDENCE

  I, the undersigned {{ first_name }} {{ last_name | upper }}, declare that I live at:
  {{ street_address }}, {{ postal_code }} {{ city }}, {{ country }}

  ---
  Date: {{ today }}
```

```bash
docfill templates add my-declaration.yaml
docfill templates show my-declaration
```

* `# ` / `## ` start headings, `---` draws a horizontal rule, and line breaks are kept.
* Filters: `upper`, `lower`, `title` (`{{ last_name | upper }}`).
* `{{ today }}` is the current date. Any other name (e.g. `{{ case_number }}`) can be supplied
  with `--set case_number=...`.

### Existing PDF forms

If a standard document already exists as a fillable PDF (AcroForm), store the PDF itself. Its
layout is left untouched and only the form fields get filled:

```yaml
name: city-hall-form
title: City hall registration form
kind: pdf_form
pdf_file: city_hall_form.pdf     # relative to this YAML file
field_map:                       # PDF field name -> docfill field (filters allowed)
  txtSurname: last_name | upper
  txtGivenNames: first_name
  txtTown: city
```

Without a `field_map`, the PDF field names are used directly as docfill field names.

## Extracted fields

| Field | Example labels recognised (case/accent-insensitive) |
|---|---|
| `first_name` | First name, Given name(s), Forename, Prenume |
| `last_name` | Last name, Surname, Family name, Nume |
| `full_name` | Name, Full name, Applicant, Nume și prenume |
| `full_address` | Address, Home address, Residence, Adresa, Domiciliu |
| `street_address` | Street, Street address, Address line 1, Strada |
| `city` | City, Town, Locality, Oraș, Localitate |
| `postal_code` | Postal code, Postcode, ZIP, Cod poștal |
| `region` | State, County, Province, Județ |
| `country` | Country, Țara |
| `place_of_birth` | Place of birth, Birthplace, Locul nașterii |

How values are found, from most to least trusted:

1. **Labels** (confidence 0.95). Handles `Label: value`, several labels on one line, values on
   the next line(s), multi-line addresses, and Word tables (key/value or header rows).
2. **Derived** values. A full name is split into first/last name (`POPESCU Ion`, `Smith, John`,
   `Ludwig van Beethoven`). A one-line address is split into street / postal code / city /
   region / country, and the reverse also works.
3. **Address patterns** (0.5–0.55). Unlabelled street lines in English, Romanian and German
   formats.
4. **ML named-entity recognition** (0.5–0.7). spaCy finds people and places in free text
   ("*My name is Maria Garcia, born in Madrid, living in Berlin*").

Values under `DOCFILL_MIN_CONFIDENCE` (0.5) are not used. Values are only copied from the
source documents, never generated. Adding a field is one `FieldSpec` entry in
`src/docfill/extraction/fields.py`.

## REST API

```bash
docfill serve            # http://127.0.0.1:8000/docs for the interactive OpenAPI UI
```

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Status, and whether OCR and the NER model are available |
| GET | `/fields` | Fields usable in standard documents |
| GET / POST | `/templates` | List / create or update a standard document (JSON; `pdf_base64` for PDF forms) |
| GET / DELETE | `/templates/{name}` | Show / delete a standard document |
| POST | `/extract` | Upload `files` and get the extracted fields as JSON |
| POST | `/fill` | Upload `files` + `template` (+ `values` JSON, `allow_missing`) and get the PDF back |
| GET | `/wizard` | The step-by-step web wizard (`/` redirects here) |
| POST | `/wizard/analyze` | Wizard step 2: raw + cleaned text per file and the reviewed fields |
| POST | `/wizard/reextract` | Re-extract fields from corrected text |
| POST | `/wizard/preview` | Live preview of the filled reference document |
| POST | `/wizard/export` | Create the PDF, save it under the chosen file name, return it |

```bash
curl -F template=residence-declaration -F files=@id_card.jpg -F files=@bill.pdf \
     -F 'values={"country": "Romania"}' -o filled.pdf http://127.0.0.1:8000/fill
```

Missing required fields return `422` with a `missing` list. The API has **no authentication**,
so keep it on a private network or behind a gateway.

## Configuration

Environment variables (or a `.env` file, see `.env.example`):

| Variable | Default | |
|---|---|---|
| `DOCFILL_DATABASE_URL` | `sqlite:///./docfill.db` | Any SQLAlchemy URL, e.g. `postgresql+psycopg://…` (install the driver) |
| `DOCFILL_OCR_LANGUAGES` | `eng` | Tesseract languages, e.g. `eng+ron` |
| `DOCFILL_OCR_DPI` | `300` | Resolution for rasterising scanned PDF pages |
| `DOCFILL_TESSERACT_CMD` | – | Path to the `tesseract` binary if it is not on `PATH` |
| `DOCFILL_SPACY_MODEL` | `en_core_web_sm` | NER model (empty disables NER), e.g. `ro_core_news_sm` |
| `DOCFILL_MIN_CONFIDENCE` | `0.5` | Minimum confidence for a value to be used |
| `DOCFILL_MAX_FILE_SIZE` | `26214400` | Upload limit in bytes |
| `DOCFILL_OUTPUT_DIR` | `output` | Folder where the wizard saves the PDFs it creates |
| `DOCFILL_PDF_FONT_PATH` | auto | TrueType font for exported PDFs |

## Project layout

```
src/docfill/
  readers/          PDF, DOCX and image readers, Tesseract OCR
  sanitize.py       text cleaning and redaction
  extraction/       field catalog, label rules, address patterns, spaCy NER, merging
  templates/        standard documents: placeholders, DB model, repository, YAML loader
  standard_documents/  bundled example standard documents (YAML)
  export/           PDF rendering (ReportLab) and PDF form filling (pypdf)
  pipeline.py       read -> sanitize -> extract -> fill -> export
  wizard.py         review helpers: field rows, candidates, preview, output file names
  web/              the web wizard (routes + a single self-contained HTML page)
  cli.py, api.py    Typer CLI (incl. `docfill wizard`) and FastAPI app
tests/              pytest suite (sample documents are generated on the fly)
examples/           script generating sample input documents
```

## Development

```bash
pytest                                  # OCR / NER tests are skipped if Tesseract / the model are missing
ruff check src tests examples && ruff format --check src tests examples
```

## Limitations and next steps

* Legacy `.doc` files are not supported (save as `.docx`).
* Label synonyms cover English and Romanian. Add others in `extraction/fields.py`.
* The default NER model is English. For mostly Romanian free text, use `ro_core_news_sm`, or
  fine-tune a spaCy model on your own annotated documents to improve free-text extraction.
* The ML model is pre-trained; corrections made in the wizard are not yet used to retrain it.
  A natural next step is to store them as labelled examples and fine-tune the spaCy model on
  your own documents.
* Possible extensions: more fields (date of birth, ID number, phone, e-mail), checkbox fields
  in PDF forms, and API authentication.
