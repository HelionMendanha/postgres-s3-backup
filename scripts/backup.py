#!/usr/bin/env python3

import os
import sys
import json
import hashlib
import logging
import tempfile
import subprocess
import uuid

from pathlib import Path
from datetime import datetime, timezone

import boto3

from botocore.config import Config


# --------------------------------------------------
# Logging
# --------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

logger = logging.getLogger("postgres-s3-backup")

SUPPORTED_VERSIONS = {14, 15, 16, 17, 18}


# --------------------------------------------------
# Configuration
# --------------------------------------------------

def required_env(name):
    value = os.getenv(name)

    if not value:
        raise RuntimeError(
            f"Required environment variable missing: {name}"
        )

    return value


# --------------------------------------------------
# PostgreSQL client
# --------------------------------------------------

def pg_binary(version, binary):
    if version not in SUPPORTED_VERSIONS:
        raise RuntimeError(
            f"Unsupported PostgreSQL version: {version}"
        )

    path = Path(
        f"/usr/lib/postgresql/{version}/bin/{binary}"
    )

    if not path.is_file():
        raise RuntimeError(f"Binary not found: {path}")

    return str(path)


def detect_postgres_version():

    result = subprocess.run(
        [
            pg_binary(18, "psql"),
            "-X",
            "-A",
            "-t",
            "-w",
            "-v", "ON_ERROR_STOP=1",
            "-c", "SHOW server_version_num;"
        ],
        check=True,
        capture_output=True,
        text=True
    )

    version_num = int(result.stdout.strip())
    major = version_num // 10000

    if major not in SUPPORTED_VERSIONS:
        raise RuntimeError(
            f"Unsupported PostgreSQL server: {major}"
        )

    return major, version_num


# --------------------------------------------------
# SHA256
# --------------------------------------------------

def calculate_sha256(filename):

    checksum = hashlib.sha256()

    with open(filename, "rb") as file:

        for chunk in iter(
            lambda: file.read(1024 * 1024), b""
        ):
            checksum.update(chunk)

    return checksum.hexdigest()


# --------------------------------------------------
# S3 client
# --------------------------------------------------

def create_s3_client():

    return boto3.client(
        "s3",
        endpoint_url=os.getenv("S3_ENDPOINT") or None,
        region_name=os.getenv(
            "AWS_DEFAULT_REGION",
            "us-east-1"
        ),
        config=Config(
            signature_version="s3v4",
            connect_timeout=15,
            read_timeout=120,
            retries={
                "max_attempts": 5,
                "mode": "standard"
            },
            s3={
                "addressing_style": os.getenv(
                    "S3_ADDRESSING_STYLE",
                    "path"
                )
            }
        )
    )


# --------------------------------------------------
# Verify remote backup
# --------------------------------------------------

def verify_remote_backup(s3, bucket, key):

    response = s3.get_object(
        Bucket=bucket,
        Key=key
    )

    checksum = hashlib.sha256()
    total_bytes = 0

    body = response["Body"]

    try:
        for chunk in body.iter_chunks(
            chunk_size=1024 * 1024
        ):
            checksum.update(chunk)
            total_bytes += len(chunk)

    finally:
        body.close()

    return checksum.hexdigest(), total_bytes


# --------------------------------------------------
# Main backup process
# --------------------------------------------------

