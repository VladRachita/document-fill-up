FROM python:3.11-slim

# Tesseract OCR (English + Romanian) and a unicode TrueType font for the exported PDFs.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng tesseract-ocr-ron fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && python -m spacy download en_core_web_sm

RUN useradd --create-home docfill && mkdir /data && chown docfill /data
USER docfill

ENV DOCFILL_DATABASE_URL=sqlite:////data/docfill.db \
    DOCFILL_OUTPUT_DIR=/data/output \
    DOCFILL_OCR_LANGUAGES=eng+ron
VOLUME /data
EXPOSE 8000

# Seeding is idempotent: unchanged standard documents are left as they are, and knowledge you
# changed or retired is never overwritten.
CMD ["sh", "-c", "docfill templates seed && docfill knowledge seed && exec docfill serve --host 0.0.0.0 --port 8000"]
