"""Pull the order list from S3.

The bucket is treated as the source of truth: every batch refreshes the list
before it runs completeness, but only downloads when the object's ETag has
changed, so a monthly batch does not re-import an unchanged file.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .config import settings
from .db import OrderSync

log = logging.getLogger("report-qa")
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


# The serving file shares this log and is not a sync of the order export. A
# serving file that would not parse was reporting itself as "last sync failed",
# about a sync nobody ran - and worse, its blank ETag became the one the next
# real sync compared against.
NOT_A_SYNC = "serving upload:%"


def last_sync(db: Session) -> OrderSync | None:
    return db.scalars(select(OrderSync)
                      .where(~OrderSync.source.like(NOT_A_SYNC))
                      .order_by(desc(OrderSync.id)).limit(1)).first()


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
    "clock": "the scheduled check - every half hour, whether or not anything "
             "is arriving",
    "sheet": "the reporting breakout sheet changed",
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


def _flat(s: str) -> str:
    return "".join(ch for ch in (s or "").lower() if ch.isalnum())


def _starts(key: str, want: str) -> bool:
    """Does this object's file name start with `want`, ignoring punctuation?

    The files arrive as "ordersdb7moupa_20260826_1508_0.csv" and
    "client-serve_20260828.csv" while the conventions are written down as
    "orders-db-" and "client-serve". A filter that reads those as different
    things is a silent empty sync waiting to happen.
    """
    if not want.strip():
        return False
    return _flat(key.rsplit("/", 1)[-1]).startswith(_flat(want))


def is_serving_file(key: str) -> bool:
    """The daily serve export, as opposed to an order export."""
    return _starts(key, settings.serving_file_prefix or "")


def _name_matches(key: str) -> bool:
    """Is this one of the order-database exports?

    The bucket holds more than the orders now, and the old rule - "every CSV
    under the prefix" - would merge whatever else lands there into the order
    list. These files are named for what they are, so that is what is matched.

    Punctuation is ignored on purpose: the files arrive as
    "ordersdb7moupa_20260826_1508_0.csv" while the naming convention is written
    down as "orders-db-", and a filter that reads those as two different things
    is a silent empty sync waiting to happen.
    """
    want = (settings.orders_file_prefix or "").strip()
    if not want:
        # Unset means take everything, as before - except the serve export,
        # which lives in the same folder and is not an order list. Merged into
        # the orders it would be 1.4 million rows of nothing recognizable.
        return not is_serving_file(key)
    return _starts(key, want)


def sweep_leftovers(older_than_minutes: int = 30) -> int:
    """Delete order downloads a previous sync abandoned. Returns bytes freed.

    Only ones older than half an hour, so a sync running right now in the other
    worker keeps its own files.
    """
    import time
    root = Path(settings.data_dir)
    cutoff = time.time() - older_than_minutes * 60
    freed = 0
    try:
        candidates = list(root.glob("orders-*"))
    except OSError:
        return 0
    for d in candidates:
        try:
            if not d.is_dir() or d.stat().st_mtime > cutoff:
                continue
            for f in d.rglob("*"):
                if f.is_file():
                    freed += f.stat().st_size
            shutil.rmtree(d, ignore_errors=True)
        except OSError:
            continue
    return freed


def disk_free() -> tuple[int, int]:
    """(bytes free, bytes total) on the data disk, or (0, 0)."""
    try:
        st = os.statvfs(str(settings.data_dir))
        return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
    except OSError:
        return 0, 0


def disk_note() -> str:
    free, total = disk_free()
    if not total:
        return "size unknown"
    return (f"{free / 1073741824:.1f} GB free of {total / 1073741824:.0f} GB "
            f"({(total - free) / total * 100:.0f}% used)")


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
                    if (key.lower().endswith(DATA_EXTS) and size > 0
                            and _name_matches(key)):
                        # NEWEST FIRST. Where two exports carry the same line
                        # item the first one read wins, so the freshest file
                        # has to be the one read first - otherwise a stale
                        # export left in the folder quietly beats the export
                        # that arrived this morning.
                        when = obj.get("LastModified")
                        out.append((-(when.timestamp() if when else 0), key))
                if not page.get("IsTruncated"):
                    break
                token = page.get("NextContinuationToken")
        else:
            out.append((0.0, k))

    if not out:
        prefix = ", ".join(settings.orders_s3_keys)
        if not seen:
            raise NothingToImport(
                f"Nothing found under s3://{settings.orders_s3_bucket}/{prefix}. "
                f"Check the folder name, and that the IAM user has s3:ListBucket "
                f"on the bucket itself, not just s3:GetObject on its contents.")
        named = f", named {settings.orders_file_prefix}*" if settings.orders_file_prefix else ""
        raise NothingToImport(
            f"Found {len(seen)} object(s) under s3://{settings.orders_s3_bucket}/{prefix} "
            f"but none are usable data files ({', '.join(DATA_EXTS)}, non-empty"
            f"{named}): "
            + "; ".join(seen[:8]) + (" ..." if len(seen) > 8 else ""))
    # Newest first, then by name so the order is stable when two files carry
    # the same timestamp.
    return [k for _when, k in _this_mornings_run(sorted(set(out)))]


# HOW FAR BACK A FILE CAN BE AND STILL BE PART OF THE SAME EXPORT.
#
# One run writes several files minutes apart - 07:32 and 07:34 on 1 September,
# 227 MB and 830 MB - so this cannot just take the newest file. But nothing
# ever deleted the older days either, and "every CSV under the prefix" meant
# every export ever dropped in that folder was downloaded and merged on every
# sync. Gigabytes an hour on a box that is already slow, and worse than slow: a
# run from three weeks ago is a picture of the orders as they were three weeks
# ago, and the merge keeps whatever line item the newest file did not happen to
# carry. Half an answer from today and half from a fortnight back, with nothing
# on screen to say which half was which.
STALE_HOURS = 12

# How many older exports the last resolve walked past, so the sync record can
# say so on screen rather than only in a log nobody reads.
_LAST_SKIPPED = [0]


def _this_mornings_run(ordered: list) -> list:
    """Keep the newest export and anything alongside it. Drop older runs.

    Entries are (-timestamp, key), newest first. A key named explicitly rather
    than found under a prefix carries no timestamp and is always kept -
    somebody asked for that file by name.
    """
    dated = [t for t in ordered if t[0] < 0]
    if not dated:
        _LAST_SKIPPED[0] = 0
        return ordered
    cutoff = -dated[0][0] - STALE_HOURS * 3600
    kept = [t for t in ordered if t[0] >= 0 or -t[0] >= cutoff]
    _LAST_SKIPPED[0] = len(ordered) - len(kept)
    if _LAST_SKIPPED[0]:
        log.info("skipped %d order export(s) more than %d hours older than the "
                 "newest one", _LAST_SKIPPED[0], STALE_HOURS)
    return kept


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
    prev = db.scalars(select(OrderSync)
                      .where(OrderSync.state != "running",
                             ~OrderSync.source.like(NOT_A_SYNC))
                      .order_by(desc(OrderSync.id)).limit(1)).first()
    try:
        # THE DAILY SERVE FILE COMES IN ON THE SAME TRIGGERS, and on its own
        # ETag - it changes every morning and the order export does not, so
        # tying them together would re-download several hundred megabytes to
        # notice a small file had moved. It cannot fail the order sync.
        try:
            sync_serving(db, force=force)
        except Exception:                                    # noqa: BLE001
            log.exception("daily serve sync failed, carrying on with orders")
        # AND THE BREAKOUT SHEET, on the same triggers and its own checksum.
        # Same rule: it cannot fail the order sync.
        try:
            from .roster_sheet import sync_roster
            sync_roster(db, force=force)
        except Exception:                                    # noqa: BLE001
            log.exception("roster sheet sync failed, carrying on with orders")
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
    # AND THE CYCLE IS PART OF THE INPUT TOO.
    #
    # The import keeps line items that touch the period being worked, so the
    # answer depends on which period that is - and nothing re-reads the export
    # when the cycle rolls over, because the file has not changed. August's
    # orders were dropped as "starts after the period" by an import that ran
    # while the board was still on July, and stayed dropped.
    from .cycle import current_period
    mapv = f"{product_map_version()}:{settings.default_period or current_period()}"
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

    # ANY DOWNLOAD A PREVIOUS SYNC LEFT BEHIND.
    #
    # The tempdir is removed on both the success and the failure path, and
    # neither runs if the process is killed - a deploy, a restart, the OOM
    # killer - which is exactly when a sync is most likely to be halfway
    # through. Every one of those leaves the whole export on the disk forever.
    #
    # It filled a 20 GB disk to 85.9%, and a full disk does not announce
    # itself: it comes back as "Downloaded but could not import: OSError
    # [Errno 28]" on the one thing that was still trying to write.
    freed = sweep_leftovers()
    if freed:
        log.info("cleared %.0f MB of abandoned order downloads", freed / 1048576)

    tmpdir = None
    try:
        client = _client()
        keys = _resolve_keys(client)
        # These exports run to hundreds of megabytes, so stream each one to disk
        # and parse it row by row rather than holding it in memory.
        tmpdir = tempfile.mkdtemp(prefix="orders-", dir=str(settings.data_dir))
        paths = []
        read_note = []
        for i, k in enumerate(keys):
            dest = Path(tmpdir) / f"{i:03d}-{Path(k).name}"
            with open(dest, "wb") as fh:
                client.download_fileobj(settings.orders_s3_bucket, k, fh)
            paths.append(dest)
            read_note.append(f"{Path(k).name} "
                             f"({dest.stat().st_size / 1048576:.0f} MB)")
        result = import_orders(db, paths, filename=keys[0] if keys else "orders.csv",
                               sheet=settings.orders_s3_sheet or None, replace=True)
    except Exception as exc:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        # SAY IT IS THE DISK, when it is the disk. "Could not import" sends
        # somebody to look at the file, and the file is fine.
        extra = ""
        if isinstance(exc, OSError) and getattr(exc, "errno", None) == 28:
            extra = (f" The disk is full - {disk_note()}. Nothing here can be "
                     f"read or written until there is room.")
        return _fail(db, source, f"Downloaded but could not import: "
                                 f"{type(exc).__name__}: {exc}.{extra}",
                     prev, etag, lm)

    n = result["kept"] if isinstance(result, dict) else result
    msg = f"Imported {n} order lines"
    if isinstance(result, dict):
        msg += f" from {result.get('files', 1)} file(s), {result.get('rows_read', 0):,} rows read"
        # WHICH FILES, AND HOW BIG. One run writes several files minutes apart
        # and they are not the same size - 227 MB then 830 MB - so "3 file(s)"
        # is not enough to tell a complete export from half of one.
        if read_note:
            msg += " - " + ", ".join(read_note[:6])
            if len(read_note) > 6:
                msg += f" and {len(read_note) - 6} more"
        if _LAST_SKIPPED[0]:
            msg += (f". {_LAST_SKIPPED[0]} older export(s) in that folder were "
                    f"not read - anything more than {STALE_HOURS} hours behind "
                    f"the newest file is a picture of a different day")
        if result.get("duplicate_rows"):
            msg += f", {result['duplicate_rows']:,} duplicate rows ignored"
        if result.get("header_overruled"):
            # Silent, this would just look like the numbers moving. It is the
            # only sign that somebody's order headers are out of date.
            msg += (f", {result['header_overruled']:,} line item(s) kept on "
                    f"their own status against an order header that disagreed")
        if result.get("order_end_is_a_window"):
            # Worth saying every time. It is the difference between "no
            # campaign ever ends" and a working lifetime list, and if the
            # export starts carrying real dates this line disappears on its
            # own - which is the sign to look for.
            msg += (". Every order in this export carries the same order end "
                    "date, so it is the range the export was pulled over "
                    "rather than any campaign's end - both order header dates "
                    "were set aside and the line items used instead")
    if remap:
        was = (prev.map_version or "").split(":")
        now = mapv.split(":")
        msg += (", re-read because the cycle moved to " + now[-1]
                if len(was) > 1 and len(now) > 1 and was[-1] != now[-1]
                else ", re-read because the product mapping changed")
    rec = OrderSync(source=(f"s3://{settings.orders_s3_bucket}/" + ", ".join(keys))[:512],
                    etag=etag[:255], last_modified=lm, rows=n, ok=True, message=msg + ".",
                    map_version=mapv, trigger=trigger,
                    guidance=(result.get("guidance") or {}) if isinstance(result, dict) else {},
                    dropped=(result.get("dropped") or {}) if isinstance(result, dict) else {})
    db.add(rec); db.commit()
    if tmpdir:
        shutil.rmtree(tmpdir, ignore_errors=True)
    return rec


# ---------------------------------------------------------- the daily serve
#
# WHAT ACTUALLY RAN, EVERY MORNING, WITHOUT ANYBODY UPLOADING IT.
#
# The serving file was a thing somebody remembered to upload, which means the
# months nobody remembered fall back to reading flight dates - and a line sold
# January to December and paused on the 2nd reads exactly like one paused on
# the 30th. Now it lands in the same bucket as the orders every morning and is
# read on the same triggers.
#
# ITS OWN ETAG, SEPARATE FROM THE ORDERS'. A serve file that changes daily
# would otherwise force a re-download of a several-hundred-megabyte order
# export every morning to notice a change in a small one.
SERVING_SOURCE = "serving upload: s3"


def serving_keys(client) -> list[str]:
    """Every daily serve export under the configured prefix."""
    out: list[str] = []
    for k in settings.orders_s3_keys:
        k = k.lstrip("/")
        if k and not k.endswith("/"):
            if is_serving_file(k):
                out.append(k)
            continue
        token = None
        while True:
            kw = {"Bucket": settings.orders_s3_bucket, "Prefix": k}
            if token:
                kw["ContinuationToken"] = token
            page = client.list_objects_v2(**kw)
            for obj in page.get("Contents", []):
                key, size = obj["Key"], obj.get("Size", 0)
                if (not key.endswith("/") and size > 0
                        and key.lower().endswith(DATA_EXTS)
                        and is_serving_file(key)):
                    out.append(key)
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
    return sorted(set(out))


def _serving_etag(client, keys: list[str]) -> str:
    h = hashlib.sha256()
    for k in keys:
        try:
            resp = client.head_object(Bucket=settings.orders_s3_bucket, Key=k)
        except Exception:                                    # noqa: BLE001
            return ""
        h.update(k.encode())
        h.update((resp.get("ETag") or "").strip('"').encode())
    return h.hexdigest()[:32]


def sync_serving(db: Session, *, force: bool = False) -> OrderSync | None:
    """Read the daily serve export from S3. Returns the record, or None.

    NEVER RAISES INTO THE ORDER SYNC. This runs alongside it, and a serve file
    that will not parse is not a reason for the order list to fail to load -
    the two answer different questions and one is not worth losing for the
    other.
    """
    if not settings.s3_configured or not (settings.serving_file_prefix or "").strip():
        return None
    prev = db.scalars(select(OrderSync).where(
        OrderSync.source.like(SERVING_SOURCE + "%"))
        .order_by(desc(OrderSync.id)).limit(1)).first()
    tmpdir = None
    try:
        client = _client()
        keys = serving_keys(client)
        if not keys:
            return prev
        etag = _serving_etag(client, keys)
        if not force and prev and prev.ok and etag and prev.etag == etag:
            return prev                       # this morning's file is already in
        tmpdir = tempfile.mkdtemp(prefix="serve-", dir=str(settings.data_dir))
        rows = []
        from .roster import _rows_from_csv, _rows_from_xlsx
        for i, k in enumerate(keys):
            dest = Path(tmpdir) / f"{i:03d}-{Path(k).name}"
            with open(dest, "wb") as fh:
                client.download_fileobj(settings.orders_s3_bucket, k, fh)
            raw = dest.read_bytes()
            part = (_rows_from_xlsx(raw, settings.orders_s3_sheet or None)
                    if k.lower().endswith((".xlsx", ".xlsm"))
                    else _rows_from_csv(raw))
            if not part:
                continue
            # One header, not one per file.
            rows.extend(part if not rows else part[1:])
        from .serving import import_serving
        # MERGED, NOT REPLACED. This file carries whatever range it carries,
        # and replacing on it would throw away every day it does not happen to
        # mention.
        res = import_serving(db, rows, period=None, merge=True)
        msg = (f"Read {res['rows_read']:,} rows from {len(keys)} daily serve "
               f"file(s), {res['clients']} client(s) across "
               f"{', '.join(res['periods'])}. Days counted on "
               f"{res['counted_on']}, and merged with what was already loaded "
               f"rather than replacing it.")
        rec = OrderSync(source=f"{SERVING_SOURCE} {Path(keys[0]).name}"[:512],
                        etag=(etag or "")[:255], rows=res["clients"], ok=True,
                        message=msg, trigger="s3")
        db.add(rec); db.commit()
        log.info("daily serve: %s", msg)
        return rec
    except Exception as exc:                                 # noqa: BLE001
        db.rollback()
        rec = OrderSync(source=SERVING_SOURCE, rows=0, ok=False,
                        message=f"Daily serve file: {type(exc).__name__}: {exc}",
                        trigger="s3")
        db.add(rec); db.commit()
        log.exception("daily serve import failed")
        return rec
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
