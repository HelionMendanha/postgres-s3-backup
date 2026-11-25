#!/usr/bin/env python3
"""Shared utilities for PostgreSQL S3 Backup."""

import re


def safe_database_name(database):
    """Return the storage-safe PostgreSQL database name."""

    if not isinstance(database, str) or not database:
        raise RuntimeError("Invalid database name")

    return "".join(
        char if char.isalnum() or char in "-_"
        else "_"
        for char in database
    )


def validate_database_name(database):
    """Require an unambiguous and retention-compatible name."""

    if not isinstance(database, str):
        raise RuntimeError("Invalid database name")

    if not re.fullmatch(r"[A-Za-z0-9_-]+", database):
        raise RuntimeError(
            f"Unsupported database name: {database!r}"
        )

    return database
