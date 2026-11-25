#!/usr/bin/env python3
import os
import sys
import json
import re
import logging
import tempfile
import subprocess
from pathlib import Path
from datetime import datetime, timezone

from backup import (
    required_env, pg_binary, detect_postgres_version,
    calculate_sha256, create_s3_client, SUPPORTED_VERSIONS,
)

logger = logging.getLogger('postgres-s3-restore')


def query(major, sql):
    return subprocess.run([
        pg_binary(major, 'psql'), '-X', '-At', '-w',
        '-v', 'ON_ERROR_STOP=1', '-c', sql
    ], check=True, text=True, stdout=subprocess.PIPE).stdout.strip()


def assert_empty(major, database):
    if query(major, 'SELECT current_database();') != database:
        raise RuntimeError('Connected database differs from requested target')
    # Reject custom schemas, relations, routines, types and extensions.
    sql = """
    SELECT EXISTS (
      SELECT 1 FROM pg_namespace
      WHERE nspname NOT IN ('public','pg_catalog','information_schema')
        AND nspname NOT LIKE 'pg_toast%'
        AND nspname NOT LIKE 'pg_temp_%'
      UNION ALL
      SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname='public'
      UNION ALL
      SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='public'
      UNION ALL
      SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace
        WHERE n.nspname='public'
      UNION ALL
      SELECT 1 FROM pg_extension WHERE extname <> 'plpgsql'
      UNION ALL
      SELECT 1 FROM pg_largeobject_metadata
      UNION ALL
      SELECT 1 FROM pg_event_trigger
      UNION ALL
      SELECT 1 FROM pg_publication
      UNION ALL
      SELECT 1 FROM pg_foreign_server
    );
    """
    if query(major, sql) != 'f':
        raise RuntimeError('Target database is not empty or cannot be verified')


def read_manifest(s3, bucket, key):
    body = s3.get_object(Bucket=bucket, Key=key+'.json')['Body']
    try:
        raw = body.read(65537)
    finally:
        body.close()
    if len(raw) > 65536:
        raise RuntimeError('Manifest exceeds size limit')
    m = json.loads(raw)
    if not isinstance(m, dict):
        raise RuntimeError('Invalid manifest')
    if (m.get('status') != 'success' or m.get('remote_verified') is not True
        or m.get('bucket') != bucket or m.get('object_key') != key
        or m.get('backup_format') != 'custom'
        or m.get('filename') != key.rsplit('/', 1)[-1]
        or type(m.get('postgres_major_version')) is not int
        or m['postgres_major_version'] not in SUPPORTED_VERSIONS
        or type(m.get('postgres_version_num')) is not int
        or m['postgres_version_num']//10000 != m['postgres_major_version']
        or type(m.get('size_bytes')) is not int or m['size_bytes'] <= 0
        or not isinstance(m.get('sha256'), str)
        or not re.fullmatch(r'[a-f0-9]{64}', m['sha256'])):
        raise RuntimeError('Manifest validation failed')
    return m


def main():
    os.umask(0o077)
    host = required_env('PGHOST')
    database = required_env('PGDATABASE')
    required_env('PGUSER')
    bucket = required_env('S3_BUCKET')
    key = required_env('S3_OBJECT_KEY')
    os.environ.setdefault('PGPORT', '5432')
    os.environ.setdefault('PGCONNECT_TIMEOUT', '15')
    port = os.environ['PGPORT']
    if not port.isdecimal() or not 1 <= int(port) <= 65535 or ',' in host:
        raise RuntimeError('Specify a single host and valid port')
    if any(os.getenv(v) for v in ('PGHOSTADDR','PGSERVICE','PGSERVICEFILE','PGOPTIONS')):
        raise RuntimeError('Conflicting libpq connection settings')
    if '=' in database or database.startswith(('postgresql://','postgres://')):
        raise RuntimeError('PGDATABASE must be a database name, not a connection string')
    if database in ('postgres','template0','template1'):
        raise RuntimeError('System database restore is prohibited')
    if required_env('RESTORE_CONFIRM') != f'{host}:{port}/{database}':
        raise RuntimeError('RESTORE_CONFIRM must exactly match host:port/database')
    if not key.endswith('.dump'):
        raise RuntimeError('S3_OBJECT_KEY must identify the dump, not the manifest')
    started = datetime.now(timezone.utc)
    logger.info('Starting PostgreSQL restore to %s:%s/%s', host, port, database)
    target, version_num = detect_postgres_version()
    configured = os.getenv('PG_MAJOR','auto')
    if configured != 'auto' and int(configured) != target:
        raise RuntimeError('Configured client version differs from target server')
    assert_empty(target, database)
    s3 = create_s3_client()
    manifest = read_manifest(s3, bucket, key)
    if manifest['postgres_major_version'] > target:
        raise RuntimeError('Restoring to an older PostgreSQL major version is prohibited')
    logger.info('Manifest validated; source major=%s target major=%s', manifest['postgres_major_version'], target)
    with tempfile.TemporaryDirectory(prefix='pgrestore-', dir=os.getenv('BACKUP_DIR','/backup')) as tmp:
        dump = Path(tmp)/'restore.dump'
        response = s3.get_object(Bucket=bucket, Key=key)
        body = response['Body']
        try:
            if response['ContentLength'] != manifest['size_bytes']:
                raise RuntimeError('Remote object size mismatch')
            total = 0
            with dump.open('wb') as out:
                for chunk in body.iter_chunks(chunk_size=1024*1024):
                    total += len(chunk)
                    if total > manifest['size_bytes']:
                        raise RuntimeError('Download exceeds manifest size')
                    out.write(chunk)
        finally:
            body.close()
        if dump.stat().st_size != manifest['size_bytes'] or calculate_sha256(dump) != manifest['sha256']:
            raise RuntimeError('Backup size or SHA256 mismatch')
        logger.info('Downloaded backup integrity: OK')
        restore = pg_binary(target,'pg_restore')
        subprocess.run([restore,'--list',str(dump)], check=True, stdout=subprocess.DEVNULL)
        logger.info('Backup archive catalog validated')
        assert_empty(target, database)
        logger.info('Executing pg_restore in a single transaction')
        subprocess.run([restore,'--dbname',database,'--no-password','--exit-on-error',
                        '--single-transaction','--no-owner','--no-privileges',str(dump)],check=True)
    finished = datetime.now(timezone.utc)
    logger.info('RESTORE COMPLETED SUCCESSFULLY')
    print(json.dumps({'status':'success','database':database,'source_object_key':key,
                      'postgres_version_num':version_num,'sha256':manifest['sha256'],
                      'duration_seconds':round((finished-started).total_seconds(),2)},indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        logger.error('RESTORE FAILED: %s', error)
        sys.exit(1)
