"""Pull the order list from S3.

The bucket is treated as the source of truth: every batch refreshes the list
before it runs completeness, but only downloads when the object's ETag has
changed, so a monthly batch does not re-import an unchanged file.
"""
from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import io
import re
import logging
import os
import shutil
from pathlib import Path

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .config import settings
from .db import OrderSync

log = logging.getLogger("report-qa")
from .roster import import_orders
from .version import map_stamp


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


DATA_EXTS = (".csv", ".tsv", ".txt", ".xlsx", ".xlsm", ".xls",
             ".csv.gz", ".tsv.gz", ".txt.gz")


class _BodyReader(io.RawIOBase):
    """A botocore StreamingBody as a raw stream TextIOWrapper can sit on."""

    def __init__(self, body):
        self._body = body

    def readable(self) -> bool:
        return True

    def readinto(self, b) -> int:
        chunk = self._body.read(len(b))
        n = len(chunk)
        b[:n] = chunk
        return n

    def close(self) -> None:
        try:
            self._body.close()
        finally:
            super().close()


class S3File:
    """One object in the bucket, read where it sits.

    NOTHING IS DOWNLOADED. Both syncs used to copy every file to the data disk
    and read it from there, and the order export alone is over a gigabyte a
    morning. A worker killed halfway left its copy behind, the serve sync's
    copies were never swept at all, and the 10 GB disk filled - after which no
    sync could start, because the first thing each did was ask for room.
    Reading the response body as it arrives needs no disk and the same memory
    the file path did, since the importers already read row by row.

    .gz is decompressed on the way through, so the exports can be written
    compressed and the transfer is a tenth of the size.
    """

    def __init__(self, client, key: str, size: int = 0):
        self.client, self.key, self.size = client, key, size
        self.name = Path(key).name

    def __str__(self) -> str:
        return self.key

    def open_binary(self):
        body = self.client.get_object(Bucket=settings.orders_s3_bucket,
                                      Key=self.key)["Body"]
        raw = io.BufferedReader(_BodyReader(body), buffer_size=1 << 20)
        if self.key.lower().endswith(".gz"):
            return gzip.GzipFile(fileobj=raw, mode="rb")
        return raw

    def open_text(self):
        return io.TextIOWrapper(self.open_binary(), encoding="utf-8-sig",
                                errors="replace", newline="")

    def read_bytes(self) -> bytes:
        with self.open_binary() as fh:
            return fh.read()

    def rows(self):
        """Every row as a list of strings, streamed."""
        if self.key.lower().endswith((".xlsx", ".xlsm", ".xls")):
            from .roster import _rows_from_xlsx
            yield from _rows_from_xlsx(self.read_bytes(),
                                       settings.orders_s3_sheet or None)
            return
        with self.open_text() as fh:
            for r in csv.reader(fh):
                yield [(c or "") for c in r]


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
    """Delete downloads a previous sync abandoned. Returns bytes freed.

    Neither sync downloads anything now, but every worker killed mid-sync
    before that left its copy behind: orders-* from the order export, serve-*
    from the daily serve files (which this never swept, and which ran to
    gigabytes once the backfills landed), and tmp*.csv from a large upload.
    Only ones older than half an hour, so a sync running right now in the
    other worker keeps its own files.
    """
    import time
    root = Path(settings.data_dir)
    cutoff = time.time() - older_than_minutes * 60
    freed = 0
    try:
        candidates = [*root.glob("orders-*"), *root.glob("serve-*"),
                      *root.glob("tmp*.csv")]
    except OSError:
        return 0
    for d in candidates:
        try:
            if d.stat().st_mtime > cutoff:
                continue
            if d.is_file():
                freed += d.stat().st_size
                d.unlink()
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
    return [k for _when, k in _latest_of_each(sorted(set(out)))]


# THE NEWEST OF EACH EXPORT, NOT THE NEWEST HOURS.
#
# Each export is its own file name with the run stamped on the end -
# orders-db-all-1_20261001_0704_0.csv, orders-db-anne_20261001_0700_0.csv - and
# every run of every export stays in the folder. Only the newest run of each
# one is the order list as it stands; the older runs are pictures of a
# different day, and merging them keeps whatever line item the newest run did
# not carry.
#
# It was a twelve-hour window around the newest file, which dropped an export
# that simply had not been re-run that morning (whitfield's newest is 21
# September) and, on a morning with two runs, read both. The trailing _0 is
# the part number: a run split across _0, _1 is read whole.
_RUN = re.compile(r"^(?P<name>.+?)_(?P<run>\d{8}_\d{4,6})(?:_(?P<part>\d+))?"
                  r"(?P<ext>\.[A-Za-z]+(?:\.gz)?)$")

# How many older runs the last resolve walked past, for the sync record.
_LAST_SKIPPED = [0]


def export_name(key: str) -> tuple[str, str]:
    """(which export, which run) for a key. A key with no run stamp is its own
    export with one run."""
    folder, _, base = key.rpartition("/")
    m = _RUN.match(base)
    if not m:
        return key, ""
    return f"{folder}/{m['name']}{m['ext'].lower()}", m["run"]


