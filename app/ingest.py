"""Inbound email handling plus the shared 'process a set of PDFs' path."""
from __future__ import annotations

import datetime as dt
import shutil
import logging
import re
import uuid
import zipfile
from io import BytesIO
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .checks import run_all
from .checks.parser import meta_from_filename
from .config import settings
from .db import Batch, KnownLogo, Report
from .notify import post_slack, send_digest
from .naming import canonical_name
from .roster import (attach_owners, client_lines, completeness, expected_any,
                     expected_products, expected_why, ordered_for,
                     quiet_products, budgets_for)

log = logging.getLogger("reportqa.ingest")

# Subjects and filenames name the market inconsistently: "7MOU SG",
# "7 Mountains SG", "Selinsgrove". Normalize them all to the tracker's spelling
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



def _rkey(client: str, accounts: str, is_lifetime: bool):
    """How one report is recognized as the same report as another."""
    from .roster import _keyify
    kind = "lifetime" if is_lifetime else "monthly"
    ids = {(a, kind) for a in _keyify(client, accounts)}
    name = re.sub(r"[^a-z0-9]", "", (client or "").lower())
    return ids, ((name, kind) if name else None)


def _index(idx: dict, rep) -> None:
    ids, name = _rkey(rep.client, rep.account_ids, rep.is_lifetime)
    for k in ids:
        idx["by_id"][k] = rep
    if name:
        idx["by_name"].setdefault(name, rep)


def _reports_for_period(db: Session, period: str) -> dict:
    """Everything already received for this period, by account id and by name.

    Newest last, so a client that has already been superseded once resolves to
    the most recent copy rather than the original.
    """
    from sqlalchemy import func, select
    idx = {"by_id": {}, "by_name": {}}
    for rep in db.scalars(select(Report).where(Report.period == period)
                          .order_by(Report.id)).all():
        _index(idx, rep)
    return idx


def _match_existing(idx: dict, meta: dict):
    """The report this one replaces, if there is one.

    Account id first - clients are typed differently in the two systems - then
    the name. A monthly never replaces a lifetime or the other way round.
    """
    ids, name = _rkey(meta.get("client", ""), meta.get("account_ids", ""),
                      bool(meta.get("is_lifetime")))
    for k in ids:
        hit = idx["by_id"].get(k)
        if hit is not None:
            return hit
    return idx["by_name"].get(name) if name else None


def open_batch(db: Session, market: str, period: str) -> Batch | None:
    """The batch these reports should join, if there is one.

    TapClicks mails one report per client, so a market's month arrives as
    eighteen separate deliveries seconds apart. Without this, every one becomes
    its own batch, its own dashboard row and its own Slack post, and the
    completeness check never sees a whole market at once. A batch stays open
    for `batch_window_minutes` and stops accepting once its digest has gone out.
    """
    from sqlalchemy import select
    cutoff = dt.datetime.utcnow() - dt.timedelta(minutes=settings.batch_window_minutes)
    return db.scalars(
        select(Batch)
        .where(Batch.market == market, Batch.period == period,
               Batch.notified_at.is_(None), Batch.received_at >= cutoff)
        .order_by(Batch.received_at.desc())
        .limit(1)
    ).first()


