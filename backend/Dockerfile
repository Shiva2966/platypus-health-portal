# Health Portal app image (DEMO - synthetic data only).
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    AUTO_MIGRATE=1 \
    HP_SECURE_COOKIES=1 \
    WEB_CONCURRENCY=1

WORKDIR /app

# PostgreSQL 16 client tools (pg_dump / pg_restore) so scripts/backup.py + restore.py work inside the
# container and match the server's major version (PGDG apt repo; Debian's own client is v15).
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && install -d /usr/share/keyrings \
 && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor -o /usr/share/keyrings/pgdg.gpg \
 && echo "deb [signed-by=/usr/share/keyrings/pgdg.gpg] http://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
      > /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client-16 \
 && apt-get purge -y --auto-remove curl gnupg \
 && rm -rf /var/lib/apt/lists/*

# dependencies first (layer cache). psycopg[binary] bundles libpq.
COPY requirements.txt .
RUN pip install -r requirements.txt

# non-root user
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin app

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini seed.py ./
COPY scripts ./scripts
RUN mkdir -p /app/data /app/backups && chown -R app:app /app

USER app
EXPOSE 8000

# liveness only (no DB hit); readiness (/readyz) is for the orchestrator / load balancer
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

# --no-access-log: URLs can contain search text (patient names). Proxy-level logs strip the query string.
# --proxy-headers: honour X-Forwarded-Proto/For from the TLS proxy so cookies are marked Secure and
# client IPs are correct for rate limiting.  Only expose this port to the proxy, never to the internet.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*", "--no-access-log"]
