# Local testing: run docfill and watch it learn

This guide starts docfill on your machine with a real database, gives you documents whose
correct values are known, and walks through the scenarios that show how docfill takes feedback
(your corrections in the wizard) and what it changes afterwards.

## What the repository gives you

| Need | Where |
|---|---|
| Whole stack in Docker: docfill + PostgreSQL (+ Adminer to browse the database) | `docker-compose.yml` |
| docfill image: Tesseract OCR (Romanian + English), Unicode font, spaCy model | `Dockerfile` |
| Database | PostgreSQL 16 in Docker; without Docker a SQLite file (`docfill.db`). Tables are created at start, standard documents and legal knowledge are seeded: nothing to run by hand |
| Settings | `.env.example` (copy to `.env`), every `DOCFILL_*` variable |
| Test documents with the correct answers | `python examples/make_samples.py` → `examples/samples/learning/` |
| Automated tests | `pytest`, `ruff` (see the README) |

## 1. Start docfill

### With Docker (recommended)

```bash
docker compose up --build -d                 # first build takes a few minutes
docker compose --profile tools up -d         # optional: Adminer on http://localhost:8080
```

* Wizard: http://localhost:8000 · knowledge page: http://localhost:8000/knowledge · API docs:
  http://localhost:8000/docs
* The command line runs inside the container: `docker compose exec app docfill learn stats`
* Adminer login: system *PostgreSQL*, server `db`, user / password / database `docfill`
* Ports already used? `DOCFILL_PORT=8001 DOCFILL_DB_PORT=5433 docker compose up -d`
  (`DOCFILL_ADMINER_PORT` for Adminer)
* The PDFs the wizard saves are also downloaded by your browser; the copies kept by docfill
  are in a volume: `docker compose cp app:/data/output ./output`
* Stop: `docker compose down` (everything is kept). Start from zero: `docker compose down -v`
* After pulling new code: `docker compose up --build -d` (seeding keeps what you changed)

### Without Docker

```bash
sudo apt install tesseract-ocr tesseract-ocr-ron tesseract-ocr-eng fonts-dejavu-core   # or brew
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,postgres]"
python -m spacy download en_core_web_sm
cp .env.example .env              # SQLite by default
docfill templates seed && docfill knowledge seed
docfill serve                     # http://127.0.0.1:8000
```

To use the PostgreSQL of Docker instead of SQLite: `docker compose up -d db` and set
`DOCFILL_DATABASE_URL=postgresql+psycopg://docfill:docfill@localhost:5432/docfill` in `.env`.
The local `docfill` command then works on the same data as the Docker app.

## 2. Create the test documents

```bash
python examples/make_samples.py
# or, without a local Python setup:
docker compose run --rm --no-deps -v "$PWD/examples:/app/examples" --user "$(id -u):$(id -g)" \
    app python examples/make_samples.py
```

`examples/samples/learning/` then holds (all people are fictitious; CNP and MRZ are valid):

| File | What it is |
|---|---|
| `ci_<person>_clean.jpg` | identity card, good scan |
| `ci_<person>_photo.jpg` | tilted, slightly out of focus (phone photo) |
| `ci_<person>_lowres.jpg` | small image, heavy JPEG compression |
| `ci_<person>_faded.jpg` | low contrast |
| `ci_<person>_glare.jpg` | a light reflection over the names |
| `fisa_<person>.docx` | client sheet with a label docfill does not know ("Localitatea natală") |
| `ANSWERS.md` | the correct values for each person: compare with what docfill proposes |

| Person | Worth checking |
|---|---|
| `popescu` | the reference: the clean card is read without mistakes |
| `stefanescu` | diacritics lost by OCR (Ștefănescu, Săcălaz), a commune docfill does not know |
| `muresan` | expired card; "Alba Iulia" is hard for OCR |
| `dumitru` | born in 2009: under 18, the PFA / II age check must fail |

## 3. How docfill learns, and what it cannot learn

When you click **Save PDF**, docfill compares what it proposed with what you saved, field by
field: **accepted**, **corrected**, **cleared** or **added** (typed where nothing was found).
It stores the outcome and the method that found the value (`label`, `mrz`, `pattern`,
`derived`, `learned`, `ner`...), never the value itself. Then:

| Mechanism | What changes next time |
|---|---|
| **Confidence calibration** | Each method's confidence for a field becomes a mix of its built-in confidence (worth 4 reviews) and how often you accepted it (per document type once there are 3 reviews of it). Below `DOCFILL_MIN_CONFIDENCE` (0.5) the value is no longer filled in, only proposed. Example: a method at 97% corrected 4 times in a row drops to 49% and stops filling that field; every acceptance raises it again. |
| **Spelling fixes** | For place-like fields (city, place of birth, county, street, issued by...) a correction that only adds accents or changes punctuation ("Com. Sacalaz" → "Com. Săcălaz") is applied automatically next time. |
| **Learned labels** | A value you type that was printed after an unknown label teaches that label. |
| **Document types** | Every saved document (with its type, corrected in *Scan & clean* if needed) is a training example for the type classifier. |
| **Your details** | Fields a form marks `remember` (represented by, contact person, billing...) are proposed again. |