def client_flight(db: Session, client: str, accounts: str,
                  cutoff: dt.date | None = None,
                  period: str | None = None) -> tuple | None:
    """(first start, last end) across every one of this client's order lines.

    Two overlapping orders are one continuous campaign to the client, so the
    range a lifetime report has to cover runs from the earliest start of any
    of them to the latest end - not the bounds of whichever single order
    happened to be looked up.
    """
    out = flight_lines(db, client, accounts)
    # A CUTOFF SCOPES IT TO THE CAMPAIGN THAT ENDED. A client can have one
    # order finishing this month and another running to October, and a lifetime
    # about the first should not be checked against the second's end date.
    ends = [l["ends"] for l in out if l["ends"]
            and (cutoff is None or l["ends"] <= cutoff)]
    last = max(ends) if ends else None
    starts = [l["starts"] for l in out if l["starts"]
              and (last is None or l["starts"] <= last)]
    if not starts:
        starts = [l["starts"] for l in out if l["starts"]]
    if not starts:
        return None
    if last:
        return (min(starts), last)

    # NOTHING ENDS INSIDE THE WINDOW, AND THE CAMPAIGN IS OVER ANYWAY.
    #
    # Sorge Funeral Home's lines are all "IO Complete" and dated to 31 December
    # 2026, so the fallback below asked a lifetime pulled in July to cover a
    # range ending eighteen months from now, and failed it for not doing so.
    # A cancelled or completed campaign ended when it stopped delivering - the
    # serving file knows the day, and failing that the cycle does.
    if cutoff and out and all(l["stopped"] for l in out):
        stop = _last_served_day(db, client, period) if period else None
        return (min(starts), stop or cutoff)

    return (min(starts), max([l["ends"] for l in out if l["ends"]], default=None))


def _last_served_day(db: Session, client: str, period: str):
    """The last day this client delivered in that month, or None.

    Matched on the client name alone: the market a report belongs to and the
    business unit on the serving file are not always spelled the same, and the
    client is the part both tools agree on.
    """
    from .board import _key as _bkey
    from .serving import last_served

    want = _bkey(client)
    best = None
    for (_mk, ck), day in last_served(db, period).items():
        if ck == want and day and (best is None or day > best):
            best = day
    return best


def flight_lines(db: Session, client: str, accounts: str) -> list[dict]:
    """Every line item behind that flight, so a wrong date can be traced.

    "This is telling me the wrong lifetime end date" is not answerable from a
    pair of dates. It is answerable from the row that supplied them - which
    order, which line item, and whether it is still live - and that took a
    screenshot of the IO tool and a guess every time.
    """
    from .roster import client_lines

    out = []
    for l in client_lines(db, client, accounts) or []:
        # The order's window when the export carried it - that is what a
        # lifetime has to cover - and the line item's when it did not.
        out.append({"order": l.account_ids or "", "lines": l.line_ids or "",
                    "product": l.product or "",
                    "starts": getattr(l, "order_starts_on", None) or l.starts_on,
                    "ends": getattr(l, "order_ends_on", None) or l.ends_on,
                    "line_starts": l.starts_on, "line_ends": l.ends_on,
                    "live": bool(getattr(l, "live", True)),
                    "stopped": bool(getattr(l, "canceled", False)
                                    or getattr(l, "complete", False))})
    out.sort(key=lambda r: (r["ends"] or dt.date.min), reverse=True)
    return out


def market_from_orders(db: Session, filenames: list[str]) -> str:
    """Look the market up from the order list using the client on the filename.

    Subjects cannot be relied on. TapClicks sends "FW: Daily report - All
    Client Data" with nothing in it that names a market, and a batch filed
    under no market never joins a partner on the board. The order list already
    knows which market a client belongs to, and the filename already carries
    the client and its account ids - so ask it rather than parsing prose.
    """
    from .roster import _keyify
    from .db import OrderLine
    from sqlalchemy import select

    lines = db.scalars(select(OrderLine)).all()
    if not lines:
        return ""
    by_account: dict[str, str] = {}
    by_client: dict[str, str] = {}
    for l in lines:
        if not l.market:
            continue
        for a in _keyify(l.client, l.account_ids):
            by_account.setdefault(a, l.market)
        by_client.setdefault(re.sub(r"[^a-z0-9]", "", l.client.lower()), l.market)

    votes: dict[str, int] = {}
    for name in filenames:
        meta = meta_from_filename(name)
        hit = None
        for a in _keyify(meta["client"], meta["account_ids"]):
            hit = by_account.get(a)
            if hit:
                break
        if hit is None:
            hit = by_client.get(re.sub(r"[^a-z0-9]", "", meta["client"].lower()))
        if hit:
            votes[hit] = votes.get(hit, 0) + 1
    if not votes:
        return ""
    # One email is one client, but a zip can hold a whole market. Either way the
    # market most of the files agree on is the right answer.
    return max(votes.items(), key=lambda kv: kv[1])[0]


