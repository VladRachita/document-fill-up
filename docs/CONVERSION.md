# Converting documents: Word ⇄ PDF, with a fidelity score

docfill converts a **Word document (.docx, .doc) to PDF** and a **PDF to Word (.docx)**, apart
from creating companies and documents, and scores **how faithful the converted document is**
(0-100) against the original and, when you give it, against the **real document**. The real
document is the PDF Word saves, or the Word document a PDF was made from.

| Where | What |
|---|---|
| **http://127.0.0.1:8000/convert** | the page: drop a file, optionally the real document, convert, download; scores, text differences and every page compared (*Differences / Original / Converted*). *Compare two documents* scores a document converted by any program against the real one |
| `docfill convert FILE [-r REAL] [--min-score 95]` | the same from the command line (`--json`, `--engine`, `--to`, `-o`); exits with an error under `--min-score` |
| `docfill compare REAL CONVERTED` | score any converted document against the real one |
| `docfill convert-engines` | which engines are installed here, and what is missing |
| `POST /convert`, `POST /convert/compare`, `GET /convert/engines`, `GET /convert/files/{name}` | the API (see `/docs`) |

Converted documents are saved in `DOCFILL_OUTPUT_DIR/converted` (a file is never overwritten).

## The score

A converted document is compared on five checks; the score is their weighted mean, over the
checks that could be made.

| Check | Weight | What is measured |
|---|---|---|
| **Layout, page by page** | 50 | Both documents are drawn page by page (72 dpi). The score is the share of the ink (text, lines, pictures) found at the same place in both, within 2 points (0.7 mm), in both directions (F1). Missing ink and added ink both count. A missing or extra page counts 0. |
| **Text** | 30 | Every word, in its order: the longest common subsequence of the two texts (case, ligatures and soft hyphens ignored). The page shows the differences: *missing* and *added* words with what comes before them. |
| **Pages** | 8 | The number of pages: of the original PDF, of the real document, or the number Word saved with the Word document (used only when it is up to date: the word count saved with it matches the document). |
| **Fonts** | 8 | The fonts of the text, weighted by the characters written with each. Score: the same font 100; a font of the same widths 90 (Liberation Serif for Times New Roman, Carlito for Calibri: the lines break at the same place, the letters look a little different); another font 0 (lines may break elsewhere: install the font). |
| **Pictures** | 4 | The number of pictures kept. |

| Score | Verdict |
|---|---|
| 97-100 | **1:1**, practically identical |
| 90-97 | very close, small differences |
| 75-90 | close, visible differences |
| < 75 | different: check the document |

**What is compared with what.**

| Conversion | Against the original | Against the real document (optional) |
|---|---|---|
| Word → PDF | text (every word of the Word file in the PDF; page headers and numbers printed on every page are not held against it), pages (as Word counted them), fonts, pictures. **Not the layout**: LibreOffice made the PDF, so it would be compared with its own layout | the PDF Word saves of the same document: **everything, page by page**. This is the true 1:1 score |
| PDF → Word | everything: the Word document is laid out by LibreOffice and compared page by page with the PDF | the Word document the PDF was made from: both laid out the same way and compared |

A Word document is laid out by LibreOffice to be compared. When a font it uses is not installed
on the server, the comparison says so: the layout score is then lower than it would be in Word.

## Measured on the reference documents

Measured on the documents in `src/docfill/standard_documents/` (LibreOffice 24.2, pdf2docx
0.5.13, Liberation, Carlito and Caladea fonts installed, no Microsoft fonts), against the
original. Times: the conversion / the conversion and its score.