def main():

    os.umask(0o077)

    host = required_env("PGHOST")
    database = required_env("PGDATABASE")
    user = required_env("PGUSER")

    bucket = required_env("S3_BUCKET")

    prefix = os.getenv(
        "S3_PREFIX", "postgresql"
    ).strip("/")

    backup_directory = os.getenv(
        "BACKUP_DIR", "/backup"
    )

    os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGCONNECT_TIMEOUT", "15")

    started_at = datetime.now(timezone.utc)

    timestamp = started_at.strftime(
        "%Y%m%dT%H%M%SZ"
    )

    backup_id = uuid.uuid4().hex[:12]

    logger.info("Starting PostgreSQL backup")
    logger.info("PostgreSQL host: %s", host)
    logger.info("Database: %s", database)
    logger.info("Database user: %s", user)

    major, version_num = detect_postgres_version()

    logger.info(
        "Detected PostgreSQL major version: %s",
        major
    )

    configured_major = os.getenv(
        "PG_MAJOR", "auto"
    )

    if configured_major != "auto":
        if int(configured_major) != major:
            raise RuntimeError(
                "Configured client version does not "
                "match PostgreSQL server version"
            )

    pg_dump = pg_binary(major, "pg_dump")
    pg_restore = pg_binary(major, "pg_restore")

    safe_database = "".join(
        char if char.isalnum() or char in "-_"
        else "_"
        for char in database
    )

    filename = (
        f"{safe_database}_pg{major}_"
        f"{timestamp}_{backup_id}.dump"
    )

    object_key = "/".join(
        part for part in [
            prefix,
            safe_database,
            started_at.strftime("%Y/%m/%d"),
            filename
        ] if part
    )

    with tempfile.TemporaryDirectory(
        prefix="pgbackup-",
        dir=backup_directory
    ) as workdir:

        dump_file = Path(workdir) / filename

        # Generate PostgreSQL custom-format backup

        logger.info("Executing pg_dump")

        subprocess.run(
            [
                pg_dump,
                "--format=custom",
                "--no-password",
                "--file", str(dump_file)
            ],
            check=True
        )

        size_bytes = dump_file.stat().st_size

        if size_bytes == 0:
            raise RuntimeError(
                "Generated backup is empty"
            )

        logger.info(
            "Backup generated: %s bytes",
            size_bytes
        )

        # Validate backup archive catalog

        subprocess.run(
            [
                pg_restore,
                "--list",
                str(dump_file)
            ],
            check=True,
            stdout=subprocess.DEVNULL
        )

        logger.info("Backup archive catalog validated")

        # Calculate local SHA256

        local_sha256 = calculate_sha256(dump_file)

        logger.info(
            "Local SHA256: %s",
            local_sha256
        )

        # Upload backup

        s3 = create_s3_client()

        logger.info(
            "Uploading backup to s3://%s/%s",
            bucket,
            object_key
        )

        s3.upload_file(
            str(dump_file),
            bucket,
            object_key,
            ExtraArgs={
                "ContentType": "application/octet-stream",
                "Metadata": {
                    "sha256": local_sha256,
                    "postgres-version": str(version_num)
                }
            }
        )

        # Verify remote content integrity

        logger.info("Verifying remote backup integrity")

        remote_sha256, remote_size = verify_remote_backup(
            s3,
            bucket,
            object_key
        )

        if remote_sha256 != local_sha256:
            raise RuntimeError(
                "Remote SHA256 checksum mismatch"
            )

        if remote_size != size_bytes:
            raise RuntimeError(
                "Remote backup size mismatch"
            )

        logger.info("Remote SHA256: %s", remote_sha256)
        logger.info("Remote backup integrity: OK")

        # Generate backup manifest

        finished_at = datetime.now(timezone.utc)

        manifest = {
            "status": "success",
            "database": database,
            "postgres_major_version": major,
            "postgres_version_num": version_num,
            "backup_format": "custom",
            "filename": filename,
            "bucket": bucket,
            "object_key": object_key,
            "size_bytes": size_bytes,
            "sha256": local_sha256,
            "remote_verified": True,
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "duration_seconds": round(
                (finished_at - started_at).total_seconds(),
                2
            )
        }

        s3.put_object(
            Bucket=bucket,
            Key=f"{object_key}.json",
            Body=json.dumps(
                manifest,
                indent=2
            ).encode("utf-8"),
            ContentType="application/json"
        )

        logger.info(
            "Backup manifest uploaded successfully"
        )

        print(json.dumps(manifest, indent=2))

    logger.info("Temporary backup files removed")
    logger.info("BACKUP COMPLETED SUCCESSFULLY")


if __name__ == "__main__":

    try:
        main()

    except Exception as error:

        logger.error("BACKUP FAILED: %s", error)

        sys.exit(1)