What it does **not** do:

* It does not retrain the OCR (Tesseract) or the spaCy model. A bad scan is not read better next
  time; docfill only trusts less the methods that were often wrong.
* Trust is kept per field and method, not per scan: docfill cannot tell a glare photo from a
  clean scan. Corrections on bad scans also lower the trust for clean ones, and acceptances on
  clean scans raise it again.
* It never learns personal values (names, CNP...). Diacritics missing in a name have to be
  corrected every time.

## 4. Scenarios

Unless a scenario names a procedure, tick **ONRC Anexa 4** under *Reference documents*, upload
the file and compare the fields with `ANSWERS.md`. After **Save PDF**, the message *Learned from
this review* says what docfill took from it. What you should see is what these files gave on a
fresh database.

1. **Baseline.** `ci_popescu_clean.jpg`: every value matches the answers. Save: nothing was
   corrected.
2. **A spelling fix.** `ci_stefanescu_clean.jpg`: the city is "Com. Sacalaz". Correct it to
   "Com. Săcălaz" and save: *Spelling fixes remembered: 1*. Then
   `ci_stefanescu_faded.jpg`: the city is now "Com. Săcălaz" straight away.
3. **Bad scans.** The `glare`, `photo` and `lowres` cards give cut values ("Ion-Andre",
   "Mun. Sib") and missing ones (place of birth, city, issued by). Correct them and save each
   time; in `docfill learn stats` the accuracy of the method behind that field drops. Once a
   method is corrected more often than accepted for a field, its values stop being filled in
   automatically and wait for you under **Needs attention**.
4. **A new label.** Upload `fisa_popescu.docx` on its own: the place of birth is empty. Type
   "Sibiu", save: *New labels: localitatea natala → place_of_birth*. Then
   `fisa_dumitru.docx`: the place of birth ("Cluj-Napoca") is found, method *learned*.
5. **Document type.** In *Scan & clean*, change the detected type of a document if it is wrong and
   save: the classifier gets an example of that type (`docfill learn stats`, document
   examples per type). To teach several at once: `docfill knowledge teach TYPE files...`.
6. **Your details.** Fill **Anexa 2a** from any card, type *Represented by (prin)* and save. The
   next Anexa 2a proposes it (method *memory*).
7. **Legal checks.** Under *What are you doing?* choose **PFA** and **înființare**, upload
   `ci_dumitru_clean.jpg`: the check
   "Titularul are cel puțin 18 ani" fails and saving is refused until you acknowledge it. The
   override is recorded: `docfill knowledge stats` shows it per rule, and rules overridden often
   are flagged for review.
8. **Expired card.** `ci_muresan_clean.jpg`: the expiry date is flagged "The identity card has
   expired".

## 5. Watch what it learned

* **In the wizard:** the message *Learned from this review* after each save.
* **Command line:** `docfill learn stats` (`--json` for everything): reviews, accuracy of
  proposed values overall and over the last 10 reviews, accuracy per field and method, learned
  labels, spelling fixes, document examples, remembered fields. API: `GET /learning/stats`.
* **Database (Adminer or `docker compose exec db psql -U docfill`):** `review_outcomes` (one row
  per field per saved review: outcome and method, no values), `learned_labels`,
  `spelling_fixes`, `doc_examples` (texts with the reviewed values and every digit removed),
  `remembered_values`, `knowledge_rule_outcomes` (legal checks per saved file).
* **Measure errors:** save a fully corrected Anexa 4 once and use it as the reference for
  another scan of the same person, e.g.
  `docfill evaluate -t onrc-anexa-4 --expected output/ci_popescu_clean.pdf examples/samples/learning/ci_popescu_glare.jpg`
  (on a fresh database: clean and faded 24/24 values match, glare 22/24, lowres 21/24, photo
  20/24). Run it again after more reviews to see the difference.
* **Start over:** `docfill learn reset --yes` forgets what was learned (standard documents and
  legal knowledge stay).

## 6. Known issues

Found while writing this guide; worth confirming, and reporting if you see others:

* "Alba Iulia" is read as "Alba lulia" (I read as l), even on the clean card: the city becomes
  "Mun. Alba" and *issued by* "SPCLEP Alba lulia".
* Diacritics in names are lost ("Ștefănescu" → "Stefanescu"). Correcting them does not stop it:
  each correction lowers the trust of one method, but another one (the MRZ, then a learned label)
  proposes the same value, which is still filled in after 5 corrections.
* Correcting a name can "learn" the card's own label (`nume/nom/last name → last_name`).
* Values docfill filled in itself (form defaults, remembered details, today's date) count as
  *added* by you when saved: this inflates *Added* in the stats and can teach wrong labels (e.g.
  `roumanie → country`, from the card's header, when the country came from the form's default).
* The evidence of a value repeats "(confirmed by the MRZ)" several times.
