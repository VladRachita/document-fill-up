"""docfill - scan documents, extract personal data and fill standard documents as PDF.

The processing pipeline is::

    read (PDF / DOCX / PNG / JPEG)  ->  sanitize  ->  extract (rules + ML NER)
        ->  fill a stored standard document  ->  export PDF
"""

__version__ = "0.1.0"
