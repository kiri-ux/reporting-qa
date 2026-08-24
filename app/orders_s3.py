"""Pull the order list from S3.

The bucket is treated as the source of truth: every batch refreshes the list
before it runs completeness, but only downloads when the object's ETag has
changed, so a monthly batch does not re-import an unchanged file.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .config import settings
from .db import OrderSync
from .roster import import_orders


def _client():
    import boto3
    return boto3.client("s3", region_name=settings.orders_s3_region)


def last_sync(db: Session) -> OrderSync | None:
    return db.scalars(select(OrderSync).order_by(desc(OrderSync.id)).limit(1)).first()


def head() -> tuple[str, dt.datetime | None]:
    resp = _client().head_object(Bucket=settings.orders_s3_bucket, Key=settings.orders_s3_key)
    etag = (resp.get("ETag") or "").strip('"')
    lm = resp.get("LastModified")
    return etag, lm.replace(tzinfo=None) if lm else None


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
    except Exception as exc:
        rec = OrderSync(source=source, ok=False, message=f"Could not reach S3: {exc}",
                        rows=prev.rows if prev else 0)
        db.add(rec); db.commit(); return rec

    if not force and prev and prev.ok and prev.etag == etag:
        prev.synced_at = dt.datetime.utcnow()          # unchanged, just touch it
        db.commit()
        return prev

    try:
        obj = _client().get_object(Bucket=settings.orders_s3_bucket, Key=settings.orders_s3_key)
        raw = obj["Body"].read()
        n = import_orders(db, raw, filename=settings.orders_s3_key,
                          sheet=settings.orders_s3_sheet or None, replace=True)
    except Exception as exc:
        rec = OrderSync(source=source, etag=etag, last_modified=lm, ok=False,
                        message=f"Downloaded but could not import: {exc}",
                        rows=prev.rows if prev else 0)
        db.add(rec); db.commit(); return rec

    rec = OrderSync(source=source, etag=etag, last_modified=lm, rows=n, ok=True,
                    message=f"Imported {n} order lines.")
    db.add(rec); db.commit()
    return rec
