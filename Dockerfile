# JobHunt: the whole stack in ONE container (PostgreSQL + API/workers + dashboard).
#   docker build -t jobhunt .
#   docker run -p 10000:10000 -v jobhunt-data:/app/data jobhunt
# All settings live in config/config.yaml.
FROM python:3.12.8-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PG_BIN=/usr/lib/postgresql/15/bin JOBHUNT_CONFIG=/app/config/config.yaml \
    MALLOC_ARENA_MAX=2

# PostgreSQL 15 server (includes pg_trgm). The cluster is created at first start, not here.
RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-15 \
 && rm -rf /var/lib/apt/lists/* /var/lib/postgresql /etc/postgresql \
 && useradd --system --uid 10001 --home-dir /app --shell /usr/sbin/nologin app

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY web ./web
COPY seed ./seed
COPY config ./config
COPY start.py .
RUN mkdir -p /app/data && chown app:app /app/data

EXPOSE 10000
# Starts as root only to fix ownership of a mounted disk; every service runs as "app".
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.getenv('PORT','10000'),timeout=4).status==200 else 1)"
CMD ["python", "/app/start.py"]
