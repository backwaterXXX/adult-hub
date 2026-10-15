"""PostgreSQL backup and restore operations."""

from __future__ import annotations

import os
import re

import psycopg

from common.db import _postgres_database_identity, database_url


def pg_backup(db_url: str | None = None, backup_dir=None, prefix: str = "adulthub", *, job_id: str | None = None) -> "object":
    """Create a compressed pg_dump backup and validate its custom archive."""
    import hashlib
    import gzip
    import shutil
    import subprocess
    import uuid
    import time as _time
    from pathlib import Path as _Path
    from urllib.parse import urlsplit

    dest_dir = _Path(backup_dir) if backup_dir else _Path(
        os.getenv("BACKUP_DIR", "./data/backups"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    if job_id is not None and not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("Invalid backup job identifier.")
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    dest = dest_dir / (f"{prefix}-job-{job_id}.dump.gz" if job_id else f"{prefix}-{stamp}.dump.gz")
    pg_restore = shutil.which("pg_restore")
    if not pg_restore:
        raise RuntimeError("pg_restore not found on PATH")

    def archive_is_valid(path):
        """Check a compressed custom-format dump without buffering it in RAM."""
        verify_tmp = dest_dir / f".{dest.name}.{uuid.uuid4().hex}.verify.dump"
        try:
            with gzip.open(path, "rb") as compressed, verify_tmp.open("wb") as raw:
                shutil.copyfileobj(compressed, raw, length=1024 * 1024)
            verification = subprocess.run(
                [pg_restore, "--list", str(verify_tmp)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False,
            )
            return verification.returncode == 0
        except (OSError, EOFError, subprocess.TimeoutExpired):
            return False
        finally:
            verify_tmp.unlink(missing_ok=True)

    if job_id and dest.is_file():
        sidecar = dest.with_name(dest.name + ".sha256")
        try:
            expected = sidecar.read_text(encoding="ascii").split()[0]
            digest = hashlib.sha256()
            with dest.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
            if (len(expected) == 64 and digest.hexdigest() == expected
                    and archive_is_valid(dest)):
                return dest
        except (OSError, ValueError, IndexError):
            pass
    conninfo = (db_url or "").strip() or database_url()
    pg_dump = shutil.which("pg_dump")
    if not pg_dump:
        raise RuntimeError("pg_dump not found on PATH")
    try:
        password = urlsplit(conninfo).password or ""
    except Exception:
        password = ""
    env = dict(os.environ, PGPASSWORD=password)
    raw_tmp = dest_dir / f".{dest.name}.{uuid.uuid4().hex}.dump"
    gzip_tmp = dest_dir / f".{dest.name}.{uuid.uuid4().hex}.tmp"
    try:
        proc = subprocess.run(
            [pg_dump, "--dbname", conninfo, "--format", "custom",
             "--no-owner", "--no-privileges", "--file", str(raw_tmp)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pg_dump failed: {proc.stderr.decode()[:300]}")
        try:
            verification = subprocess.run(
                [pg_restore, "--list", str(raw_tmp)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=30, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("pg_restore archive verification timed out") from exc
        if verification.returncode != 0:
            raise RuntimeError(f"pg_restore archive verification failed: {verification.stderr.decode()[:300]}")
        with raw_tmp.open("rb") as src, gzip.open(gzip_tmp, "wb") as compressed:
            shutil.copyfileobj(src, compressed, length=1024 * 1024)
        os.replace(gzip_tmp, dest)
    finally:
        raw_tmp.unlink(missing_ok=True)
        gzip_tmp.unlink(missing_ok=True)
    digest = hashlib.sha256()
    with dest.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    checksum = dest.with_name(dest.name + ".sha256")
    temporary = checksum.with_suffix(checksum.suffix + ".tmp")
    try:
        temporary.write_text(f"{digest.hexdigest()}  {dest.name}\n", encoding="ascii")
        os.replace(temporary, checksum)
    finally:
        temporary.unlink(missing_ok=True)
    return dest


def pg_restore(dump_path: "object", db_url: str | None = None, *,
               clean: bool = True, run_migrations: bool = True) -> bool:
    """Restore a database from a pg_dump custom-format archive (supports .dump and .dump.gz)."""
    import gzip
    import shutil
    import subprocess
    import tempfile
    from pathlib import Path as _Path
    from urllib.parse import urlsplit

    pg_restore_bin = shutil.which("pg_restore")
    if not pg_restore_bin:
        raise RuntimeError("pg_restore not found on PATH")

    dump_file = _Path(dump_path)
    if not dump_file.exists():
        raise FileNotFoundError(f"Dump file not found: {dump_file}")

    conninfo = (db_url or "").strip() or database_url()
    try:
        password = urlsplit(conninfo).password or ""
    except Exception:
        password = ""
    env = dict(os.environ, PGPASSWORD=password)

    tmp_path = None
    if dump_file.name.endswith(".gz"):
        with tempfile.NamedTemporaryFile(suffix=".dump", delete=False) as tmp:
            tmp_path = tmp.name
        with gzip.open(dump_file, "rb") as src, open(tmp_path, "wb") as dst:
            shutil.copyfileobj(src, dst)
        target_path = tmp_path
    else:
        target_path = str(dump_file)

    try:
        verify = subprocess.run(
            [pg_restore_bin, "--list", target_path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if verify.returncode != 0:
            raise RuntimeError(f"Dump verification failed: {verify.stderr.decode()[:300]}")

        cmd = [
            pg_restore_bin,
            "--dbname", conninfo,
            "--no-owner",
            "--no-privileges",
        ]
        if clean:
            cmd.extend(["--clean", "--if-exists"])
        cmd.append(target_path)

        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"pg_restore failed: {proc.stderr.decode()[:400]}")

        if run_migrations:
            from common.migrations import apply_migrations
            apply_migrations(conninfo)

        return True
    finally:
        if tmp_path:
            _Path(tmp_path).unlink(missing_ok=True)


def pg_restore_test(dump_path: "object", restore_admin_db_url: str | None = None, *,
                    primary_db_url: str | None, job_id: str,
                    max_uncompressed_bytes: int = 8 * 1024 * 1024 * 1024) -> dict:
    """Restore one checked archive into a random disposable DB, validate, then drop it."""
    import gzip
    import hashlib
    import shutil
    import subprocess
    import sys
    import tempfile
    from pathlib import Path as _Path
    from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlsplit

    source = _Path(dump_path)
    if (source.is_symlink() or not source.is_file() or source.name.startswith(".")
            or not source.name.startswith("adulthub-") or not source.name.endswith(".dump.gz")):
        raise ValueError("Only a regular PostgreSQL backup archive can be tested.")
    sidecar = source.with_name(source.name + ".sha256")
    if sidecar.is_symlink() or not sidecar.is_file():
        raise ValueError("A matching backup checksum is required.")
    try:
        checksum_line = sidecar.read_text(encoding="ascii").strip().split()
        expected_hash, expected_name = checksum_line[0], checksum_line[1]
    except (OSError, UnicodeError, IndexError):
        raise ValueError("Backup checksum is invalid.") from None
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    if expected_name != source.name or not re.fullmatch(r"[0-9a-f]{64}", expected_hash) or digest.hexdigest() != expected_hash:
        raise ValueError("Backup checksum does not match.")

    conninfo = (restore_admin_db_url or "").strip()
    if not conninfo:
        raise ValueError("A dedicated restore-test database connection is not configured.")
    parts = urlsplit(conninfo)
    primary_url = (primary_db_url or "").strip() or database_url()
    target_identity = _postgres_database_identity(conninfo)
    primary_identity = _postgres_database_identity(primary_url)
    if target_identity is None or primary_identity is None:
        raise ValueError("PostgreSQL restore-test connections are invalid.")
    if target_identity == primary_identity:
        raise ValueError("The restore-test connection must not point at the primary database.")
    disposable_name = f"adult_hub_restore_test_{job_id}"
    if not re.fullmatch(r"[a-f0-9]{32}", job_id):
        raise ValueError("Restore test job identifier is invalid.")
    query_pairs = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
                   if key.casefold() not in {"dbname", "database"}]
    clean_query = urlencode(query_pairs, doseq=True)

    def build_database_url(database: str) -> str:
        path = f"/{quote(database, safe='')}"
        if parts.netloc:
            return parts._replace(path=path, query=clean_query).geturl()
        suffix = f"?{clean_query}" if clean_query else ""
        return f"{parts.scheme}://{path}{suffix}"

    maintenance_url = build_database_url("postgres")
    target_url = build_database_url(disposable_name)
    pg_restore_bin = shutil.which("pg_restore")
    if not pg_restore_bin:
        raise RuntimeError("PostgreSQL restore tools are unavailable.")

    raw_path = None
    created = False
    admin_conn = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".dump", delete=False) as raw:
            raw_path = raw.name
            total = 0
            with gzip.open(source, "rb") as compressed:
                while chunk := compressed.read(1024 * 1024):
                    total += len(chunk)
                    if total > max_uncompressed_bytes:
                        raise ValueError("Backup is too large for a disposable restore test.")
                    raw.write(chunk)
        listing = subprocess.run([pg_restore_bin, "--list", raw_path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 timeout=60, check=False)
        if listing.returncode != 0:
            raise ValueError("Backup archive is invalid.")

        admin_conn = psycopg.connect(maintenance_url, connect_timeout=10, autocommit=True)
        with admin_conn.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname=%s", (disposable_name,))
            if cursor.fetchone():
                cursor.execute(psycopg.sql.SQL("DROP DATABASE {} WITH (FORCE)").format(
                    psycopg.sql.Identifier(disposable_name)))
            cursor.execute(psycopg.sql.SQL("CREATE DATABASE {}").format(psycopg.sql.Identifier(disposable_name)))
        created = True

        query = parse_qs(clean_query)
        env = dict(os.environ, PGAPPNAME="adult-hub-restore-test")

        def connection_parameter(query_name: str, uri_value: str | None = None):
            values = query.get(query_name, [])
            if len(values) > 1:
                raise ValueError("PostgreSQL restore-test connections are invalid.")
            return values[0] if values else uri_value

        query_host = connection_parameter("host")
        host = query_host if query_host is not None else (
            unquote(parts.hostname) if parts.hostname else None
        )
        port = connection_parameter("port", str(parts.port) if parts.port else None)
        username = connection_parameter("user", unquote(parts.username) if parts.username else None)
        password = connection_parameter("password", unquote(parts.password) if parts.password else None)
        if host:
            env["PGHOST"] = host
        if port:
            env["PGPORT"] = str(port)
        if username:
            env["PGUSER"] = username
        if password:
            env["PGPASSWORD"] = password
        env["PGDATABASE"] = disposable_name
        for key in ("sslmode", "sslrootcert", "sslcert", "sslkey", "target_session_attrs"):
            if query.get(key):
                env["PG" + key.upper()] = query[key][0]
        restored = subprocess.run(
            [pg_restore_bin, "--dbname", disposable_name, "--no-owner", "--no-privileges",
             "--exit-on-error", raw_path], stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, env=env, timeout=3600, check=False,
        )
        if restored.returncode != 0:
            raise RuntimeError("Backup restore into the disposable database failed.")
        with psycopg.connect(target_url, connect_timeout=10) as restored_conn:
            with restored_conn.cursor() as cursor:
                cursor.execute("SELECT to_regclass('public.videos') IS NOT NULL")
                has_catalog = bool(cursor.fetchone()[0])
                if not has_catalog:
                    raise RuntimeError("Restored database is missing the video catalog table.")
                cursor.execute("SELECT count(*) FROM public.videos")
                catalog_rows = int(cursor.fetchone()[0])
        return {"outcome": "passed", "catalog_rows": catalog_rows}
    except Exception as exc:
        if isinstance(exc, (ValueError, RuntimeError)):
            raise
        raise RuntimeError("Backup restore test could not be completed.") from exc
    finally:
        operation_failed = sys.exc_info()[0] is not None
        cleanup_error = None
        if created and admin_conn is not None:
            try:
                with admin_conn.cursor() as cursor:
                    cursor.execute(psycopg.sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        psycopg.sql.Identifier(disposable_name)))
            except Exception as exc:
                cleanup_error = exc
                # Preserve a failed restore result, but never hide the fact that
                # cleanup could not run in logs: this is a fixed safe message.
                import logging
                logging.getLogger("common.db").error("Could not remove disposable restore-test database.")
        if admin_conn is not None:
            try:
                admin_conn.close()
            except Exception as exc:
                cleanup_error = cleanup_error or exc
        if raw_path:
            try:
                _Path(raw_path).unlink(missing_ok=True)
            except OSError as exc:
                cleanup_error = cleanup_error or exc
        if cleanup_error is not None and not operation_failed:
            raise RuntimeError("Restore test cleanup could not be confirmed.") from cleanup_error
