#!/usr/bin/env python3
"""Conservative GFS retention planner for PostgreSQL S3 backups."""
import os
import sys
import json
import re
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from common import validate_database_name
from backup import required_env, create_s3_client
from restore import read_manifest

logger = logging.getLogger('postgres-s3-retention')


def select_backups(records, daily=7, weekly=4, monthly=3, tz=timezone.utc):
    ordered = sorted(records, key=lambda r: (r['started'], r['key']), reverse=True)
    keep = {}
    for label, limit, period in (
        ('daily', daily, lambda d: d.date()),
        ('weekly', weekly, lambda d: tuple(d.isocalendar()[:2])),
        ('monthly', monthly, lambda d: (d.year, d.month)),
    ):
        seen = set()
        for record in ordered:
            slot = period(record['started'].astimezone(tz))
            if slot in seen:
                continue
            if len(seen) >= limit:
                break
            seen.add(slot)
            keep.setdefault(record['key'], []).append(label)
    if ordered:
        keep.setdefault(ordered[0]['key'], []).append('latest')
    return keep


def count_env(name, default):
    value = os.getenv(name, str(default))
    if not re.fullmatch(r'[0-9]{1,4}', value) or not 1 <= int(value) <= 1000:
        raise RuntimeError(f'{name} must be an integer between 1 and 1000')
    return int(value)


def main():
    bucket = required_env('S3_BUCKET')
    prefix = required_env('S3_PREFIX').strip('/')
    database = required_env('PGDATABASE')
    if not prefix or any(p in ('', '.', '..') for p in prefix.split('/')):
        raise RuntimeError('Explicit nonempty S3_PREFIX required')
    validate_database_name(database)
    if os.getenv('RETENTION_DRY_RUN', 'true').lower() != 'true':
        raise RuntimeError('Deletion is disabled in this release')
    daily = count_env('RETENTION_DAILY', 7)
    weekly = count_env('RETENTION_WEEKLY', 4)
    monthly = count_env('RETENTION_MONTHLY', 3)
    tz = ZoneInfo(os.getenv('RETENTION_TIMEZONE', 'UTC'))
    scope = f'{prefix}/{database}/'
    pattern = re.compile(re.escape(scope) + r'(\d{4}/\d{2}/\d{2})/' + re.escape(database) + r'_pg(14|15|16|17|18)_(\d{8}T\d{6}Z)_[a-f0-9]{12}\.dump')
    s3 = create_s3_client()
    records = []
    ignored = []
    keys = set()
    for page in s3.get_paginator('list_objects_v2').paginate(Bucket=bucket, Prefix=scope):
        keys.update(obj['Key'] for obj in page.get('Contents', []))
    for key in sorted(keys):
        if not key.endswith('.dump.json'):
            if not key.endswith('.dump') or key+'.json' not in keys:
                ignored.append(key)
            continue
        dump = key[:-5]
        match = pattern.fullmatch(dump)
        if not match or dump not in keys:
            ignored.append(key)
            continue
        # Fail closed on invalid manifests or storage errors.
        m = read_manifest(s3, bucket, dump)
        started = datetime.fromisoformat(m['started_at'])
        finished = datetime.fromisoformat(m['finished_at'])
        if started.tzinfo is None or finished.tzinfo is None:
            raise RuntimeError(f'Missing timezone: {key}')
        started = started.astimezone(timezone.utc)
        if (m.get('database') != database or started.strftime('%Y/%m/%d') != match[1]
                or started.strftime('%Y%m%dT%H%M%SZ') != match[3]
                or m['postgres_major_version'] != int(match[2])
                or finished < started or finished > datetime.now(timezone.utc)):
            raise RuntimeError(f'Inconsistent manifest: {key}')
        head = s3.head_object(Bucket=bucket, Key=dump)
        if head['ContentLength'] != m['size_bytes'] or head.get('Metadata', {}).get('sha256') != m['sha256']:
            raise RuntimeError(f'Object metadata mismatch: {dump}')
        records.append({'key': dump, 'started': started, 'size': m['size_bytes']})
    if not records:
        raise RuntimeError('No valid backup pairs found; refusing to plan retention')
    keep = select_backups(records, daily, weekly, monthly, tz)
    candidates = [r for r in records if r['key'] not in keep]
    print(json.dumps({'status': 'success', 'dry_run': True, 'bucket': bucket,
                      'prefix': scope, 'valid_backups': len(records),
                      'keep': keep, 'candidates': [r['key'] for r in candidates],
                      'candidate_bytes': sum(r['size'] for r in candidates),
                      'ignored': ignored, 'deleted': 0}, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        logger.error('RETENTION FAILED: %s', error)
        sys.exit(1)