def _latest_of_each(ordered: list) -> list:
    """Keep every part of the newest run of each export. Drop older runs.

    Entries are (-timestamp, key), newest first, and stay in that order. A key
    named outright carries no timestamp and is always kept.
    """
    newest: dict[str, str] = {}
    for when, key in ordered:
        name, run = export_name(key)
        if when < 0 and name not in newest:
            newest[name] = run
    kept = [t for t in ordered
            if t[0] >= 0 or export_name(t[1])[1] == newest[export_name(t[1])[0]]]
    _LAST_SKIPPED[0] = len(ordered) - len(kept)
    if _LAST_SKIPPED[0]:
        log.info("skipped %d older run(s) of the order exports", _LAST_SKIPPED[0])
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
        freed = sweep_leftovers()
        if freed:
            log.info("cleared %.0f MB of abandoned downloads", freed / 1048576)
    except Exception:                                        # noqa: BLE001
        pass
    try:
        # THE ORDERS FIRST. The serve files and the breakout sheet come in on
        # the same triggers with their own fingerprints, and neither can fail
        # the order sync - or hold it up: the first serve sync after a backfill
        # lands is gigabytes of reading.
        result = _sync(db, source, prev, force=force, trigger=trigger)
        try:
            sync_serving(db, force=force)
        except Exception:                                    # noqa: BLE001
            log.exception("daily serve sync failed")
        try:
            from .roster_sheet import sync_roster
            sync_roster(db, force=force)
        except Exception:                                    # noqa: BLE001
            log.exception("roster sheet sync failed")
        return result
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
    mapv = map_stamp()
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

    try:
        client = _client()
        keys = _resolve_keys(client)
        files = []
        read_note = []
        for k in keys:
            try:
                size = client.head_object(
                    Bucket=settings.orders_s3_bucket, Key=k).get("ContentLength", 0)
            except Exception:                                # noqa: BLE001
                size = 0
            f = S3File(client, k, size)
            # A spreadsheet has to be whole to be opened, and they are small.
            files.append(f.read_bytes() if k.lower().endswith((".xlsx", ".xlsm", ".xls"))
                         else f)
            read_note.append(f"{f.name} ({size / 1048576:.0f} MB)")
        result = import_orders(db, files, filename=keys[0] if keys else "orders.csv",
                               sheet=settings.orders_s3_sheet or None, replace=True)
    except Exception as exc:
        return _fail(db, source, f"Could not import: {type(exc).__name__}: {exc}.",
                     prev, etag, lm)

    n = result["kept"] if isinstance(result, dict) else result
    # DID THIS EXPORT JUST LOSE HALF THE BOARD?
    #
    # The import replaces the order list outright, so a narrower export - a
    # date range someone tightened to make the files smaller, a partner's feed
    # that failed this morning - silently takes clients off the board. Nothing
    # anywhere says a number went down; the board simply has fewer rows on it,
    # which looks exactly like a quiet month.
    #
    # This cannot refuse the import: by the time the count is known the replace
    # has happened. It can refuse to be quiet about it.
    warn = ""
    was = getattr(prev, "rows", 0) or 0
    if was >= 200 and n < was * 0.75:
        warn = (f" WORTH A LOOK: this is {was - n} fewer order lines than the "
                f"last sync ({was} to {n}, down {(was - n) / was * 100:.0f}%). "
                f"That is a big drop for one morning. Either a lot of campaigns "
                f"ended at once, or this export covers less than the last one "
                f"did - a narrower date range, or a partner's file missing from "
                f"the folder.")
    # "IMPORTED 2,427 OF 213,394 ROWS" READS AS A LIMIT, AND THERE IS NO LIMIT.
    #
    # The export is at daily grain: one line item that ran all August is 31
    # rows. 213,394 rows are about 10,600 line items, of which 5,796 are RFPs
    # that were never sold and 1,838 ended before the cycle - and what is left
    # rolls up to one row per client per product. Nothing is capped and nothing
    # is dropped for volume, but the two numbers side by side look exactly like
    # a ceiling, which is a frightening thing to read about your own order list.
    msg = f"Imported {n} order lines"
    if isinstance(result, dict):
        dupes = result.get("duplicate_rows", 0)
        read = result.get("rows_read", 0)
        msg += f" from {result.get('files', 1)} file(s)"
        if read:
            items = read - dupes
            msg += (f". The export is at daily grain: {read:,} rows are "
                    f"{items:,} line items once the repeats per day are folded "
                    f"together, and those roll up to {n} rows of one client and "
                    f"one product. Nothing is capped")
        # WHICH FILES, AND HOW BIG. One run writes several files minutes apart
        # and they are not the same size - 227 MB then 830 MB - so "3 file(s)"
        # is not enough to tell a complete export from half of one.
        if read_note:
            msg += ". Files read: " + ", ".join(read_note[:6])
            if len(read_note) > 6:
                msg += f" and {len(read_note) - 6} more"
        if _LAST_SKIPPED[0]:
            msg += f". {_LAST_SKIPPED[0]} older run(s) not read"
        if result.get("months_disagree"):
            # ONE OF TWO FIELDS IS WRONG ON THAT LINE. months_running says the
            # campaign runs one length and the budgets say another, and the
            # campaign total is built on the first of them.
            msg += (f", {result['months_disagree']:,} line item(s) where the "
                    f"months on the order and the budgets disagree about how "
                    f"long the campaign runs")
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
                    etag=etag[:255], last_modified=lm, rows=n, ok=True, message=msg + "." + warn,
                    map_version=mapv, trigger=trigger,
                    guidance=(result.get("guidance") or {}) if isinstance(result, dict) else {},
                    dropped=(result.get("dropped") or {}) if isinstance(result, dict) else {},
                    dropped_orders=(result.get("dropped_orders") or {})
                    if isinstance(result, dict) else {},
                    order_statuses=(result.get("order_statuses") or {})
                    if isinstance(result, dict) else {})
    db.add(rec); db.commit()
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