def process_batch(db: Session, files: list[tuple[str, bytes]], *, source: str = "upload",
                  email_from: str = "", subject: str = "", market: str = "",
                  notify: bool = True, coalesce: bool = False) -> Batch:
    pdfs = expand_attachments(files)
    names = [n for n, _ in pdfs]
    market = (market or guess_market(subject, email_from, names)
              or market_from_orders(db, names))
    period = guess_period(names, subject)

    batch = open_batch(db, market, period) if coalesce else None
    if batch is not None:
        batch.status = "running"
    else:
        batch = Batch(
            source=source, email_from=email_from, email_subject=subject,
            market=market, period=period, status="running",
        )
        db.add(batch)
    db.flush()

    store = settings.data_dir / f"batch-{batch.id}"
    store.mkdir(parents=True, exist_ok=True)

    # A RE-PULL REPLACES THE REPORT IT CORRECTS.
    #
    # The old version skipped any file whose name it had already seen, so
    # re-running a report in TapClicks and letting it come back through the
    # Zap did nothing at all - the fix was silently dropped and the board went
    # on showing the broken copy. Matching on client and kind instead means
    # the corrected report supersedes the old one in place, keeping its notes
    # and its place on the board.
    existing = _reports_for_period(db, batch.period)

    held = 0
    for name, blob in pdfs:
        safe = re.sub(r"[^A-Za-z0-9._ -]", "_", name)[:180] or f"{uuid.uuid4().hex}.pdf"
        # Land it somewhere of its own first. Coalescing puts a market's whole
        # month in one batch directory, so writing straight to the final name
        # overwrote the copy already there - including one somebody had signed
        # off, which is the exact thing the guard below exists to prevent.
        path = store / f".incoming-{uuid.uuid4().hex}.pdf"
        path.write_bytes(blob)
        meta_guess = meta_from_filename(name)
        # The name is the only thing known about the file at this point, and a
        # lifetime is judged against the whole campaign rather than the month.
        life_guess = bool(meta_guess.get("is_lifetime"))
        exp = expected_products(db, meta_guess["client"], meta_guess["account_ids"],
                                period=batch.period, lifetime=life_guess)
        why = expected_why(db, meta_guess["client"], meta_guess["account_ids"],
                           period=batch.period)
        any_of = expected_any(db, meta_guess["client"], meta_guess["account_ids"],
                              period=batch.period)
        quiet = quiet_products(db, meta_guess["client"], meta_guess["account_ids"],
                               period=batch.period, lifetime=life_guess)
        budgets = budgets_for(db, meta_guess["client"], meta_guess["account_ids"],
                              period=batch.period)

        from .recheck import _orders_current, sibling_for
        orders_ok = _orders_current(db)
        # The corner of page one, and which other markets print the same
        # mark. Worked out here rather than inside the check because it takes
        # a database question, and a check is handed facts rather than going
        # looking for them.
        from .checks.logo import header_logo_hash, is_generic
        logo = header_logo_hash(path)
        logo_bad = is_generic(db, logo)
        logo_seen = bool(db.scalar(select(func.count()).select_from(KnownLogo)))
        # A lifetime is measured against the campaign that ended, so its
        # flight stops at this cycle's lifetime window rather than at whatever
        # else the client has running later in the year.
        from .cycle import cycle_for as _cycle_for
        cut = _cycle_for(batch.period).lifetime_cutoff if life_guess else None
        flight = client_flight(db, meta_guess["client"], meta_guess["account_ids"],
                               cutoff=cut, period=batch.period)
        flines = flight_lines(db, meta_guess["client"], meta_guess["account_ids"])
        # AFTER the flight is known: a lifetime is paced against the campaign
        # it reports on, not against every order the client has.
        ordered = ordered_for(db, meta_guess["client"], meta_guess["account_ids"],
                              batch.period, lifetime=life_guess, window=flight)
        # Only the products this campaign actually ran. A lifetime covering
        # Dec to July is not owed a Display line that starts on 28 July.
        exp = expected_products(db, meta_guess["client"], meta_guess["account_ids"],
                                period=batch.period, lifetime=life_guess,
                                window=flight)
        try:
            result = run_all(path, filename=name,
                             for_client=meta_guess["client"]
                             if meta_guess.get("named") else "",
                             expected_products=exp,
                             flight=flight, flight_lines=flines,
                             period=batch.period,
                             market=batch.market or "",
                     expected_why=why, expected_any=any_of,
                     quiet_products=quiet,
                     logo_hash=logo, logo_generic=logo_bad,
                     logo_known=logo_seen, budgets=budgets, ordered=ordered,
                     orders_current=orders_ok,
                     # The other half of the pair, if this client is getting
                     # both. Looked up from what is known - the row this is
                     # about does not exist yet.
                     sibling=sibling_for(db, meta_guess["client"], batch.period,
                                         batch.market or "", life_guess))
        except Exception as exc:
            result = {"meta": {"client": Path(name).stem, "period": batch.period,
                               "account_ids": "", "is_lifetime": False},
                      "impressions": 0, "clicks": 0, "pages": 0, "products": [], "severity": "fail",
                      "findings": [{"code": "unreadable", "severity": "fail",
                                    "title": "Report could not be read",
                                    "detail": str(exc)}],
                      "checks": []}
        meta = result["meta"]
        rep = _match_existing(existing, meta)

        # A copy somebody signed off, or put there by hand, is not overwritten
        # by the feed turning up later with its own version. The new file is
        # kept beside it and somebody says which one is real. Silently
        # replacing a reviewed report means the sign-off now belongs to a file
        # nobody has read.
        if rep is not None and rep.protected:
            if rep.pending_path and rep.pending_path != str(path):
                # An older waiting copy is superseded by this one - the queue
                # is one deep, and the newest arrival is the one worth judging.
                try:
                    Path(rep.pending_path).unlink()
                except OSError:
                    pass
            waiting = store / f"waiting-{rep.id}-{safe}"
            path.replace(waiting)
            rep.pending_path = str(waiting)
            rep.pending_name = name
            rep.pending_at = dt.datetime.utcnow()
            db.flush()
            log.info("held a newer file for report %s (%s)", rep.id, rep.protected)
            held += 1
            continue

        replaced = rep is not None
        if rep is None:
            rep = Report(batch_id=batch.id, period=meta.get("period") or batch.period)
            db.add(rep)
        else:
            # It moves to this batch, so the board and the digest both show it
            # against the delivery that actually corrected it.
            rep.batch_id = batch.id
            # THE SIGN-OFF GOES. THE TICKS DO NOT.
            #
            # A sign-off is a statement about a particular file, so a new file
            # needs a new one. An acceptance is a statement about a FINDING -
            # "that CTR flag is a known quirk of this client's GA page" - and
            # it is just as true of the re-pull as it was of the copy before
            # it. Clearing them meant re-ticking the same six boxes on the
            # same six false alarms every time a report came round again,
            # which is how somebody learns to stop reading them.
            #
            # Carried by what the finding SAYS, not by its position in the
            # list: a finding that is gone from the new file drops its tick,
            # and one that has moved down the list keeps it.
            _old_findings = list(rep.findings or [])
            _old_acked = list(rep.acked or [])
            rep.review_state = "new"
            rep.reviewed_at = None
            log.info("superseded report %s for %s %s", rep.id, rep.client, rep.period)

        # Now it can take its real name: either this is a new report, or it is
        # superseding one nobody had claimed.
        final = store / safe
        if path != final:
            path.replace(final)
            path = final
        rep.filename = name
        rep.stored_path = str(path)
        rep.client = meta.get("client", "") or rep.client
        rep.account_ids = meta.get("account_ids", "") or rep.account_ids
        rep.market = batch.market or rep.market
        rep.period = meta.get("period") or batch.period
        rep.is_lifetime = bool(meta.get("is_lifetime"))
        rep.pages = result["pages"]
        rep.impressions = result["impressions"]
        rep.clicks = result["clicks"]
        rep.products = ", ".join(result.get("products") or [])
        rep.severity = result["severity"]
        rep.findings = result["findings"]
        rep.checks = result.get("checks") or []
        if replaced:
            from .recheck import remap_acks
            rep.acked = remap_acks(_old_findings, _old_acked, rep.findings)
        # Stamped with the code that judged it, so a later deploy knows whether
        # this answer is still the current one.
        from .version import rules_version
        rep.rules_version = rules_version()
        rep.logo_hash = logo
        db.flush()
        attach_owners(db, rep)
        # THE NAME IS BUILT, and only now - attach_owners has just matched this
        # report to its order line, which is where the order ids come from when
        # the file arrived without them. A hand-pulled "Digital Marketing
        # Report.pdf" becomes "July 2026_All Seasons Powersports 53908.pdf",
        # which is what the board, the zip and the partner's folder all file by.
        # EVERY ORDER THE REPORT COVERS, not just the one it was filed under.
        # This used to run only when the file arrived with no ids at all, so a
        # report whose name carried one of its three orders kept that name -
        # which is worse than carrying none, because it looks complete.
        from .naming import ids_for_report
        ids = ids_for_report(db, rep)
        if ids:
            rep.account_ids = ids
        # RENAMED, AND WHY IT MATTERS. A file that arrived without its order
        # id - or with the wrong one - came out of a folder somebody put
        # together by hand, and that is worth seeing on the report rather than
        # being quietly corrected. The name it arrived under is kept.
        built = canonical_name(rep)
        arrived = (name or "").strip()
        if built != arrived:
            from .checks.parser import meta_from_filename as _mfn
            was = _mfn(arrived)
            mine = {i for i in (rep.account_ids or "").replace(",", " ").split()}
            theirs = set((was.get("account_ids") or "").split())
            if mine and theirs != mine:
                rep.renamed_from = arrived[:512]
        rep.filename = built
        db.flush()
        if not replaced:
            _index(existing, rep)

    if not batch.market:
        # attach_owners stamps a market onto each report from its order line,
        # so by now the batch can borrow it even when nothing else knew.
        found: dict[str, int] = {}
        for r in batch.reports:
            if r.market:
                found[r.market] = found.get(r.market, 0) + 1
        if found:
            batch.market = max(found.items(), key=lambda kv: kv[1])[0]

    batch.status = "done"
    batch.last_report_at = dt.datetime.utcnow()
    db.commit()
    db.refresh(batch)

    if notify:
        finish_batch(db, batch.id)
    return batch