| Document | Engine | Score | Layout | Text | Pages | Fonts | Time |
|---|---|---|---|---|---|---|---|
| Act constitutiv SRL, .docx, 8 pages → PDF | libreoffice | **98.3** | – | 100.0 | 100.0 | 90.0 | 1.3 s / 1.4 s |
| Act constitutiv SA, .doc → PDF | libreoffice | **78.9** | – | 100.0 | – | 0.0 | 1.3 s / 2.7 s |
| Act constitutiv SRL, PDF → Word | **pdf2docx** | **95.5** | 91.4 | 100.0 | 100.0 | 100.0 | 1.1 s / 3.1 s |
| Act constitutiv SRL, PDF → Word | libreoffice | **60.0** | 23.2 | 100.0 | 100.0 | 100.0 | 1.5 s / 5.0 s |
| ONRC Anexa 4 (form), PDF → Word | **pdf2docx** | **65.9** | 48.2 | 90.6 | 50.0 | 100.0 | 2.6 s / 4.2 s |
| ONRC Anexa 4 (form), PDF → Word | libreoffice | **49.5** | 35.7 | 45.7 | 100.0 | 100.0 | 1.8 s / 46.2 s |
| Beneficiari reali (form), PDF → Word | **pdf2docx** | **73.5** | 56.5 | 87.9 | 100.0 | 100.0 | 2.1 s / 3.9 s |
| Beneficiari reali (form), PDF → Word | libreoffice | **74.0** | 50.4 | 99.4 | 100.0 | 100.0 | 2.0 s / 28.2 s |

What the scores show:

* **Word → PDF** of a text document keeps every word and **the 8 pages Word counted**. The only
  difference is the font: Times New Roman is drawn with Liberation Serif, which has the same
  widths. Install the Microsoft fonts (`ttf-mscorefonts-installer`) to get 100. The SA model is
  written in **Arial Narrow**, which is not installed: install it, or Liberation Sans Narrow
  (`fonts-liberation`, same widths).
* **PDF → Word with pdf2docx** gives an editable document that is very close for text documents:
  on the SRL act, 5 of the 8 pages are practically identical. Where a line wraps differently, the
  rest of the page moves down a line (pages 3, 4 and 6: 80-84%).
* **Official forms** (boxes, columns, ticks) do not convert well to Word with any engine:
  Anexa 4 gets a third page from pdf2docx. Keep such forms as PDFs. docfill fills them as PDFs.
* **LibreOffice's PDF import** keeps every word but places each line in a frame of its own,
  **shifted by about 1 cm** (the page margins), so its layout score is low, and it is slow to lay
  out again. It is the fallback when pdf2docx is not installed.

## Libraries explored

### Word → PDF

| Library / tool | How | 1:1 with Word | Runs | Cost, licence | Verdict |
|---|---|---|---|---|---|
| **LibreOffice** (`soffice --headless --convert-to pdf`) | its own layout engine | very close for usual documents **when the fonts (or metric-compatible ones) are installed**; can differ on floating shapes, text boxes, SmartArt, complex tables | Linux, macOS, Windows, Docker | free, MPL-2.0 | **used**: the only engine of good fidelity that runs on the server, offline |
| Microsoft Word (automated, e.g. the `docx2pdf` package) | Word itself saves the PDF | **exact**: it *is* the reference | Windows / macOS desktop with Word | Word licence; Microsoft does not support unattended Office automation on servers | use it to make the **real document** to score against (File › Save As › PDF) |
| Microsoft Graph (`…/content?format=pdf`) | Word for the web renders it | near exact | cloud | Microsoft 365 | the documents (identity data) leave the machine |
| Gotenberg | LibreOffice behind an HTTP API, in Docker | = LibreOffice | Docker | free, MIT | an option to run conversions as a separate service |
| unoserver | keeps LibreOffice running | = LibreOffice, about 1 s faster per file | Linux, macOS, Windows | free, MIT | next step if there are many conversions |
| Aspose.Words | its own engine, closest to Word without Word | very close | anywhere, offline | commercial | an option if LibreOffice is not close enough |
| pandoc; mammoth + WeasyPrint | through LaTeX / HTML | **no**: they keep the content, not the layout | anywhere | free | not for 1:1 |
| CloudConvert, ConvertAPI, Adobe PDF Services | cloud APIs | very close (Word or LibreOffice behind) | cloud | paid per file | the documents leave the machine |

### PDF → Word

