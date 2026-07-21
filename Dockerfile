FROM python:3.12-slim-bookworm AS runtime

ARG OCR_ENGINE=tesseract

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PDF_OCR_ENGINE=${OCR_ENGINE}

WORKDIR /app

COPY requirements.txt requirements-rapidocr.txt ./

RUN if [ "$OCR_ENGINE" = "tesseract" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng \
        && rm -rf /var/lib/apt/lists/* \
        && pip install --no-cache-dir -r requirements.txt; \
    elif [ "$OCR_ENGINE" = "rapidocr" ]; then \
        apt-get update \
        && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
        && rm -rf /var/lib/apt/lists/* \
        && pip install --no-cache-dir -r requirements-rapidocr.txt; \
    else \
        echo "Unsupported OCR_ENGINE: $OCR_ENGINE" >&2; exit 1; \
    fi

COPY app ./app

RUN mkdir -p /app/app/data /app/outputs \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=3)" || exit 1

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
