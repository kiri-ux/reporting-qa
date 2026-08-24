"""Pull the order list from S3.

The bucket is treated as the source of truth: every batch refreshes the list
before it runs completeness, but only downloads when the object's ETag has
changed, so a monthly batch does not re-import an unchanged file.
"""
from __future__ import annotations

import datetime as dt
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .config import settings
from .db import OrderSync
from .roster import import_orders


class CredentialsMissing(RuntimeError):
    pass


def _client():
    import boto3

    key = settings.aws_access_key_id.strip()
    secret = settings.aws_secret_access_key.strip()
    missing = [n for n, v in (("AWS_ACCESS_KEY_ID", key),
                              ("AWS_SECRET_ACCESS_KEY", secret)) if not v]
    if missing:
        raise CredentialsMissing(
            f"{' and '.join(missing)} not visible to the running app. Set them on "
            f"the web service in Render, then Manual Deploy so the process "
            f"restarts with them."
        )
    kw = dict(region_name=settings.orders_s3_region.strip() or "us-east-1",
              aws_access_key_id=key, aws_secret_access_key=secret)
    if settings.aws_session_token.strip():
        kw["aws_session_token"] = settings.aws_session_token.strip()
    return boto3.client("s3", **kw)


def last_sync(db: Session) -> OrderSync | None:
    return db.scalars(select(OrderSync).order_by(desc(OrderSync.id)).limit(1)).first()


def _resolve_keys(client) -> list[str]:
    """A key ending in / is treated as a prefix, so several exports can be
    dropped in one folder and merged."""
    out: list[str] = []
    for k in settings.orders_s3_keys:
        if k.endswith("/"):
            token = None
            while True:
                kw = {"Bucket": settings.orders_s3_bucket, "Prefix": k}
                if token:
                    kw["ContinuationToken"] = token
                page = client.list_objects_v2(**kw)
                for obj in page.get("Contents", []):
                    if obj["Key"].lower().endswith((".csv", ".xlsx", ".xlsm")):
                        out.append(obj["Key"])
                if not page.get("IsTruncated"):
                    break
                token = page.get("NextContinuationToken")
        else:
            out.append(k)
    return sorted(set(out))


def head() -> tuple[str, dt.datetime | None]:
    """Combined fingerprint across every key, so adding a file counts as a change."""
    client = _client()
    parts, newest = [], None
    for key in _resolve_keys(client):
        resp = client.head_object(Bucket=settings.orders_s3_bucket, Key=key)
        parts.append(key + ":" + (resp.get("ETag") or "").strip('"'))
        lm = resp.get("LastModified")
        if lm and (newest is None or lm.replace(tzinfo=None) > newest):
            newest = lm.replace(tzinfo=None)
    return "|".join(parts), newest


def sync(db: Session, *, force: bool = False) -> OrderSync:
    """Refresh the order list from S3. Returns the sync record either way."""
    source = f"s3://{settings.orders_s3_bucket}/{settings.orders_s3_key}"
    prev = last_sync(db)

    if not settings.s3_configured:
        rec = OrderSync(source="", ok=False, message="No S3 bucket configured.", rows=0)
        db.add(rec); db.commit(); return rec

    if not force and prev and prev.ok:
        age = (dt.datetime.utcnow() - prev.synced_at).total_seconds() / 60
        if age < settings.orders_refresh_minutes:
            return prev

    try:
        etag, lm = head()
    except CredentialsMissing as exc:
        rec = OrderSync(source=source, ok=False, message=str(exc),
                        rows=prev.rows if prev else 0)
        db.add(rec); db.commit(); return rec
    except Exception as exc:
        rec = OrderSync(source=source, ok=False, message=f"Could not reach S3: {exc}",
                        rows=prev.rows if prev else 0)
        db.add(rec); db.commit(); return rec

    if not force and prev and prev.ok and prev.etag == etag:
        prev.synced_at = dt.datetime.utcnow()          # unchanged, just touch it
        db.commit()
        return prev

    tmpdir = None
    try:
        client = _client()
        keys = _resolve_keys(client)
        # These exports run to hundreds of megabytes, so stream each one to disk
        # and parse it row by row rather than holding it in memory.
        tmpdir = tempfile.mkdtemp(prefix="orders-", dir=str(settings.data_dir))
        paths = []
        for i, k in enumerate(keys):
            dest = Path(tmpdir) / f"{i:03d}-{Path(k).name}"
            with open(dest, "wb") as fh:
                client.download_fileobj(settings.orders_s3_bucket, k, fh)
            paths.append(dest)
        result = import_orders(db, paths, filename=keys[0] if keys else "orders.csv",
                               sheet=settings.orders_s3_sheet or None, replace=True)
    except Exception as exc:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        rec = OrderSync(source=source, etag=etag, last_modified=lm, ok=False,
                        message=f"Downloaded but could not import: {exc}",
                        rows=prev.rows if prev else 0)
        db.add(rec); db.commit(); return rec

    n = result["kept"] if isinstance(result, dict) else result
    msg = f"Imported {n} order lines"
    if isinstance(result, dict):
        msg += f" from {result.get('files', 1)} file(s), {result.get('rows_read', 0):,} rows read"
        if result.get("duplicate_rows"):
            msg += f", {result['duplicate_rows']:,} duplicate rows ignored"
    rec = OrderSync(source=f"s3://{settings.orders_s3_bucket}/" + ", ".join(keys),
                    etag=etag, last_modified=lm, rows=n, ok=True, message=msg + ".",
                    guidance=(result.get("guidance") or {}) if isinstance(result, dict) else {})
    db.add(rec); db.commit()
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return rec
