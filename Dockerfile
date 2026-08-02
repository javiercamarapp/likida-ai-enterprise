# syntax=docker/dockerfile:1
# =============================================================================
# Likida AI — Agente contable IA enterprise
# Imagen de la API FastAPI (servida por uvicorn) en build multi-stage.
#
# Build:
#   docker build -t b2b-ai:1.0.0 .
# Run (single container):
#   docker run --rm -p 8000:8000 \
#     -e B2B_API_KEY=$(openssl rand -hex 32) \
#     -v b2b-data:/data \
#     b2b-ai:1.0.0
# En producción se orquesta con docker-compose.prod.yml (api+postgres+redis+nginx).
# =============================================================================

# ---- Stage 1: builder — instala dependencias + empaqueta en un prefix ----
# Base de imagen fija para builds reproducibles.
FROM python:3.11-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /build

# Instalar dependencias del sistema mínimas que algunas wheels compiladas
# puedan necesitar (psycopg2-binary / lxml traen sus binaries: no requiere
# compilación; se mantiene para futuras dependencias).
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Primero el manifiesto de dependencias → capa cacheable por separado.
# (El código cambia mucho más a menudo que pyproject.toml.)
COPY pyproject.toml README.md ./
COPY b2b_ai ./b2b_ai
COPY landing ./landing

# Instala en /install (prefix) para copiar solo lo necesario al runtime.
# NOTA: se usa pip install directo (sin --mount=type=cache de BuildKit) para
# que el Dockerfile funcione con el builder legacy y con BuildKit por igual.
RUN pip install --prefix=/install --no-cache-dir . \
    && pip install --prefix=/install --no-cache-dir "uvicorn[standard]>=0.20" \
    && pip install --prefix=/install --no-cache-dir playwright

# Instala Chromium en /opt/playwright (binario reutilizable por el runtime).
# --with-deps instala deps del sistema en el builder (no se copian al runtime;
# el stage runtime las instala explícitamente).
RUN python -m playwright install --with-deps chromium

# ---- Stage 2: runtime — solo lo necesario para servir ----
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    B2B_DB_PATH=/data/b2b_ai.db \
    B2B_API_KEY="" \
    B2B_WORKERS=1 \
    B2B_HOST=0.0.0.0 \
    B2B_PORT=8000 \
    # Railway asigna PORT dinámicamente. Si $PORT existe, B2B_PORT se ignora
    # porque el CMD usa ${PORT} como fallback. Esto permite compatibilidad con
    # Railway (PORT dinámico) y Docker local (B2B_PORT=8000).
    PORT=8000 \
    # Playwright: ruta donde están los navegadores instalados.
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app

# Etiquetas de trazabilidad de la imagen.
LABEL org.opencontainers.image.title="b2b-ai" \
      org.opencontainers.image.version="1.0.0" \
      org.opencontainers.image.description="Agente contable IA enterprise (Likida AI)"

# Copia solo el prefix instalado + la landing estática (no el builder completo).
COPY --from=builder /install /usr/local
COPY --from=builder /build/landing /app/landing

# Copia el binario de Chromium instalado en el builder.
COPY --from=builder /opt/playwright /opt/playwright

# Dependencias del sistema que Chromium necesita para correr en headless.
# Instaladas explícitamente porque --with-deps del builder NO las copia.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl \
        libglib2.0-0 \
        libnss3 \
        libnspr4 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxrandr2 \
        libgbm1 \
        libpango-1.0-0 \
        libcairo2 \
        libasound2 \
        libxshmfence1 \
        libx11-xcb1 \
        libxcb1 \
        fonts-liberation \
        fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Directorio de datos persistente (montar como volumen).
RUN mkdir -p /data

# No corre como root (buena práctica en contenedores).
RUN useradd --create-home --uid 1000 b2b \
    && chown -R b2b:b2b /data /app /opt/playwright
USER b2b

EXPOSE 8000

# Healthcheck del API vía curl (más robusto que urllib para stack).
# Además, el smoke test de Playwright se ejecuta vía endpoint /health/playwright.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# Workers configurables vía B2B_WORKERS. En producción detrás de Nginx se
# recomienda B2B_WORKERS=$(nproc). El contenedor con SQLite debe correr con 1
# worker (concurrencia de escritura); el backend Postgres (en desarrollo) puede
# escalar a N.
# CMD: usa $PORT si existe (Railway), sino B2B_PORT (Docker local).
CMD ["sh", "-c", "uvicorn b2b_ai.api.app:app --host ${B2B_HOST} --port ${PORT:-${B2B_PORT}} --workers ${B2B_WORKERS} --proxy-headers"]
