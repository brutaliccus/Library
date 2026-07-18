# --- Frontend (Vite → static assets) ---
FROM node:22-bookworm-slim AS frontend
WORKDIR /src/frontend

COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
# outDir matches vite.config (../backend/static) so assets land in /src/backend/static
RUN npm run build


# --- Backend API + baked static UI ---
FROM python:3.11-slim

WORKDIR /app

# Calibre for MOBI/AZW3 → EPUB; p7zip for RAR/ZIP extraction (ebook torrents)
RUN apt-get update && apt-get install -y --no-install-recommends calibre p7zip-full \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY migrations/ migrations/
COPY app/ app/
COPY scripts/ scripts/
COPY --from=frontend /src/backend/static/ static/

RUN mkdir -p /app/data

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
