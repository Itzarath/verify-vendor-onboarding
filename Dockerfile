# One image: the built React frontend served by the FastAPI backend, plus the in-process evaluation worker.
# Build:  docker build -t verify .        Run: see docs/DEPLOYMENT.md (needs DATABASE_URL and the other settings).

# ---- 1. frontend
FROM node:24-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

# ---- 2. backend + built frontend
FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    APP_ENV=prod
WORKDIR /app
COPY backend/requirements.txt backend/requirements-prod.txt backend/
# PyMuPDF 2.x also pulls in optional layout/LLM extras (onnxruntime, numpy, ...) that this app never loads: install it
# on its own without them, then the rest normally.
RUN pip install --no-deps pymupdf==2.1  && grep -v '^pymupdf' backend/requirements-prod.txt > /tmp/req.txt  && pip install -r /tmp/req.txt -c backend/requirements.txt
COPY backend/alembic.ini backend/
COPY backend/app backend/app
COPY backend/migrations backend/migrations
COPY backend/policies backend/policies
COPY backend/fixtures/out backend/fixtures/out
COPY --from=web /web/dist frontend/dist
# run as an unprivileged user; nothing in the image needs to be writable
RUN useradd --create-home --uid 10001 app
USER app
WORKDIR /app/backend
EXPOSE 8000
# migrate, then serve; the platform sets PORT. Proxy headers so rate limits see the real client address.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --proxy-headers --forwarded-allow-ips='*'"]