def prune_old_pdfs(db: Session) -> dict:
    """Delete stored PDFs past the retention window, keep every check result.

    A cycle is roughly 1.6 GB of PDFs. Left alone that fills a 10 GB disk in
    six months, and a full disk does not announce itself - it shows up as
    unrelated write failures somewhere else entirely. The Report rows, their
    findings and every sign-off stay; only the file goes, and the archive in
    the shared drive is the copy that matters by then.
    """
    from sqlalchemy import select
    months = settings.keep_pdf_months
    if months <= 0:
        return {"freed": 0, "files": 0}

    today = dt.date.today()
    y, m = today.year, today.month - months
    while m <= 0:
        y, m = y - 1, m + 12
    cutoff = f"{y:04d}-{m:02d}"

    freed = files = 0
    rows = db.scalars(select(Report).where(Report.stored_path != "",
                                           Report.period < cutoff)).all()
    for r in rows:
        p = Path(r.stored_path)
        try:
            if p.exists():
                freed += p.stat().st_size
                p.unlink()
                files += 1
        except OSError:
            continue
        r.stored_path = ""
    if files:
        db.commit()
        log.info("pruned %d report PDFs before %s, freed %.1f MB",
                 files, cutoff, freed / 1048576)
    # empty batch directories left behind
    for d in sorted((settings.data_dir).glob("batch-*")):
        try:
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass

    # AND THE PACKAGED ZIPS. One is built per partner per package, and "sync
    # reports" builds another - so a cycle that gets re-packaged a few times
    # leaves several copies of every partner's reports on the disk, on top of
    # the reports themselves. They are a download of something that has already
    # gone to the partner's folder; past the retention window they are two
    # copies of a copy.
    zips = settings.data_dir / "deliveries"
    if zips.is_dir():
        for d in sorted(zips.iterdir()):
            if not d.is_dir() or d.name >= cutoff:
                continue
            for f in d.rglob("*"):
                try:
                    if f.is_file():
                        freed += f.stat().st_size
                        files += 1
                except OSError:
                    continue
            shutil.rmtree(d, ignore_errors=True)

    # Anything an interrupted order sync left behind - see orders_s3.
    try:
        from .orders_s3 import sweep_leftovers
        freed += sweep_leftovers()
    except Exception:                     # noqa: BLE001
        pass
    return {"freed": freed, "files": files, "cutoff": cutoff}


