FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Unprivileged runtime user.
RUN groupadd --system app && useradd --system --gid app --home-dir /app --no-create-home app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini pyproject.toml ./
COPY app ./app
COPY scripts ./scripts
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 0755 /usr/local/bin/docker-entrypoint.sh && mkdir -p /app/data && chown app:app /app/data

USER app
ENV DATABASE_URL=sqlite:////app/data/app.db \
    BACKUP_DIR=/app/data/backups

VOLUME ["/app/data"]
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "app.main"]
