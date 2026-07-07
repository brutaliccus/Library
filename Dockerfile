FROM python:3.11-slim

WORKDIR /app

# Calibre for MOBI/AZW3 → EPUB conversion; p7zip for RAR/ZIP extraction (ebook torrents)
RUN apt-get update && apt-get install -y --no-install-recommends calibre p7zip-full \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY migrations/ migrations/
COPY app/ app/
COPY backend/static/ static/

RUN mkdir -p /app/data

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