def sweep_stale(db: Session) -> int:
    """Notify any batch that has gone quiet but never got its digest.

    The quiet timer lives in a background task, so a deploy or a worker restart
    mid-delivery would otherwise leave a batch silently un-notified forever.
    Cheap enough to run on a page load.
    """
    from sqlalchemy import select
    quiet = dt.datetime.utcnow() - dt.timedelta(minutes=settings.batch_quiet_minutes)
    stale = db.scalars(
        select(Batch).where(Batch.notified_at.is_(None), Batch.status == "done",
                            Batch.last_report_at.is_not(None),
                            Batch.last_report_at < quiet).limit(5)
    ).all()
    for b in stale:
        try:
            finish_batch(db, b.id, respect_quiet=False)
        except Exception:  # noqa: BLE001 - a page load must never 500 over this
            db.rollback()
    return len(stale)


def finish_batch(db: Session, batch_id: int, *, respect_quiet: bool = False) -> None:
    """Refresh the order list, judge completeness, then notify.

    Split out from process_batch so the inbound webhook can answer first. The
    order export runs to hundreds of megabytes, and downloading it should not
    hold a mail provider's connection open.
    """
    batch = db.get(Batch, batch_id)
    if batch is None or batch.notified_at:
        return
    if respect_quiet and batch.last_report_at:
        # Something arrived while this timer was asleep, so the delivery is
        # still in progress. That report scheduled its own timer, which will
        # be the one to send. Doing nothing here is what debounces the digest.
        quiet_for = (dt.datetime.utcnow() - batch.last_report_at).total_seconds() / 60
        if quiet_for < settings.batch_quiet_minutes:
            return
    if settings.s3_configured:
        try:
            from .orders_s3 import sync as sync_orders
            sync_orders(db, trigger="batch")
        except Exception:
            pass
    comp = completeness(db, batch.market, batch.period)
    owners = [e for r in batch.reports if r.severity in ("fail", "warn")
              for e in (r.owner_buyer, r.owner_team) if e and "@" in e]
    post_slack(batch, comp)
    send_digest(batch, comp, extra_to=owners)
    batch.notified_at = dt.datetime.utcnow()
    db.commit()


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
