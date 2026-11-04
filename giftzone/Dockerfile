# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATABASE_URL=sqlite+aiosqlite:////data/monstal.db

WORKDIR /app
COPY requirements.txt ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export PIP_CERT=/run/secrets/proxy_ca; fi; \
    python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --create-home --uid 10001 giftzone \
    && mkdir /data \
    && chown giftzone:giftzone /data
COPY --chown=giftzone:giftzone . .
USER giftzone

CMD ["sh", "-c", "alembic upgrade head && python -m app.seed && exec python -m app.main"]
