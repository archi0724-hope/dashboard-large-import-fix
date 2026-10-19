FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY asgi_app.py app.py vendor_core.py storage.py exports.py catalogue_extraction.py import_service.py drive_import.py ./
COPY vendor_dashboard ./vendor_dashboard/
COPY .streamlit ./.streamlit/

RUN mkdir -p /app/vendor_data && chown -R appuser:appuser /app

USER appuser

EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8501/api/health', timeout=3).read()" || exit 1

CMD ["python","-m","streamlit","run","asgi_app.py","--server.address=0.0.0.0","--server.port=8501","--server.headless=true"]
