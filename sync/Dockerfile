# Pin the tested Python release and image digest; update after dependency tests.
FROM python:3.14.7-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}
LABEL org.opencontainers.image.version=${APP_VERSION}
COPY VERSION /opt/adult-hub/VERSION

# Scheduled database backups use pg_dump/pg_restore. Match the PostgreSQL 17
# test database rather than Debian Bookworm's older client.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg git \
    && install -d /usr/share/keyrings \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
       | gpg --dearmor -o /usr/share/keyrings/postgresql.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-17 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install the fully pinned sync dependency set, including the shared
# package's runtime dependencies, before installing the local package itself.
COPY common/ ./common/
COPY resolver/video_duplicates.py resolver/video_duplicate_scoring.py resolver/video_duplicate_merge_history.py ./
COPY sync/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir --no-deps ./common

COPY backup_db.py backup_scheduler.py restore_db.py ./
COPY tools/benchmark_catalog_search.py ./tools/benchmark_catalog_search.py
COPY sync/ .

# Run as non-root: /data and /cache are volume-mounted read-write in
# compose, so they are writable for appuser (the database lives in Postgres).
RUN useradd -m -u 10000 appuser && mkdir -p /cache /data && chown -R appuser /app /cache
USER appuser

CMD ["python", "runner.py"]
