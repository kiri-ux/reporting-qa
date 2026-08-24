"""Inbound email handling plus the shared 'process a set of PDFs' path."""
from __future__ import annotations

import datetime as dt
import re
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

from sqlalchemy.orm import Session

from .checks import run_all
from .checks.parser import meta_from_filename
from .config import settings
from .db import Batch, Report
from .roster import attach_owners, completeness, expected_products
from .notify import post_slack, send_digest

# Subjects and filenames name the market inconsistently: "7MOU SG",
# "7 Mountains SG", "Selinsgrove". Normalise them all to the tracker's spelling
# so completeness can match reports to order lines.
MARKET_HINTS = [
    (re.compile(r"selinsgrove|7\s*mou(?:ntains)?\s*(?:pa\s*)?sg\b", re.I),
     "7 Mountains PA Selinsgrove"),
    (re.compile(r"state\s*college|7\s*mou(?:ntains)?\s*(?:pa\s*)?sc\b", re.I),
     "7 Mountains PA State College"),
    (re.compile(r"altoona", re.I), "7 Mountains PA Altoona"),
]


def guess_market(subject: str, sender: str, filenames: list[str]) -> str:
    hay = " ".join([subject or "", sender or ""] + filenames)
    for rx, name in MARKET_HINTS:
        if rx.search(hay):
            return name
    m = re.search(r"\b(7 ?Mountains[\w \-]*)", subject or "", re.I)
    return m.group(1).strip() if m else ""


def guess_period(filenames: list[str], subject: str = "") -> str:
    from dateutil import parser as dp
    for name in filenames + [subject]:
        m = re.match(r"([A-Za-z]+ \d{4})_", Path(name).name)
        if m:
            try:
                return dp.parse("01 " + m.group(1)).strftime("%Y-%m")
            except Exception:
                pass
    today = dt.date.today().replace(day=1) - dt.timedelta(days=1)
    return today.strftime("%Y-%m")


def expand_attachments(files: list[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """Flatten zips, keep PDFs, drop macOS resource forks."""
    out: list[tuple[str, bytes]] = []
    for name, blob in files:
        low = name.lower()
        if low.endswith(".zip"):
            try:
                with zipfile.ZipFile(BytesIO(blob)) as z:
                    for info in z.infolist():
                        if info.is_dir() or "__MACOSX" in info.filename:
                            continue
                        if info.filename.lower().endswith(".pdf"):
                            out.append((Path(info.filename).name, z.read(info)))
            except zipfile.BadZipFile:
                continue
        elif low.endswith(".pdf") and not Path(name).name.startswith("._"):
            out.append((Path(name).name, blob))
    return out


def process_batch(db: Session, files: list[tuple[str, bytes]], *, source: str = "upload",
                  email_from: str = "", subject: str = "", market: str = "",
                  notify: bool = True) -> Batch:
    pdfs = expand_attachments(files)
    names = [n for n, _ in pdfs]
    batch = Batch(
        source=source, email_from=email_from, email_subject=subject,
        market=market or guess_market(subject, email_from, names),
        period=guess_period(names, subject), status="running",
    )
    db.add(batch)
    db.flush()

    store = settings.data_dir / f"batch-{batch.id}"
    store.mkdir(parents=True, exist_ok=True)

    for name, blob in pdfs:
        safe = re.sub(r"[^A-Za-z0-9._ -]", "_", name)[:180] or f"{uuid.uuid4().hex}.pdf"
        path = store / safe
        path.write_bytes(blob)
        meta_guess = meta_from_filename(name)
        exp = expected_products(db, meta_guess["client"], meta_guess["account_ids"])
        try:
            result = run_all(path, filename=name, expected_products=exp)
        except Exception as exc:
            result = {"meta": {"client": Path(name).stem, "period": batch.period,
                               "account_ids": "", "is_lifetime": False},
                      "impressions": 0, "clicks": 0, "pages": 0, "products": [], "severity": "fail",
                      "findings": [{"code": "unreadable", "severity": "fail",
                                    "title": "Report could not be read",
                                    "detail": str(exc)}]}
        meta = result["meta"]
        rep = Report(
            batch_id=batch.id, filename=name, stored_path=str(path),
            client=meta.get("client", ""), account_ids=meta.get("account_ids", ""),
            market=batch.market, period=meta.get("period") or batch.period,
            is_lifetime=bool(meta.get("is_lifetime")),
            pages=result["pages"], impressions=result["impressions"], clicks=result["clicks"],
            products=", ".join(result.get("products") or []),
            severity=result["severity"], findings=result["findings"],
        )
        db.add(rep)
        db.flush()
        attach_owners(db, rep)

    batch.status = "done"
    db.commit()
    db.refresh(batch)

    if notify:
        # refresh the order list from S3 before judging completeness, so a
        # campaign added or ended since the last batch is already reflected
        if settings.s3_configured:
            try:
                from .orders_s3 import sync as sync_orders
                sync_orders(db)
            except Exception:
                pass
        comp = completeness(db, batch.market, batch.period)
        owners = [e for r in batch.reports if r.severity in ("fail", "warn")
                  for e in (r.owner_buyer, r.owner_team) if e and "@" in e]
        post_slack(batch, comp)
        send_digest(batch, comp, extra_to=owners)
        batch.notified_at = dt.datetime.utcnow()
        db.commit()
    return batch


# ---------------------------------------------------------------- webhook shapes
def parse_mailgun(form) -> tuple[str, str, list[tuple[str, bytes]]]:
    sender = form.get("sender") or form.get("from") or ""
    subject = form.get("subject") or ""
    return sender, subject, []          # attachments pulled from UploadFile list in main.py


def parse_postmark(payload: dict) -> tuple[str, str, list[tuple[str, bytes]]]:
    import base64
    sender = payload.get("From", "")
    subject = payload.get("Subject", "")
    files = []
    for a in payload.get("Attachments", []) or []:
        try:
            files.append((a.get("Name", "attachment"), base64.b64decode(a.get("Content", ""))))
        except Exception:
            continue
    return sender, subject, files
