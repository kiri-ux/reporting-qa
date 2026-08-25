"""Pull the order list from S3.

The bucket is treated as the source of truth: every batch refreshes the list
before it runs completeness, but only downloads when the object's ETag has
changed, so a monthly batch does not re-import an unchanged file.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .config import settings
from .db import OrderSync
from .roster import import_orders
from .version import product_map_version


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


# A sync that has been "running" longer than this is assumed dead - a deploy
# or a restart mid-import - and stops blocking the next attempt.
STALE_RUN_MINUTES = 30


def running_sync(db: Session) -> OrderSync | None:
    rec = db.scalars(select(OrderSync).where(OrderSync.state == "running")
                     .order_by(desc(OrderSync.id)).limit(1)).first()
    if rec is None:
        return None
    started = rec.started_at or rec.synced_at
    if (dt.datetime.utcnow() - started).total_seconds() > STALE_RUN_MINUTES * 60:
        rec.state = "done"
        rec.ok = False
        rec.message = ("Interrupted - the service restarted while this sync was "
                       "running. Nothing was lost; run it again.")
        db.commit()
        return None
    return rec


# What started a sync, in the words the page shows.
TRIGGERS = {
    "button": "you pressed the button",
    "rules": "the import rules changed on this deploy, so the loaded orders "
             "were answering from an older version of them",
    "batch": "a batch of reports arrived, and the order list is refreshed "
             "first so a campaign added or ended since the last run is there",
}


def begin_sync(db: Session, trigger: str = "") -> OrderSync | None:
    """Claim the sync. Returns None if one is already in flight.

    The claim is a row rather than an in-process flag because there are two
    gunicorn workers, and a lock one of them holds means nothing to the other.
    """
    if running_sync(db) is not None:
        return None
    now = dt.datetime.utcnow()
    rec = OrderSync(source=f"s3://{settings.orders_s3_bucket}/{settings.orders_s3_key}",
                    state="running", started_at=now, synced_at=now, ok=True,
                    trigger=trigger,
                    message="Downloading and parsing the export...")
    db.add(rec)
    db.commit()
    return rec


DATA_EXTS = (".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls")


class NothingToImport(RuntimeError):
    pass


def _resolve_keys(client) -> list[str]:
    """A key ending in / is treated as a prefix, so several exports can be
    dropped in one folder and merged. Raises with what it did find when the
    prefix turns up no data files, since an empty list is otherwise silent."""
    out: list[str] = []
    seen: list[str] = []                   # everything under the prefix, for the error
    for k in settings.orders_s3_keys:
        k = k.lstrip("/")                  # "/orders/" and "orders/" are the same place
        if k == "" or k.endswith("/"):     # "" or "/" means the whole bucket
            token = None
            while True:
                kw = {"Bucket": settings.orders_s3_bucket, "Prefix": k}
                if token:
                    kw["ContinuationToken"] = token
                page = client.list_objects_v2(**kw)
                for obj in page.get("Contents", []):
                    key, size = obj["Key"], obj.get("Size", 0)
                    if key.endswith("/"):          # console folder marker
                        continue
                    seen.append(f"{key} ({size:,} bytes)")
                    if key.lower().endswith(DATA_EXTS) and size > 0:
                        out.append(key)
                if not page.get("IsTruncated"):
                    break
                token = page.get("NextContinuationToken")
        else:
            out.append(k)

    if not out:
        prefix = ", ".join(settings.orders_s3_keys)
        if not seen:
            raise NothingToImport(
                f"Nothing found under s3://{settings.orders_s3_bucket}/{prefix}. "
                f"Check the folder name, and that the IAM user has s3:ListBucket "
                f"on the bucket itself, not just s3:GetObject on its contents.")
        raise NothingToImport(
            f"Found {len(seen)} object(s) under s3://{settings.orders_s3_bucket}/{prefix} "
            f"but none are usable data files ({', '.join(DATA_EXTS)}, non-empty): "
            + "; ".join(seen[:8]) + (" ..." if len(seen) > 8 else ""))
    return sorted(set(out))


def head() -> tuple[str, dt.datetime | None]:
    """Combined fingerprint across every key, so adding a file counts as a change.

    HASHED, not concatenated. The obvious version joined `key:etag` for every
    object, which is ~67 characters each - five files overflowed the 255-char
    column and Postgres rejected the insert inside an exception handler, so the
    whole request 500'd with the real cause nowhere on screen. SQLite does not
    enforce VARCHAR length, which is why every local test passed. A digest is
    fixed width whatever the folder holds, and comparing it is all this is for.
    """
    client = _client()
    parts, newest = [], None
    for key in _resolve_keys(client):
        resp = client.head_object(Bucket=settings.orders_s3_bucket, Key=key)
        parts.append(key + ":" + (resp.get("ETag") or "").strip('"'))
        lm = resp.get("LastModified")
        if lm and (newest is None or lm.replace(tzinfo=None) > newest):
            newest = lm.replace(tzinfo=None)
    digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
    return f"{len(parts)}f-{digest[:40]}", newest


def _close(db: Session, claim_id: int | None) -> None:
    if claim_id is None:
        return
    rec = db.get(OrderSync, claim_id)
    if rec is not None and rec.state == "running":
        db.delete(rec)                  # the claim itself is not history
        db.commit()


def _fail(db: Session, source: str, message: str, prev: OrderSync | None,
          etag: str = "", lm: dt.datetime | None = None) -> OrderSync:
    """Record a failed sync.

    The rollback matters: once a statement errors, Postgres aborts the whole
    transaction and every later statement in it fails too. Without this, the
    attempt to save the error message raises its own error and the caller gets
    a bare 500 instead of the explanation.
    """
    db.rollback()
    rec = OrderSync(source=source[:512], etag=etag[:255], last_modified=lm, ok=False,
                    message=message, rows=prev.rows if prev else 0)
    db.add(rec)
    db.commit()
    return rec


def sync(db: Session, *, force: bool = False, claim_id: int | None = None,
         trigger: str = "") -> OrderSync:
    """Refresh the order list from S3. Returns the sync record either way."""
    source = f"s3://{settings.orders_s3_bucket}/{settings.orders_s3_key}"
    prev = db.scalars(select(OrderSync).where(OrderSync.state != "running")
                      .order_by(desc(OrderSync.id)).limit(1)).first()
    try:
        return _sync(db, source, prev, force=force, trigger=trigger)
    finally:
        _close(db, claim_id)


def _sync(db: Session, source: str, prev: OrderSync | None, *,
          force: bool = False, trigger: str = "") -> OrderSync:

    if not settings.s3_configured:
        return _fail(db, "", "No S3 bucket configured.", None)

    # "UNCHANGED" IS ABOUT THE ANSWER, NOT THE FILE.
    #
    # The export is not stored; every line item is mapped to a product on the
    # way in and only the product is kept. So when the mapping code is fixed,
    # the orders already loaded still carry the old answer - and the ETag test
    # below, doing exactly what it was built to do, means the file is never
    # read again to correct them. A live TikTok order sat on the board as a
    # Video order for that reason. The mapping is part of the input.
    mapv = product_map_version()
    remap = bool(prev and prev.ok and (prev.map_version or "") != mapv)

    if not force and not remap and prev and prev.ok:
        age = (dt.datetime.utcnow() - prev.synced_at).total_seconds() / 60
        if age < settings.orders_refresh_minutes:
            return prev

    try:
        etag, lm = head()
    except (CredentialsMissing, NothingToImport) as exc:
        return _fail(db, source, str(exc), prev)
    except Exception as exc:
        return _fail(db, source, f"Could not reach S3: {exc}", prev)

    if not force and not remap and prev and prev.ok and prev.etag == etag:
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
        return _fail(db, source, f"Downloaded but could not import: "
                                 f"{type(exc).__name__}: {exc}", prev, etag, lm)

    n = result["kept"] if isinstance(result, dict) else result
    msg = f"Imported {n} order lines"
    if isinstance(result, dict):
        msg += f" from {result.get('files', 1)} file(s), {result.get('rows_read', 0):,} rows read"
        if result.get("duplicate_rows"):
            msg += f", {result['duplicate_rows']:,} duplicate rows ignored"
        if result.get("header_overruled"):
            # Silent, this would just look like the numbers moving. It is the
            # only sign that somebody's order headers are out of date.
            msg += (f", {result['header_overruled']:,} line item(s) kept on "
                    f"their own status against an order header that disagreed")
    if remap:
        msg += ", re-read because the product mapping changed"
    rec = OrderSync(source=(f"s3://{settings.orders_s3_bucket}/" + ", ".join(keys))[:512],
                    etag=etag[:255], last_modified=lm, rows=n, ok=True, message=msg + ".",
                    map_version=mapv, trigger=trigger,
                    guidance=(result.get("guidance") or {}) if isinstance(result, dict) else {})
    db.add(rec); db.commit()
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return rec
