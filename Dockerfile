FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="PostgreSQL S3 Backup"
LABEL org.opencontainers.image.description="Multi-version PostgreSQL backup and restore for S3-compatible storage"
LABEL org.opencontainers.image.source="https://github.com/HelionMendanha/postgres-s3-backup"
LABEL org.opencontainers.image.version="1.0.0"

ENV DEBIAN_FRONTEND=noninteractive \
    TZ=UTC \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    AWS_EC2_METADATA_DISABLED=true \
    PG_MAJOR=auto

# Install system dependencies and PostgreSQL repository
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        bash \
        jq \
        tzdata; \
    install -d /usr/share/postgresql-common/pgdg; \
    curl -fsSL \
        https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc; \
    echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt bookworm-pgdg main" \
        > /etc/apt/sources.list.d/pgdg.list; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        postgresql-client-14 \
        postgresql-client-15 \
        postgresql-client-16 \
        postgresql-client-17 \
        postgresql-client-18; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN python -m pip install --no-cache-dir \
    boto3==1.43.113

# Validate PostgreSQL clients during build
RUN set -eux; \
    for version in 14 15 16 17 18; do \
        /usr/lib/postgresql/${version}/bin/pg_dump --version; \
        /usr/lib/postgresql/${version}/bin/pg_restore --version; \
        /usr/lib/postgresql/${version}/bin/psql --version; \
    done

# Create non-root user
RUN groupadd --gid 10001 pgbackup \
    && useradd \
        --uid 10001 \
        --gid 10001 \
        --create-home \
        --shell /bin/bash \
        pgbackup \
    && mkdir -p /app /backup \
    && chown -R 10001:10001 /app /backup

# Default PostgreSQL client
ENV PATH="/usr/lib/postgresql/16/bin:${PATH}"

WORKDIR /app

COPY --chown=0:0 scripts/ /app/scripts/

USER 10001:10001

CMD ["python", "/app/scripts/backup.py"]
