# document-fill-up (`docfill`)

A Python machine-learning project that **reads documents** (PDF, Word, PNG, JPEG), **cleans**
them, **detects what kind of document** each one is, **extracts the data** a standard document
needs and **fills the standard documents stored in a database**, exporting **new PDFs**. Every
document you check in the wizard **teaches it**, so errors decrease over time.

It is built for **Romanian registration work**: opening, changing and closing companies
(**SRL, SRL-D, SA**) and natural persons with an economic activity (**PFA, II, IF** at the trade
register, **PFI** - persoană fizică independentă - at ANAF). A **legal knowledge base**
knows each procedure (forms, documents of the file, legal checks with the article they come
from) and can be **fed and corrected over time**: laws, rules, procedures and document types,
each versioned and verified by a named person (see [Legal knowledge](#legal-knowledge-romania)).

The source is typically the **Romanian identity card** (CI, or the electronic CEI) of each person
involved (a birth certificate, an act constitutiv, a sworn statement or an already filled form
can be added); the results are the files of the dossier, each saved as its own PDF (the act
constitutiv of an SRL as a Word document, to edit before it is signed):

| Result | What it is |
|---|---|
| **ONRC Anexa 2a** | cerere de înregistrare (înmatriculare, înscriere mențiuni, radiere) - official PDF form |
| **ONRC Anexa 4** | declarație privind îndeplinirea condițiilor de funcționare - official PDF form |
| **Anexa 1 - cerere de înregistrare fiscală** | the fiscal vector (Ordinul nr. 2.509/5.672/C/2022) - official PDF form |
| **ONRC Formular nr. 3** | declarație privind beneficiarii reali (Legea nr. 129/2019) - official PDF form |
| **Declarație pe proprie răspundere - administrator** | one for each administrator, in the wording filers submit |
| **Act constitutiv SA (sistem unitar)** | the ONRC model, filled: founders, capital and shares, board, director, censors |
| **Act constitutiv SRL - asociat unic** | the model filers use, filled: the firm (proof of the name), the registered office (comodat), CAEN activities, capital, the sole associate, the administrator, the beneficial owner - a **Word document (.docx)** laid out like the model |

These are the **[reference documents](#reference-documents)**: kept exactly as they were given and
checked by the tests after every change.

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
| Reading | `pypdf`, `pypdfium2`, `python-docx`, `Pillow`, `pytesseract` + Tesseract OCR, `antiword` / LibreOffice (legacy `.doc`) |
| Cleaning | `ftfy`, regular expressions, Luhn / IBAN mod-97 checks |
| Machine learning | `scikit-learn` (document type classifier, TF-IDF search of laws), `spaCy` NER, `pycountry` |
| Legal knowledge | versioned YAML entries (legal forms, procedures, declarative rules, document types), fed laws split into articles |
| Validation | CNP control digit, ICAO 9303 MRZ check digits, date/IBAN/e-mail checks |
| Standard documents | `SQLAlchemy` 2 (SQLite by default, PostgreSQL via URL), `PyYAML`, `pydantic` |
| PDF export | `pypdf` (AcroForm filling) + `ReportLab` (Unicode appearances, text documents) |
| Interfaces | Web wizard (FastAPI + one self-contained HTML page), `Typer` CLI, REST API |

## Installation

System packages: **Tesseract OCR with Romanian**, a TrueType font with full Unicode coverage
(DejaVu is auto-detected) so names like *Ștefănescu* or *Țară* render, and **antiword** (or
LibreOffice) to read legacy Word `.doc` files.

```bash
# Debian / Ubuntu
sudo apt install tesseract-ocr tesseract-ocr-ron tesseract-ocr-eng fonts-dejavu-core antiword
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

For local development and testing, `docker compose up --build -d` starts docfill with
PostgreSQL (and, with `--profile tools`, Adminer to browse the database).
**[docs/LOCAL_TESTING.md](docs/LOCAL_TESTING.md)** describes the setup, test documents with known
answers (good and bad scans) and scenarios to watch docfill learn from your corrections.

## The wizard

```bash
docfill serve                 # open http://127.0.0.1:8000
```

1. **Documents.** Three short choices:
   * **What you are doing** (optional): the legal form (SRL, SRL-D, SA, PFA, PFI, II, IF) and
     the operation (înființare, modificare, radiere). The procedure's legal checks and the
     **documents of the file** (dosar) are one click away, with the knowledge not verified yet.
     The operation ticks its request on every chosen form that has the box (*înmatriculare*,
     *modificare*, *radiere*: Anexa 2a, the beneficial owner declaration); it can be unticked
     in the review.
   * **Which documents you need**: the documents of that procedure as cards, the required ones
     ticked. Tick one, a few or all (*All*, *Required only*, *None*); forms docfill does not
     have yet are shown with their official link, any other standard document can be added.
     Without a procedure, pick any documents. The next steps ask only for what the ticked
     documents need, and only their legal checks run.
   * **What to upload**, which follows the documents ticked: the applicant's identity card,
     and the identity card of every other person involved when a ticked document needs them
     (associates, administrators, beneficial owners), an already filled form or a document
     identifying a person (PDF, Word, photo or scan). For a company, the **act constitutiv**
     gives the company and its persons (see *The act constitutiv* in
     [Romanian documents](#romanian-documents)). When a lawyer or a proxy files the request,
     put **their identity card** in the separate **Representative** box and say whether they
     are a lawyer (împuternicire avocațială) or a proxy (procură specială / generală
     autentică): their data goes to "Filed by" and the contact person, never to a person of the
     company, with or without an act constitutiv. For the **act constitutiv of an SRL**, the
     box *Sole associate and administrator* takes the sole associate's identity card and,
     unless *the sole associate is also the administrator* stays ticked, the administrator's
     (the administrator is then person 1, who signs the requests). *Fill in by hand* is also
     possible.
2. **Scan & clean.** For each file: the **detected document type** with its confidence, or
   *recognised from its title* (change it if wrong; the classifier learns from it), **whose
   document it is** (person 1, 2 or 3: each identity card is a person, documents with the same
   CNP go to the same person; or the **representative** filing the request, whose card fills
   "Filed by" and the contact person; an act constitutiv names its persons itself), the text read
   from the file next to the
   cleaned text (**fix OCR mistakes there, fields are re-extracted as you type**), what was
   removed or redacted.
3. **Review fields.** Fields are grouped (Person 1 - the applicant, Person 2, Person 3,
   Shareholders / management / beneficial owners, Company, Articles of incorporation, Tax
   registration, Request, Contact person, Billing, Filed by). **Needs attention** shows only what has to be
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
   file name; one PDF per reference document (one per administrator for the statements) is saved
   in the output folder
   (`DOCFILL_OUTPUT_DIR`, never overwritten) and downloaded. The page shows what docfill learned
   from the review, including the checks you overrode (recorded so rules get reviewed).
   **Start a new document** clears the page for the next one: the documents just created are
   unticked, the files, values and previews are gone. What was saved stays: the PDFs in the
   output folder, and what docfill learned and remembers (in the database). Only the values of
   the documents ticked are sent when previewing or saving, so nothing typed for a previous
   document reaches the next one.

The same review runs in the terminal: `docfill wizard ci.jpg -t onrc-anexa-2a -t onrc-anexa-4`
(procedures and legal checks are in the web wizard; in the terminal use
`docfill knowledge check PROCEDURE -s FIELD=VALUE`).

## Romanian documents

**Identity card (CI).** Multilingual labels with the value underneath (`Nume/Nom/Last name`),
`SERIA AX NR 123456`, the CNP, the validity range `22.06.22-14.11.2032`, the domicile in card
style (`Jud.CJ Mun.Cluj-Napoca` / `Str.Florilor nr.5 bl.A2 sc.1 et.3 ap.10`, split into
street, number, block, staircase, floor, apartment, locality, county) and the **machine
readable zone** (`IDROU...`, TD1/TD2/TD3). MRZ values are only trusted when their check digit
matches; OCR confusions (O/0, I/1, B/8, `LF` for `IF`) are repaired when that makes the check
digit match. The second line of the zone is read on its own when a tilted photograph splits the
first, and a lost or extra character in it is put back when exactly one reading matches. The
printed names are confirmed against the MRZ.

**Electronic identity card (CEI).** The PDF the **RO CEI Reader** application exports from the
card is recognised by its footer (or by the labels only it prints) and read label by label:
`Nume de familie`, `Prenume`, `Cetățenie: ROU` (*Română*), `Sex`, `CNP`, `Data nașterii`,
`Locul nașterii: Jud.BV Mun.Brașov` (place and county of birth), `Număr document: BV1234567`
(series `BV`, number `1234567`), `Data emiterii`, `Data expirării`, `Autoritatea emitentă`
(printed over two lines) and `Domiciliu` (split like the card's). The document is written as
`CEI` (*identificat prin CEI, seria BV, nr. 1234567*). The export's text layer runs some words
together for one PDF reader (`Str.Exemplelornr.7Bl.B3`): the reader that keeps the blanks is
used, and the parts of an address are separated again if they still touch. The cedilla letters
of old encodings (`Braşov`) are written with the Romanian comma below (`Brașov`), on every
identity card.

**CNP.** It is read twice: as printed, and rebuilt from the MRZ (on the card with two lines its
optional field holds the CNP without the date of birth, which the zone gives). The control digit
is verified; letters OCR took for digits (`l` for 1, `O` for 0) are put back when that makes the
control digit match. A CNP that fails is not filled automatically but offered for review, so one
wrong digit is quicker to fix than thirteen to type; when the zone has a valid one, that is used.
The two readings confirm each other, and when they disagree neither is filled. A valid CNP gives
the date of birth, sex and issuing county, and cross-checks them. (A CNP that happens to look
like a payment card number is never redacted.)

**Misread card text.** The card prints names in capitals and places, streets and the issuing
office in capitalised words, so docfill reads `lON`, `ANDREl`, `Alba lulia`, `luliu Maniu`
(capital I read as l, 1, a bar or j), `jud.AB` and a county code such as `C}` (the hook of the J)
as what they can only be. Bucharest prints the sector (`Mun.București Sec.2`), which stays in the
place of birth; towns are written `Or.Huedin`. A printed name that is cut off (glare, a fold) is
finished from the MRZ. A value that cannot be what the field holds is **shown for review but
never filled in**: symbols or a piece of the zone read as text, a neighbouring label read as the
value, a place that the register does not have (see below).

**Romanian names and places.** docfill knows which names exist in Romania, the way a Romanian
clerk reading the card does (`src/docfill/data/`, no network needed):

* *Given names and surnames* (about 1,100 each: traditional, saints', Biblical, recent, and the
  Hungarian ones of Transylvania). The diacritics OCR loses are restored: `STEFANESCU` becomes
  `ȘTEFĂNESCU`, `LACRAMIOARA` `LĂCRĂMIOARA`, `Cr1stian` `Cristian` (a digit inside a name is the
  letter it looks like). Only the Romanian diacritics (ă â î ș ț) are added, never one that was
  read is taken away, and a Hungarian name is left as it was read. A name that is not listed is
  never changed.
* *A first name that is not a Romanian name* but is a look-alike letter away from one
  (`JOANA`, `IOANA`; `J`/`I`, `O`/`Q`) is **corrected** — with a note, and what was read stays
  one click away. The machine readable zone has the last word: if it says `JOANA` too, the person
  is called that; if it says `IOANA` the name is confirmed; if both words are names (`JULIA`,
  `IULIA`) docfill does not decide. A surname, a lost letter (`ONUT` for `IONUȚ`) or a name cut off
  by glare (`DUMITRES`) is **offered, not filled in**. A first name that does not fit the sex of
  the CNP (`IOANA` on a card of a man) is flagged.
* *The register of localities* (SIRUTA: 103 municipalities, 216 towns, 2,862 communes and
  13,000 villages of the 41 counties and Bucharest). The locality of a domicile or of a place of
  birth is looked up in the **county the card prints** (`Jud.BV`): `Com.Harman` becomes
  `Com. Hărman`, `Mun.Cluj Napoca` `Mun. Cluj-Napoca`. A name that the county does not have is
  shown for review, with the names it may be — cut off by glare (`Mun. Sib` → Sibiu, `Oraș Huedi`
  → Huedin), one letter misread (`Hucdin`), a locality that exists in another county
  (`Mun. Cluj-Napoca` with `Jud.AB`: the county or the name was misread) — and when only one
  fits, it is the value to check. The place in the name of the issuing office
  (`SPCLEP Drobeta-Tumu Severin`) is checked the same way.
* *Street words* with diacritics (`Libertății`, `Păcii`, `Școlii`, `Vodă`...): `Str.Stefan cel Mare`
  becomes `Str. Ștefan cel Mare`.

The lists are plain text next to the code, so a missing name is one line to add; the register is
generated from SIRUTA by `tools/build_localities.py` (data licence in the file).

**Birth certificate.** Detected and read (labels, parents, place of birth). Note: handwritten
values (old certificates) cannot be read by Tesseract; the wizard asks for them.

**Place names.** Diacritics lost by OCR are restored from the register of localities and the
lists of names (above); anything docfill does not know is learned from your corrections.

**Filled forms as a source.** A filled Anexa 2a (or any registered PDF form) is recognised from
its form fields and read back through its field map, so a finished Anexa 2a fills Anexa 4.

**Identification clause.** Declarations, powers of attorney, AGA decisions and acts identify a
person in one sentence: `POPESCU ION, CNP …, cu domiciliul în Mun. Cluj-Napoca, Str. Florilor
nr. 5, bl. A2, jud. Cluj, țara România, cetățenia Română, născut în Mun. Sibiu, jud. Sibiu, la
data de …, identificat prin CI, seria AX, nr. …, emisă de …, la data de …, valabilă până la data
de …, în calitate de administrator al societății … S.R.L.` docfill cuts it at its keywords and reads every piece (the
act constitutiv wording `D-nul/d-na …, născut(ă) la data de …, domiciliat(ă) în …, posesor(e) al
CI seria … nr. …` too). Names in capitals are family name first, as Romanian documents print
them (`Nume și prenume: POPESCU ION` is *Popescu* / *Ion*).

**The act constitutiv.** An act constitutiv (Word or PDF, recognised from its title) is read
as a whole, not as one person's document:

| What the act says | Fields |
|---|---|
| `Denumirea societății este: EXEMPLU — societate cu răspundere limitată/S.R.L` (or the title `ACT CONSTITUTIV al Societății EXEMPLU S.R.L.`) | `company_name` = `EXEMPLU S.R.L.` (the legal form is added when the name is written without it) |
| `Sediul societății este [în / :] Mun. Timișoara, Ale. Teilor nr. 4, bl. 12, et. VII, ap. 31, camera 1, jud. Timiș.` | the registered office and its parts (`camera 1` stays with the street: the form has no box for it), the trade register office (Timiș) |
| `… dovezii privind disponibilitatea firmei nr. 123456 din 01.09.2026` | the number and date of the name availability proof |
| `— activitatea principală clasa CAEN 4711 și denumirea activității …`, `— clasa CAEN 4725 și denumirea activității …` (or `4711 - …` in the object of activity) | `caen_activities`, one per line, the main activity first (Anexa 4, 3.1) |
| `capitalul social subscris al societății este de [:] 500 lei … 50 de părți sociale` | `share_capital`, `share_count` |
| every identification clause, after `Asociat unic:` / `Asociați:`, `Capitalul social este deținut de către…`, `Administrarea societății se face de către:`, `beneficiarul real al societății este:` | the persons, each with their roles: associate (shares from `deține 60 de părți sociale` or `100%`), administrator (`pe perioadă de 30 de ani`), beneficial owner (`art. 4 alin. (2) lit. a) pct. 1`, the description in brackets); the list of associates |

The same CNP is the same person in every article. **Person 1, who signs the forms (section IV
of Anexa 2a, V.1 of Anexa 4), is the administrator** (an associate one first), then the other
administrators and the associates; their capacity is written from their roles (`asociat unic și
administrator`). An identity card with the CNP of a person of the act goes to that person; an
identity card of **nobody the act names is the representative** filing the request (avocat /
împuternicit), unless it was put in the wizard's Representative box (always the representative):
it fills "Filed by" (XII) and the contact person (VII), never a person. Whether
the representative is **a lawyer or a proxy is asked every time** (never remembered): it writes
`prin avocat, conform împuternicirii avocațiale` (or `prin împuternicit, conform procurii
speciale / generale autentice`) in section IV and the capacity in XII.

The **proof of the firm name** (ONRC, Formular nr. 17) gives the reserved name and its number /
date, and the **proof of the registered office** (comodat, lease) the address of the premises;
the people in them (the trade register's letterhead, the owner lending the premises) are not
persons of the request, so they never fill the applicant.

**Where Anexa 4 writes the activities.** The activities of the company (`caen_activities`, read
from the act) go to **3.2. Activități desfășurate la terți**, the main one first, followed by any
other activity at third parties (`caen_third_party`): a registered office without activity
(`sediu social fără desfășurare de activitate`) is the usual case. Tick *The activities are
carried out at the registered office* in the review to list them under 3.1 (sediu social /
profesional) instead.

**The act constitutiv of an SRL (asociat unic)** is written from the model filers use, every
paragraph without blanks copied verbatim:

| In the act | Comes from |
|---|---|
| the firm, `conform dovezii privind disponibilitatea firmei nr. … din …` | the proof of the firm name (ONRC, Formular nr. 17) |
| `Art. 1.4. — Sediul societății este în …` | the proof of the registered office (contract de comodat / închiriere): the premises `situat în …`; `Ale. lancu`, `BI. 12`, `Et. Vil`, `Timisoara` as OCR reads a scan are written `Ale. Iancu`, `bl. 12`, `et. VII`, `Timișoara`. OCR may also misread the words around the address (`imobllul sltuat Tn`), or the title (the contract is then recognised by its parties, *comodant* and *comodatar*): the first address after the premises is taken, at a lower confidence |
| `Art. 2.1.` the object of activity, the main domain (group and class), the main and the secondary activities | the CAEN activities typed in the review, the main one first; the object is proposed from the main activity and can be written otherwise |
| `Art. 3.1.` the share capital, the number of părți sociale (`50 de părți sociale`) and their nominal value (computed) | typed in the review |
| `Asociat unic:`, `Art. 3.2.` and `Art. 10.` (the beneficial owner, 100%) | the sole associate's identity card |
| `Art. 6.1.` the administrator | the same person, or the administrator's identity card |
| `Art. 12.1.` and `Data:` | the date of the documents (today, editable) |

The act is created as a **Word document** (`.docx`), so it can still be edited before it is
signed; every other document is a PDF. It is laid out like the model: Times New Roman 12,
justified paragraphs, the title and the chapters centred in bold, the number of each article in
bold, the firm, the object of activity, the associate and the administrator in bold, the date
and the signature at the end.

## Several persons

Opening a company involves several people: the applicant who signs the forms, the shareholders,
the administrators, the beneficial owners. docfill keeps **three persons**:

| Person | Fields | Filled from |
|---|---|---|
| 1 - the applicant | `last_name`, `cnp`, `city`... | the administrator of the act constitutiv, or the first identity card |
| 2 | `p2_last_name`, `p2_cnp`, `p2_city`... | the next person of the act, or the second identity card |
| 3 | `p3_last_name`, `p3_cnp`, `p3_city`... | the third person of the act, or the third identity card |
| the representative | `filer_*`, `contact_*` | the identity card of a lawyer / proxy the act constitutiv does not name |

A document with the CNP of a person already read goes to that person; the person of every
document can be changed in the wizard. Every person's CNP, date of birth and identity card are
checked, and derived values (date of birth and sex from the CNP...) are derived for each. The
**roles** of each person decide where they appear:

| Role (field) | Effect |
|---|---|
| `associate` | an associate / shareholder (ticked for whoever holds shares); a sole associate holds every share, and person 1's capacity is written from the roles (`asociat unic și administrator`) |
| `shares` | a founder in the act constitutiv, with the value and share of the capital computed; the list of associates is derived |
| `board_role` (președinte / membru / administrator unic / administrator) | a member of the board or the sole administrator (the administration type is derived); a sworn statement is made for them |
| `general_director` | appointed general director in the act constitutiv |
| `beneficial_owner` (how control is exercised, art. 4 alin. (2) Legea 129/2019) | a block of the beneficial owner declaration, with the matching box ticked |

Legal checks make sure that every person with a role is fully identified and that at least one
beneficial owner is declared.

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
| **Your details** | Fields marked `remember` (lawyer / filer, contact person, billing, communication, submitted documents) are proposed again. Whether the person filing is a lawyer or a proxy is asked every time. |

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
| `entity` | a legal form | SRL, SRL-D, SA (societăți), PFA, PFI, II, IF (persoane fizice) |
| `procedure` | a legal form × an operation: where it is filed (ONRC / ANAF), the official forms (which docfill fills, and where to get the others), the documents of the file, extra fields to collect, legal basis | 21: every form × înființare / modificare / radiere |
| `rule` | a legal check with its legal basis | 31, e.g. SA capital ≥ 90.000 lei and ≥ 2 shareholders (Legea 31/1990 art. 10), SRL ≤ 50 associates (art. 12), legal form in the firm name, PFA / II holder ≥ 18 years, IF ≥ 2 members, CAEN classes per PFA / II / IF, at least one change ticked (mențiuni), a reason for closing, the PFI's profession, every person with a role fully identified, at least one beneficial owner (Legea 129/2019), a tax on profit or on micro-enterprise revenue in the fiscal vector |
| `doc_type` | a document the classifier learns to recognise | act constitutiv, dovada sediului, dovada disponibilității denumirii, hotărâre AGA / decizie asociat unic, certificat de înregistrare, declarație beneficiar real, specimen de semnătură, acord de constituire IF, document privind dreptul de exercitare a profesiei, cerere de înregistrare fiscală (built in: identity card, birth certificate, Anexa 2a, Anexa 4, administrator statement) |

Which forms docfill fills:

| Procedure | Filed at | Forms filled by docfill | Official forms still to add |
|---|---|---|---|
| SRL, SRL-D înființare | ONRC | Anexa 2a (înmatriculare) + Anexa 4 + Anexa 1 (cerere de înregistrare fiscală) + declarația privind beneficiarii reali + declarațiile administratorilor | - (the act constitutiv of an SRL is a document to attach) |
| SA înființare | ONRC | the same + the act constitutiv (sistem unitar, ONRC model) | - |
| SRL, SRL-D, SA modificare | ONRC | Anexa 2a (înscriere mențiuni: section 4, 4.1 changes, 4.2 documents) + Anexa 4 and the beneficial owner declaration when needed | - |
| SRL, SRL-D, SA radiere | ONRC | Anexa 2a (radiere: section 6, reason) | - |
| PFA, II, IF înființare | ONRC | Anexa 4 | Anexa 2b (cerere de înregistrare persoane fizice) |
| PFA, II, IF modificare / radiere | ONRC | - | Anexa 2b |
| PFI înființare / modificare / radiere | ANAF | - | Formularul 070 (declarație de înregistrare fiscală / de mențiuni / de radiere) |

The forms still to add are linked from the wizard and the knowledge page (official ONRC / ANAF
downloads). To add one: `docfill forms inspect form.pdf --suggest`, write its YAML (see
`src/docfill/standard_documents/`), `docfill templates add form.yaml`, then set `template:` on
the procedure's form (`docfill knowledge show procedure/pfa.infiintare` → edit → `add`).

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
`pattern`, `lines_pattern`, `one_of`; any check can apply only `when` a field has a value (e.g.
the identity of person 2 is required when they hold shares or sit on the board). A procedure lists its forms (`template:` the docfill
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
  # one box of a field shown in several boxes: "71#1": contact_email, "71#2": contact_phone
lists:                                 # repeated rows, one per line in the wizard
  caen_activities: {rows: 18, columns: ["clasa_caen.0.{i}", "clasa_caen_desc.0.{i}"]}
choices:                               # option buttons: value -> button state
  # CheckBox90_2: {poștă: /v1, curier: /v2, mijloace electronice: /v3}
ticks:                                 # section boxes ticked when any listed field has a value
  # CheckBox15: [change_name, change_seat, change_activity]
combs:                                 # one character per box
  # micro_tax_start: {boxes: ["21", ..., "28"], format: date}          # ddmmyyyy
  # estimated_turnover: {boxes: ["61", ..., "68"], format: amount, align: right}
box_choices:                           # a choice ticking one of several check boxes
  # vat_period: {lunară: BBox23, trimestrială: BBox20}
filled_when:                           # PDF fields left empty unless the field has a value
  # p2_beneficial_owner: ["33", "31", ...]   (the block of beneficial owner 2)
defaults: {id_type: CI, country: România}
remember: [contact_phone, billing_iban]   # the filer's own details
asks: [representative_type]            # asked in the review though no box prints it
optional_fields: [building, entrance, floor, apartment]
```

**Text documents** with `{{ placeholders }}` (`# ` headings, `---` rules, filters `upper`,
`lower`, `title`, `{{ today }}`) rendered to PDF with ReportLab, or to Word with python-docx when
the document says `output: docx` (an act the filer edits before signing; `bold: [company_name,
…]` names the fields whose values the Word document writes in bold). A line can start with a
**condition** so one document covers the variants of a model: `[[p2_shares]] …` (the field has
a value), `[[general_director|p2_general_director]] …` (any of them), `[[administration=
administrator unic]] …` (this value), `[[!field]] …` (the opposite). `per_person: [board_role]`
makes one copy for each person with that role, written with the plain fields (`{{ last_name }}`).

**Computed values** are composed when a document is filled, never typed: `domicile_line`
(`Mun. Cluj-Napoca, Str. Florilor nr. 5, bl. A2, …, jud. Cluj`), `company_seat_line`,
`share_value`, `shares_value` / `shares_percent` (per person), `main_activity`,
`main_caen_group`, `secondary_activities`, and words agreeing with the person's sex
(`born_word`: *născut* / *născută*). The wizard asks for their inputs instead (see
`src/docfill/computed.py`); every one exists for persons 2 and 3 (`p2_domicile_line`...).

Adding a new official form:

```bash
docfill forms blank filled_example.pdf blank.pdf   # remove every value (safe to share)
docfill forms inspect blank.pdf --suggest          # fields, printed labels, suggested mapping
docfill templates add my-form.yaml
```

## Reference documents

The documents docfill must fill are kept in `src/docfill/standard_documents/` **exactly as they
were given** (the official PDFs are not re-saved or blanked; they carry no values):

| File | Standard document |
|---|---|
| `onrc-anexa-2a.pdf` | `onrc-anexa-2a`, `onrc-anexa-2a-mentiuni`, `onrc-anexa-2a-radiere` |
| `onrc-anexa-4.pdf` | `onrc-anexa-4` |
| `anexa-1-inregistrare-fiscala.pdf` | `cerere-inregistrare-fiscala` |
| `onrc-declaratie-beneficiari-reali.pdf` | `onrc-declaratie-beneficiari-reali` |
| `model-act-constitutiv-sa-sistem-unitar.doc` | `act-constitutiv-sa` (a text document: every paragraph of the model without blanks is copied verbatim) |
| `model-act-constitutiv-srl-asociat-unic.docx` | `act-constitutiv-srl` (the same way; the model's metadata, the names of who edited it, removed) |

`tests/test_reference_forms.py` checks, after every change, that each file is unchanged (SHA-256)
and that filling it from known input (fictitious people) gives the **expected document**: every
value in its box, the right boxes ticked, the blocks that must stay empty left empty, the act
constitutiv identical to the model where the model has no blanks, and the administrator's
statement identical, paragraph by paragraph, to the statements filers submit. A new official
version of a form is added as a new file and a new version of the standard document.

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
| GET | `/wizard/files/{name}`, `/learning/stats` | Download a saved document (PDF or Word); what was learned |
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
| `DOCFILL_DATABASE_URL` | `sqlite:///./docfill.db` | Any SQLAlchemy URL (standard documents and learning data); PostgreSQL needs `pip install -e ".[postgres]"` |
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
  readers/             PDF (text, form values, OCR of scans), DOCX, legacy DOC, images; OCR
  sanitize.py          text cleaning and redaction
  doctypes.py          document type classifier (scikit-learn), incl. types taught as knowledge
  knowledge/           legal knowledge: specs, declarative rules, versioned store, laws + search,
    bundled/           Romanian legal forms, procedures, rules and document types (YAML)
  extraction/          field catalog (persons 1-3, the representative), label rules, patterns
                       (CNP, MRZ...), the identification clause, the act constitutiv and the
                       proofs of the firm name and registered office (articles.py), NER,
                       derivations, repairs and checks of the text printed on an identity card
                       (idcard.py)
  computed.py          values composed when filling (domicile line, share value...)
  ro.py, mrz.py        Romanian knowledge (counties, CNP, addresses, places) and MRZ parsing
  lexicon.py, names.py Romanian names and localities (data/): lookups, repairs of names,
                       settling a name against the machine readable zone
  data/                given names, surnames, street words, the register of localities (text)
  validation.py        cross-checks and live validation
  learning.py          review outcomes, calibration, learned labels, memory
  templates/           standard documents: placeholders, DB model, repository, YAML loader
  standard_documents/  the reference documents (official PDFs, the ONRC act model) + YAML:
                       Anexa 2a (înmatriculare, mențiuni, radiere), Anexa 4, Anexa 1,
                       beneficial owners, administrator statement, act constitutiv SA and
                       SRL (asociat unic)
  export/              PDF forms (fill, blank, inspect) and text rendering
  pipeline.py, app.py  read -> clean -> detect -> extract -> fill; wiring with learning
  wizard.py, web/      review logic, the web wizard and the knowledge page
  samples.py           synthetic documents (fictitious Romanian identity card)
  cli.py, api.py       Typer CLI and FastAPI app
tools/                 build_localities.py: the register of localities from SIRUTA
tests/                 pytest suite (fictitious data only); test_reference_forms.py checks
                       every reference document
```

## Development

Local stack, test documents and learning scenarios: [docs/LOCAL_TESTING.md](docs/LOCAL_TESTING.md).

```bash
pytest                                  # OCR / NER tests are skipped if Tesseract / the model are missing
ruff check src tests examples && ruff format --check src tests examples
```

## Limitations and next steps

* **Forms not in docfill yet.** docfill fills the ONRC Anexa 2a (înmatriculare, înscriere
  mențiuni, radiere), Anexa 4, Anexa 1 (cerere de înregistrare fiscală), the beneficial owner
  declaration, the administrators' statements and the act constitutiv of an SA. **Anexa 2b** (PFA / II / IF) and **ANAF form 070** (PFI) are
  listed with their official download link but not bundled yet (see the table in
  [Legal knowledge](#legal-knowledge-romania)); until they are added, those procedures cannot
  produce the request PDF.
* **The bundled legal knowledge is draft** and must be verified by a legal professional against
  the law in force; it has no expert opinion beyond the rules written in it. docfill does not
  use a large language model: it checks values against the rules and searches the laws you feed
  it, locally.

* Handwriting (old birth certificates) is not readable by Tesseract; a handwriting OCR model
  (or a cloud OCR service, if sending the data out is acceptable) would be needed.
* The identity card reader is tested on synthetic cards built from the official layout (ten
  fictitious people, each as a clean card and as tilted, small, faded, glared and mid-size scans:
  every value of a clean, faded or mid-size scan is read correctly, and a wrong value is almost
  never filled in; on a bad scan the values it cannot read are missing or proposed, not wrong).
  Real phone photos (glare, angle) may need better image straightening, and a card scanned
  small (under about 1000 px wide) loses fields - try yours and correct in the wizard, the
  corrections are learned. Text hidden by glare cannot be known: a place that is cut off is only
  caught when it is the beginning of a name of the register (and proposed, not filled in).
* The lists of names are not exhaustive. A rare surname or first name that is not listed keeps
  the diacritics OCR lost (the wizard shows it; correct it once), and is never "repaired". The
  corrections made on cards were measured on synthetic cards only.
* CAEN activity names are read from the act constitutiv (or a filled Anexa 4) or typed; a CAEN
  Rev. 3 list could fill the name from the code.
* The list of submitted documents (Anexa 2a, IX) is typed (and remembered), not built from the
  uploaded files yet.
* Three persons at most (the three blocks of the beneficial owner declaration); founders that
  are companies, an SA administered in the dualist system (directorat + consiliu de
  supraveghere) and a capital partly paid at registration are not in the act constitutiv
  template: write those acts separately.
* docfill writes the act constitutiv of an SA (the ONRC model given as reference) and of an SRL
  with a sole associate (the model filers use); an SRL with several associates, or an SRL-D,
  has its act written separately.