| Library / tool | How | Result | Runs | Cost, licence | Verdict |
|---|---|---|---|---|---|
| **pdf2docx** | rebuilds paragraphs, tables (from ruling lines), pictures and page margins from the PDF (PyMuPDF) | **editable** Word, very close for text documents (95.5 above), weaker on forms | anywhere, offline | free: pdf2docx is MIT, **PyMuPDF is AGPL-3.0** (or a commercial licence from Artifex) | **used** (`pip install "docfill[convert]"`); see *Licence* below |
| **LibreOffice** PDF import (`--infilter=writer_pdf_import`) | every line in a positioned frame | all the text, lines shifted by the margins, hard to edit | anywhere, offline | free, MPL-2.0 | **fallback** engine |
| Microsoft Word (opens a PDF: *PDF Reflow*) | Word rebuilds the document | good, editable | Windows / macOS desktop | Word licence | a real document of reference; not on the server |
| Adobe Acrobat / PDF Services API (Export PDF) | Adobe's engine | among the best, editable | desktop / cloud | commercial | the documents leave the machine (API) |
| Aspose.PDF | *flow* or *text box* modes | very close | anywhere, offline | commercial | an option for better forms |
| ABBYY FineReader | OCR and layout analysis | best for scans | desktop / server | commercial | for scanned documents |
| ocrmypdf (Tesseract) + pdf2docx | adds a text layer to a scan first | editable text from a scan | anywhere | free (MPL-2.0 + pdf2docx) | next step for scans |
| Docling, Marker | machine learning to Markdown / HTML / JSON | the structure, not the layout | anywhere | free | not for 1:1 |

### Measuring fidelity

| Option | Why it was or was not used |
|---|---|
| a pixel difference, SSIM (scikit-image), ImageMagick `compare`, diff-pdf | a page is mostly white paper: comparing every pixel gives very different pages a high similarity, and a shift of one pixel from anti-aliasing counts as a difference |
| **ink overlap with a 2 pt tolerance** (numpy + Pillow) | **used**: only the ink is compared, both ways (missing and added), tolerant of anti-aliasing, and a moved line shows as red and blue on the page |
| `difflib` (standard library) | 12 s to align two texts of 40,000 words |
| **rapidfuzz** (Indel distance, LCS) | **used**: the same alignment in 0.2 s, with the differences to show (MIT) |
| **pdfium** (pypdfium2) | **used**: draws the pages, gives the text and the font of every character |
| python-docx / the Word XML | **used** (lxml): the text of a Word document with its text boxes, headers and footers, and the font of every character as Word picks it: run, character style, paragraph style, document default, theme fonts |

## Installing

| Need | Install |
|---|---|
| Word → PDF, and laying Word documents out to compare them | LibreOffice Writer: `apt install libreoffice-writer-nogui` (macOS: `brew install --cask libreoffice`). The core package alone cannot open Word documents; docfill says so |
| Fonts of the same widths as Word's | `apt install fonts-liberation fonts-crosextra-carlito fonts-crosextra-caladea` (Times New Roman, Arial, Courier New, Arial Narrow; Calibri; Cambria) |
| The Microsoft fonts themselves, for a fonts score of 100 | `apt install ttf-mscorefonts-installer` (accept its licence), or copy the fonts of the documents to `/usr/share/fonts` |
| PDF → Word that can be edited | `pip install -e ".[convert]"` (pdf2docx) |

The Docker image and CI install all of these except the Microsoft fonts.

`DOCFILL_CONVERT_TIMEOUT` (300 s) limits one conversion. Every engine runs in its own process: a
damaged file cannot hang or crash docfill.

## Licence

docfill is MIT. LibreOffice (MPL-2.0) is a separate program it runs. **PyMuPDF**, which pdf2docx
needs, is **AGPL-3.0** unless a commercial licence is bought from Artifex. The AGPL asks that
whoever runs a modified version as a network service offers its source code to the users of that
service. pdf2docx is therefore an optional extra (`[convert]`): without it, PDF → Word uses
LibreOffice. Check with your legal adviser before offering the converter outside the firm.

## Limitations and next steps

* Without Microsoft Word on the server, a Word document converted to PDF is not compared page by
  page against the original. Give the real PDF Word saves to get that score.
* Scanned PDFs (pictures only) become Word documents of pictures. Next step: OCR first
  (ocrmypdf + Tesseract, both already used by docfill for reading).
* Forms with many boxes stay best as PDFs. To edit one in Word, check its score and its pages.
* LibreOffice starts for every conversion (about 1 s). Next step for heavy use: unoserver.
* LibreOffice opens untrusted documents: macros are not run when converting, but keep docfill on
  a private network as the README says (the API has no authentication).
