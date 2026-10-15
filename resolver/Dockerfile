# Pin the tested Python release and image digest; update after dependency tests.
FROM python:3.14.7-slim-bookworm@sha256:82bc3c539b8813ada9d68c63b40158fa002f7f33de9bf3312a3dfdc0620dff56

WORKDIR /app

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ARG APP_VERSION=dev
ENV APP_VERSION=${APP_VERSION}
LABEL org.opencontainers.image.version=${APP_VERSION}
COPY VERSION /opt/adult-hub/VERSION

# Resolver dependencies include a pinned Git source package; backup
# verification also needs PostgreSQL custom-archive tools.
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg git \
    && install -d /usr/share/keyrings \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
       | gpg --dearmor -o /usr/share/keyrings/postgresql.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/postgresql.gpg] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
       > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-17 ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Install the fully pinned resolver dependency set, including the shared
# package's runtime dependencies, before installing the local package itself.
COPY common/ ./common/
COPY sync/video_fingerprints.py ./resolver_video_fingerprints.py
COPY sync/fingerprint_mp4.py ./fingerprint_mp4.py
COPY resolver/requirements.lock ./requirements.lock
RUN pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir --no-deps ./common

COPY resolver/ .
COPY tools/export_video_duplicate_review_corpus.py ./tools/export_video_duplicate_review_corpus.py

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/admin/health/ready')" || exit 1

# Run as non-root: /cache and /data are volume-mounted read-write in
# compose, so they are writable for appuser (the database lives in Postgres).
RUN useradd -m -u 10000 appuser && mkdir -p /cache /data && chown -R appuser /app /cache
USER appuser

CMD ["python3", "supervisor.py"]
