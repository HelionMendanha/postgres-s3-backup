#!/usr/bin/env python3

import os
import sys
import json
import logging
import subprocess
import time

from pathlib import Path
from datetime import datetime, timezone

from common import safe_database_name, validate_database_name

from backup import (
    required_env,
    pg_binary,
    detect_postgres_version
)

logger = logging.getLogger("postgres-s3-backup-all")

SCRIPT_DIR = Path(__file__).resolve().parent


def discover_databases(major, maintenance_db):

    env = os.environ.copy()

    env["PGDATABASE"] = maintenance_db
    env["PGOPTIONS"] = (
        "-c default_transaction_read_only=on "
        "-c statement_timeout=30000"
    )

    sql = """
        SELECT COALESCE(
            json_agg(datname ORDER BY datname),
            '[]'::json
        )
        FROM pg_database
        WHERE NOT datistemplate
          AND datallowconn;
    """

    result = subprocess.run(
        [
            pg_binary(major, "psql"),
            "-X",
            "-A",
            "-t",
            "-w",
            "-v", "ON_ERROR_STOP=1",
            "-c", sql
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True
    )

    databases = json.loads(result.stdout.strip())

    if not isinstance(databases, list):
        raise RuntimeError("Invalid database inventory")

    return databases


def validate_databases(databases):

    prefixes = set()

    for database in databases:

        if (
            not database
            or "=" in database
            or database.startswith(
                ("postgresql://", "postgres://")
            )
        ):
            raise RuntimeError(
                f"Unsafe database name: {database!r}"
            )

        validate_database_name(database)
        prefix = safe_database_name(database)

        if prefix in prefixes:
            raise RuntimeError(
                f"Database storage prefix collision: {prefix}"
            )

        prefixes.add(prefix)


def main():

    started_at = datetime.now(timezone.utc)
    start_time = time.monotonic()

    host = required_env("PGHOST")
    required_env("PGUSER")
    required_env("S3_BUCKET")
    required_env("S3_PREFIX")

    os.environ.setdefault("PGPORT", "5432")
    os.environ.setdefault("PGCONNECT_TIMEOUT", "15")

    maintenance_db = os.getenv(
        "PG_MAINTENANCE_DB", "postgres"
    )

    validate_databases([maintenance_db])

    os.environ["PGDATABASE"] = maintenance_db

    excluded = {
        item.strip()
        for item in os.getenv(
            "EXCLUDE_DATABASES",
            "template0,template1"
        ).split(",")
        if item.strip()
    }

    dry_run = os.getenv(
        "BACKUP_DRY_RUN", "true"
    ).strip().lower()

    if dry_run not in {"true", "false"}:
        raise RuntimeError(
            "BACKUP_DRY_RUN must be true or false"
        )

    logger.info("Starting PostgreSQL instance backup")
    logger.info("PostgreSQL host: %s", host)
    logger.info("Discovery database: %s", maintenance_db)

    major, version_num = detect_postgres_version()

    configured_major = os.getenv("PG_MAJOR", "auto")

    if configured_major != "auto":
        if int(configured_major) != major:
            raise RuntimeError(
                "Configured PostgreSQL major version mismatch"
            )

    logger.info(
        "Detected PostgreSQL major version: %s",
        major
    )

    discovered = discover_databases(
        major,
        maintenance_db
    )

    selected = [
        database
        for database in discovered
        if database not in excluded
    ]

    validate_databases(selected)

    if not selected:
        raise RuntimeError("No databases selected")

    logger.info(
        "Databases discovered: %s",
        len(discovered)
    )

    logger.info(
        "Databases selected: %s",
        len(selected)
    )

    results = []

    for database in selected:

        logger.info(
            "Processing database: %s",
            database
        )

        if dry_run == "true":

            results.append({
                "database": database,
                "status": "planned"
            })

            continue

        env = os.environ.copy()

        env["PGDATABASE"] = database
        env["PG_MAJOR"] = str(major)

        db_start = time.monotonic()

        try:

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT_DIR / "backup.py")
                ],
                env=env,
                check=False
            )

            status = (
                "success"
                if result.returncode == 0
                else "failed"
            )

            results.append({
                "database": database,
                "status": status,
                "exit_code": result.returncode,
                "duration_seconds": round(
                    time.monotonic() - db_start, 2
                )
            })

        except Exception as error:

            logger.error(
                "Database backup failed: %s: %s",
                database,
                error
            )

            results.append({
                "database": database,
                "status": "failed",
                "exit_code": 1
            })

    successful = sum(
        item["status"] == "success"
        for item in results
    )

    failed = sum(
        item["status"] == "failed"
        for item in results
    )

    summary = {
        "status": (
            "dry-run"
            if dry_run == "true"
            else "failed" if failed
            else "success"
        ),
        "host": host,
        "postgres_version_num": version_num,
        "postgres_major_version": major,
        "databases_discovered": len(discovered),
        "databases_selected": len(selected),
        "databases_successful": successful,
        "databases_failed": failed,
        "databases_excluded": [
            database
            for database in discovered
            if database in excluded
        ],
        "results": results,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "duration_seconds": round(
            time.monotonic() - start_time, 2
        )
    }

    print(json.dumps(summary, indent=2))

    if failed:
        logger.error(
            "INSTANCE BACKUP COMPLETED WITH FAILURES"
        )
        return 1

    if dry_run == "true":
        logger.info("BACKUP DISCOVERY COMPLETED")
    else:
        logger.info(
            "INSTANCE BACKUP COMPLETED SUCCESSFULLY"
        )

    return 0


if __name__ == "__main__":

    try:
        sys.exit(main())

    except Exception as error:
        logger.error(
            "INSTANCE BACKUP FAILED: %s",
            error
        )
        sys.exit(1)
