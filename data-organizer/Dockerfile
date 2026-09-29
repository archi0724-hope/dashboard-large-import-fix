FROM python:3.12-slim

# System dependencies:
#   tesseract-ocr        - OCR for scanned PDFs / images
#   libreoffice-writer    - converts legacy .doc to .docx (read-only, into a temp copy)
#   fonts-dejavu-core     - decent OCR/PDF font coverage
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-eng libreoffice-writer fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd -m -u 1000 organizer \
    && mkdir -p /app/data /app/logs /app/secrets \
    && chown -R organizer:organizer /app
USER organizer

ENV DASHBOARD_HOST=0.0.0.0 \
    DASHBOARD_PORT=8000 \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# Healthcheck hits the JSON API, not the HTML page
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s CMD \
    python -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8000/api/progress', timeout=3)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