def serving_keys(client) -> list[tuple[str, str, int]]:
    """Every daily serve export under the configured prefix, oldest first, as
    (key, etag, size)."""
    out: list[tuple[float, str, str, int]] = []
    for k in settings.orders_s3_keys:
        k = k.lstrip("/")
        if k and not k.endswith("/"):
            if is_serving_file(k):
                try:
                    resp = client.head_object(Bucket=settings.orders_s3_bucket, Key=k)
                except Exception:                            # noqa: BLE001
                    continue
                out.append((0.0, k, (resp.get("ETag") or "").strip('"'),
                            resp.get("ContentLength", 0)))
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
                    when = obj.get("LastModified")
                    out.append((when.timestamp() if when else 0.0, key,
                                (obj.get("ETag") or "").strip('"'), size))
            if not page.get("IsTruncated"):
                break
            token = page.get("NextContinuationToken")
    return [(k, e, n) for _w, k, e, n in sorted(set(out))]


def sync_serving(db: Session, *, force: bool = False) -> OrderSync | None:
    """Read the daily serve files from S3. Returns the latest record, or None.

    ONLY THE FILES NOT READ YET. Each file is a week or so of days, and the
    days are merged as a union, so a file once read has nothing more to add
    until it changes. Every file was re-read whenever any one of them changed,
    which after the two backfills was over three gigabytes every morning. Each
    file read leaves a record with its own key and ETag, and a file whose
    key and ETag are already on an ok record is skipped. force re-reads all.

    One record per file, committed as it goes, so a worker killed halfway
    through a backlog picks up at the next file rather than starting over.

    NEVER RAISES INTO THE ORDER SYNC.
    """
    if not settings.s3_configured or not (settings.serving_file_prefix or "").strip():
        return None

    def latest():
        return db.scalars(select(OrderSync).where(
            OrderSync.source.like(SERVING_SOURCE + "%"))
            .order_by(desc(OrderSync.id)).limit(1)).first()

    try:
        client = _client()
        found = serving_keys(client)
    except Exception as exc:                                 # noqa: BLE001
        db.rollback()
        rec = OrderSync(source=SERVING_SOURCE, rows=0, ok=False,
                        message=f"Daily serve file: {type(exc).__name__}: {exc}",
                        trigger="s3")
        db.add(rec); db.commit()
        log.exception("daily serve listing failed")
        return rec
    if not found:
        return latest()
    done = set() if force else {
        (src, etag) for src, etag in db.execute(
            select(OrderSync.source, OrderSync.etag).where(
                OrderSync.source.like(SERVING_SOURCE + " %"),
                OrderSync.ok.is_(True)))}

    from .serving import import_serving
    rec = None
    for key, etag, size in found:
        source = f"{SERVING_SOURCE} {key}"[:512]
        if (source, etag[:255]) in done:
            continue
        try:
            # MERGED, NOT REPLACED. This file carries whatever range it
            # carries, and replacing on it would throw away every day it does
            # not happen to mention.
            res = import_serving(db, S3File(client, key, size).rows(),
                                 period=None, merge=True)
            msg = (f"Read {res['rows_read']:,} rows from {Path(key).name}, "
                   f"{res['clients']} client(s) across "
                   f"{', '.join(res['periods'])}. Days counted on "
                   f"{res['counted_on']}.")
            rec = OrderSync(source=source, etag=etag[:255], rows=res["clients"],
                            ok=True, message=msg, trigger="s3")
            db.add(rec); db.commit()
            log.info("daily serve: %s", msg)
        except Exception as exc:                             # noqa: BLE001
            db.rollback()
            rec = OrderSync(source=source, rows=0, ok=False,
                            message=f"Daily serve file {Path(key).name}: "
                                    f"{type(exc).__name__}: {exc}",
                            trigger="s3")
            db.add(rec); db.commit()
            log.exception("daily serve import failed: %s", key)
    return rec or latest()
