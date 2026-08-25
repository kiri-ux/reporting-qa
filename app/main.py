from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form, HTTPException,
                     Query, Request, UploadFile)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from . import brand, version
from .config import settings
from .db import (Batch, Inbound, OrderLine, OrderSync, Partner, Report,
                 SessionLocal, init_db)
from .ingest import (finish_batch, parse_postmark, process_batch,
                    prune_old_pdfs, sweep_stale)
from .orders_s3 import last_sync, sync as sync_orders
from .roster import completeness, import_orders

app = FastAPI(title="Report QA")

_HERE = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

templates = Jinja2Templates(directory=str(_HERE / "templates"))


def _eastern(value, fmt: str = "%b %-d at %-I:%M %p"):
    """Show a stored time where the people reading it actually are.

    Everything is stored in UTC, which is right, and shown in UTC, which was
    not - "reviewed at 03:18" for something done at eleven o'clock the previous
    evening reads as a different day's work. Naive timestamps are treated as
    UTC because that is what the database holds.
    """
    if not value:
        return ""
    from datetime import timezone
    from zoneinfo import ZoneInfo
    if getattr(value, "tzinfo", None) is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo("America/New_York")).strftime(fmt) + " ET"


templates.env.filters["et"] = _eastern
# Chrome that every page needs and no view should have to remember to pass.
templates.env.globals.update(
    head_tags=brand.HEAD_TAGS,
    build_label=version.label(),
    build_notes=version.BUILD_NOTES,
    build_service=version.service(),
    nav="",
)



# Test and placeholder partners that should never reach a dashboard, a count or
# a delivery. Matched case-insensitively on the whole name.
EXCLUDED_PARTNERS = {"dummy partner", "test partner", "test", "zzz test"}


def _excluded(market: str) -> bool:
    return (market or "").strip().lower() in EXCLUDED_PARTNERS


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.on_event("startup")
def _startup():
    init_db()
    # The roster ships in the repo, so a fresh deploy has owners and
    # recipients without anyone importing anything.
    db = SessionLocal()
    try:
        from .partners import seed_if_empty
        seed_if_empty(db)
    except Exception:
        import traceback; traceback.print_exc(); db.rollback()
    finally:
        db.close()


@app.get("/healthz")
def healthz():
    """Includes the build so you can confirm what is actually live without
    trusting the dashboard, which looks identical either way."""
    return {"ok": True, **version.info()}


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return Response(content=brand.FAVICON_SVG, media_type="image/svg+xml")


def _finish(batch_id: int) -> None:
    """Runs after the webhook has already answered."""
    db = SessionLocal()
    try:
        finish_batch(db, batch_id)
    finally:
        db.close()


async def _finish_when_quiet(batch_id: int) -> None:
    """Wait out the quiet period, then send the digest if nothing else landed.

    Reports arrive one email per client, so every one of them schedules one of
    these. All but the last find a newer report and return without sending;
    the last one finds silence and sends. That is the whole debounce - no
    scheduler, no extra service, and a lost timer is caught by sweep_stale on
    the next page load.
    """
    import asyncio
    await asyncio.sleep(settings.batch_quiet_minutes * 60)
    db = SessionLocal()
    try:
        finish_batch(db, batch_id, respect_quiet=True)
    except Exception:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        db.rollback()
    finally:
        db.close()



def _log_inbound(db: Session, *, source: str, sender: str, subject: str,
                 files: list, accepted: bool, outcome: str,
                 batch: Batch | None = None) -> None:
    """Record the attempt. Never let logging be the thing that fails a
    webhook - a sender that gets a 500 will retry, and a retry storm is worse
    than a missing log line."""
    try:
        db.add(Inbound(
            source=source, sender=sender[:255], subject=subject[:512],
            filenames=", ".join(n for n, _ in files)[:4000], files=len(files),
            bytes=sum(len(b) for _, b in files), accepted=accepted,
            outcome=outcome, batch_id=batch.id if batch else None,
            market=batch.market if batch else "", period=batch.period if batch else ""))
        db.commit()
    except Exception:  # noqa: BLE001
        import traceback; traceback.print_exc(); db.rollback()


def _key_matches(sent: str | None) -> bool:
    """Compare the inbound key, tolerating what a URL does to it.

    A query string decodes "+" as a SPACE - that is form-encoding semantics,
    and it applies to the query part of a URL whether or not anyone intended
    it. Render's generated secrets are base64, which contains "+" about half
    the time, so the value arrives the right length with the right last four
    characters and simply is not the same string. Undoing that here costs
    nothing: a real secret never contains a space.
    """
    if sent is None:
        return False
    want = settings.inbound_secret
    for candidate in (sent, sent.strip(), sent.replace(" ", "+"),
                      sent.strip().replace(" ", "+")):
        if candidate == want or candidate == want.strip():
            return True
    return False


def _guard(k: str | None, db: Session | None = None, source: str = "",
           request: Request | None = None) -> None:
    """Check the shared secret on the inbound URL, or the X-Inbound-Key header.

    The header is there because it is not query-encoded, so a secret with "+"
    or "/" in it needs no escaping at all.
    """
    if request is not None and _key_matches(request.headers.get("x-inbound-key")):
        return
    if _key_matches(k):
        return

    want = settings.inbound_secret
    got = "nothing" if not k else f"{len(k)} characters ending {k[-4:]!r}"
    detail = (f"The ?k= value on the URL does not match INBOUND_SECRET. "
              f"Render holds {len(want)} characters ending {want[-4:]!r}; "
              f"the request sent {got}.")
    if k and len(k) == len(want) and " " in k and "+" in want:
        detail += (" They are the same length because the secret contains '+', "
                   "which a URL turns into a space. Replace every '+' with "
                   "'%2B' in the webhook URL.")
    if db is not None:
        _log_inbound(db, source=source, sender="", subject="", files=[],
                     accepted=False, outcome=detail)
    raise HTTPException(status_code=403, detail=detail)


# ---------------------------------------------------------------- inbound email
@app.post("/inbound/mailgun")
async def inbound_mailgun(request: Request, background: BackgroundTasks,
                          k: str | None = Query(None), db: Session = Depends(get_db)):
    _guard(k, db, "mailgun", request)
    form = await request.form()
    sender = str(form.get("sender") or form.get("from") or "")
    subject = str(form.get("subject") or "")
    files: list[tuple[str, bytes]] = []
    for key, value in form.multi_items():
        if hasattr(value, "filename") and value.filename:
            files.append((value.filename, await value.read()))
    if not files:
        _log_inbound(db, source="mailgun", sender=sender, subject=subject, files=[],
                     accepted=False, outcome="Email had no attachments.")
        return {"ok": True, "skipped": "no attachments"}
    batch = process_batch(db, files, source="mailgun", email_from=sender,
                          subject=subject, notify=False, coalesce=True)
    _log_inbound(db, source="mailgun", sender=sender, subject=subject, files=files,
                 accepted=True, outcome=f"Filed under {batch.market or 'no market'} "
                                        f"for {batch.period}.", batch=batch)
    background.add_task(_finish_when_quiet, batch.id)
    return {"ok": True, "batch": batch.id, "reports": len(batch.reports)}


@app.post("/inbound/zapier")
async def inbound_zapier(request: Request, background: BackgroundTasks,
                         k: str | None = Query(None), db: Session = Depends(get_db)):
    """Zapier's "Webhooks by Zapier - POST" action, Payload Type = Form.

    Any form field carrying a file is treated as an attachment, because Zapier
    names that field whatever you called it in the Zap. `subject` and `from`
    are optional and only help guess the market; the filename is what actually
    identifies the client.

    Reports coalesce into one batch per market per month - see open_batch.
    """
    _guard(k, db, "zapier", request)   # reject before reading the upload, not after
    form = await request.form()
    sender = str(form.get("from") or form.get("sender") or form.get("email") or "")
    subject = str(form.get("subject") or "")
    market = str(form.get("market") or "")
    files: list[tuple[str, bytes]] = []
    for _key, value in form.multi_items():
        if hasattr(value, "filename") and value.filename:
            files.append((value.filename, await value.read()))
    if not files:
        _log_inbound(db, source="zapier", sender=sender, subject=subject, files=[],
                     accepted=False, outcome="No file on the request. In the Zap, set "
                                             "Payload Type to Form and map the attachment "
                                             "into the File field.")
        return {"ok": True, "skipped": "no file on the request",
                "hint": "In the Zap, set Payload Type to Form and map the "
                        "attachment into the File field."}
    pdfs = [n for n, _ in files if n.lower().endswith((".pdf", ".zip"))]
    if not pdfs:
        _log_inbound(db, source="zapier", sender=sender, subject=subject, files=files,
                     accepted=False,
                     outcome="Attachment is not a PDF or a zip, so nothing was checked.")
        return {"ok": True, "skipped": "no pdf attachment",
                "got": [n for n, _ in files]}
    batch = process_batch(db, files, source="zapier", email_from=sender,
                          subject=subject, market=market, notify=False, coalesce=True)
    _log_inbound(db, source="zapier", sender=sender, subject=subject, files=files,
                 accepted=True, outcome=f"Filed under {batch.market or 'no market'} "
                                        f"for {batch.period}.", batch=batch)
    background.add_task(_finish_when_quiet, batch.id)
    return {"ok": True, "batch": batch.id, "market": batch.market,
            "period": batch.period, "reports": len(batch.reports)}


@app.post("/inbound/postmark")
async def inbound_postmark(request: Request, background: BackgroundTasks,
                           k: str | None = Query(None), db: Session = Depends(get_db)):
    _guard(k)
    payload = await request.json()
    sender, subject, files = parse_postmark(payload)
    if not files:
        return {"ok": True, "skipped": "no attachments"}
    batch = process_batch(db, files, source="postmark", email_from=sender,
                          subject=subject, notify=False, coalesce=True)
    background.add_task(_finish_when_quiet, batch.id)
    return {"ok": True, "batch": batch.id, "reports": len(batch.reports)}


# ---------------------------------------------------------------- dashboard
@app.get("/batches", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    sweep_stale(db)
    batches = db.scalars(select(Batch).order_by(desc(Batch.received_at)).limit(40)).all()
    latest = batches[0] if batches else None
    comp = completeness(db, latest.market, latest.period) if latest else None
    return templates.TemplateResponse(request, "dashboard.html", {
        "batches": batches, "latest": latest, "comp": comp, "nav": "batches",
        "orders": db.query(OrderLine).count(),
    })


@app.get("/batch/{batch_id}", response_class=HTMLResponse)
def batch_view(batch_id: int, request: Request, db: Session = Depends(get_db)):
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404)
    comp = completeness(db, batch.market, batch.period)
    order = {"fail": 0, "warn": 1, "pass": 2}
    reports = sorted(batch.reports, key=lambda r: (order.get(r.severity, 3), r.client.lower()))
    return templates.TemplateResponse(request, "batch.html", {
        "batch": batch, "reports": reports, "comp": comp, "nav": "dash"})


@app.get("/report/{report_id}/file")
def report_file(report_id: int, db: Session = Depends(get_db)):
    rep = db.get(Report, report_id)
    if not rep or not rep.stored_path or not Path(rep.stored_path).exists():
        raise HTTPException(404)
    # inline, not attachment: these get looked at far more often than saved
    return FileResponse(rep.stored_path, media_type="application/pdf",
                        headers={"Content-Disposition":
                                 f'inline; filename="{rep.filename}"'})


# ---------------------------------------------------------------- manual paths
@app.post("/upload")
async def upload(files: list[UploadFile] = File(...), market: str = Form(""),
                 db: Session = Depends(get_db)):
    payload = [(f.filename or "file.pdf", await f.read()) for f in files]
    batch = process_batch(db, payload, source="upload", market=market, subject=market)
    return RedirectResponse(f"/batch/{batch.id}", status_code=303)


def _client_rollup(db: Session, lines: list[OrderLine]) -> list[dict]:
    """One row per client, with a chip per product.

    A client with nine line items is nine rows in the raw list and one thing
    to check in real life, which is what makes the raw list hard to read. The
    flight is the widest across that client's products, since that is the
    window a report has to cover.
    """
    from .product_codes import pill
    from .partners import find as find_partner, resolve_owner

    by: dict[tuple[str, str], dict] = {}
    partner_cache: dict[str, Partner | None] = {}
    for l in lines:
        key = (l.market, l.client)
        row = by.get(key)
        if row is None:
            if l.market not in partner_cache:
                partner_cache[l.market] = find_partner(db, l.market)
            p = partner_cache[l.market]
            row = by[key] = {
                "partner": l.market, "client": l.client, "orders": set(), "lines": set(),
                "products": [], "codes": set(), "starts": None, "ends": None,
                "buyers": [], "reporter": p.reporting_team if p else "",
                "trainer": p.trainer if p else "", "_partner": p,
                "in_roster": p is not None,
                "lifetime": False,
            }
        pl = pill(l.product)
        if pl["code"] and pl["code"] not in row["codes"]:
            row["codes"].add(pl["code"])
            row["products"].append(pl)
        if l.account_ids:
            row["orders"].add(l.account_ids)
        for lid in (l.line_ids or "").split(","):
            if lid.strip():
                row["lines"].add(lid.strip())
        # RESOLVE THE BUYER HERE, not only at import.
        #
        # The stored buyer is whatever the export's campaign manager said at
        # import time, so every line loaded before the roster fallback existed
        # has an empty one - and re-importing 850 MB to fill in a name nobody
        # changed is the wrong fix. Reporter and trainer already came from the
        # roster on every render; the buyer now does too, so the column repairs
        # itself and stays right when the roster is updated.
        who = l.buyer or resolve_owner(row["_partner"], l.product)[0]
        if who and who not in row["buyers"]:
            row["buyers"].append(who)
        if l.starts_on and (row["starts"] is None or l.starts_on < row["starts"]):
            row["starts"] = l.starts_on
        if l.ends_on and (row["ends"] is None or l.ends_on > row["ends"]):
            row["ends"] = l.ends_on
        row["lifetime"] = row["lifetime"] or bool(l.needs_lifetime)

    out = list(by.values())
    for r in out:
        r.pop("_partner", None)
        r["products"].sort(key=lambda p: p["code"])
        r["orders"] = ", ".join(sorted(r["orders"]))
        r["line_ids"] = ", ".join(sorted(r.pop("lines")))
        r["buyer"] = ", ".join(r["buyers"])
    out.sort(key=lambda r: (r["partner"].lower(), r["client"].lower()))
    return out


@app.get("/orders", response_class=HTMLResponse)
def orders_view(request: Request, view: str = Query("clients"),
                sync: str = Query(""), db: Session = Depends(get_db)):
    lines = [l for l in db.scalars(
        select(OrderLine).order_by(OrderLine.market, OrderLine.client)).all()
        if not _excluded(l.market)]
    from .product_codes import PRODUCTS, ink_on
    legend = [{"code": c, "bg": h, "fg": ink_on(h), "name": n} for c, h, n, _ in PRODUCTS]
    clients = _client_rollup(db, lines) if view == "clients" else []
    from .orders_io import guidance_from_loaded
    from .orders_s3 import running_sync
    running = running_sync(db)
    sync_rec = last_sync(db)
    guidance = (sync_rec.guidance if sync_rec and sync_rec.guidance
                else guidance_from_loaded(db))
    # A partner in the order export with no roster row has no reporter, no
    # trainer and no fallback owner, which is worth seeing rather than reading
    # as an empty cell.
    no_roster = sorted({c["partner"] for c in clients if not c["in_roster"] and c["partner"]})
    return templates.TemplateResponse(request, "orders.html", {
        "lines": lines, "sync": sync_rec, "guidance": guidance, "running": running,
        "s3": settings.s3_configured,
        "nav": "orders", "view": view, "legend": legend,
        "clients": clients, "no_roster": no_roster,
        "env_report": settings.env_report(),
        "s3_uri": f"s3://{settings.orders_s3_bucket}/{settings.orders_s3_key}"
                  if settings.s3_configured else ""})


def _csv_response(filename: str, header: list[str], rows) -> Response:
    """Whatever the page is showing, downloadable. Written through csv.writer
    so a client name with a comma or a quote in it survives the trip."""
    import csv as _csv, io as _io
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(
        content="\ufeff" + buf.getvalue(),        # BOM, so Excel reads UTF-8
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/orders.csv")
def orders_csv(view: str = Query("clients"), db: Session = Depends(get_db)):
    lines = [l for l in db.scalars(
        select(OrderLine).order_by(OrderLine.market, OrderLine.client)).all()
        if not _excluded(l.market)]
    today = dt.date.today().isoformat()
    if view == "clients":
        rows = [[c["partner"], c["client"],
                 " ".join(p["code"] for p in c["products"]),
                 ", ".join(p["name"] for p in c["products"]),
                 c["orders"], c["line_ids"], c["starts"] or "", c["ends"] or "",
                 c["buyer"], c["reporter"], c["trainer"],
                 "yes" if c["lifetime"] else "", "" if c["in_roster"] else "not on roster"]
                for c in _client_rollup(db, lines)]
        return _csv_response(f"report-qa-clients-{today}.csv",
                             ["Partner", "Client", "Products", "Product names", "Order",
                              "Line items", "Start", "End", "Buyer", "Reporter", "Trainer",
                              "Lifetime due", "Roster"], rows)
    rows = [[l.market, l.client, l.product, l.account_ids, l.line_ids,
             l.starts_on or "", l.ends_on or "", l.buyer, l.buyer_email]
            for l in lines]
    return _csv_response(f"report-qa-order-lines-{today}.csv",
                         ["Partner", "Client", "Product", "Order", "Line items",
                          "Start", "End", "Buyer", "Buyer email"], rows)


@app.get("/partners.csv")
def partners_csv(db: Session = Depends(get_db)):
    from .partners import all_partners
    rows = [[p.partner, p.buyer, p.buyer_email, p.seo, p.seo_email, p.manager,
             p.reporting_team, p.trainer, "; ".join(p.recipients),
             p.reporting_notes, p.buyer_notes] for p in all_partners(db)]
    return _csv_response(f"report-qa-partners-{dt.date.today().isoformat()}.csv",
                         ["Partner", "Buyer", "Buyer email", "SEO", "SEO email", "Manager",
                          "Reporter", "Trainer", "Reports go to", "Reporting notes",
                          "Buyer notes"], rows)


# ---------------------------------------------------------------- cycle board
@app.get("/", response_class=HTMLResponse)
@app.get("/cycle", response_class=HTMLResponse)
def cycle_view(request: Request, period: str = Query(""), group: str = Query(""),
               state: str = Query(""), db: Session = Depends(get_db)):
    from .board import STATE_LABEL, by_group, expected_for, summary
    from .cycle import current_period, cycle_for, recent_periods
    from .delivery import latest_deliveries

    period = period or settings.default_period or current_period()
    prune_old_pdfs(db)          # cheap, and keeps the disk from filling silently
    cyc = cycle_for(period)
    exp = expected_for(db, period)
    groups = by_group(db, period, exp)
    if group:
        groups = [g for g in groups if g.group == group]
    rows = [e for g in groups for e in g.expected]
    if state:
        rows = [e for e in rows if e.state == state]
    from .product_codes import pill
    chips = {e.ident: [pill(p) for p in e.products] for e in rows}
    # A pinned period outside the last thirteen months would not be in the
    # dropdown, and the board would show a cycle you could not switch back to.
    periods = recent_periods()
    if period not in periods:
        periods = sorted(set(periods) | {period}, reverse=True)
    return templates.TemplateResponse(request, "cycle.html", {
        "nav": "cycle", "cycle": cyc, "period": period, "chips": chips,
        "periods": periods, "groups": groups, "rows": rows,
        "summary": summary(exp), "state_label": STATE_LABEL,
        "filter_group": group, "filter_state": state,
        "deliveries": latest_deliveries(db, period),
        "notify": settings.notify_status,
        "configured": settings.delivery_configured,
        "today": dt.date.today(),
    })


@app.post("/report/{report_id}/review")
def review_report(report_id: int, request: Request, state: str = Form(...),
                  who: str = Form(""), db: Session = Depends(get_db)):
    rep = db.get(Report, report_id)
    if not rep:
        raise HTTPException(404)
    if state not in {"new", "reviewed", "waived", "needs_fix"}:
        raise HTTPException(400, "unknown review state")
    rep.review_state = state
    rep.reviewed_by = who.strip()
    rep.reviewed_at = dt.datetime.utcnow() if state != "new" else None
    db.commit()
    back = request.headers.get("referer") or "/cycle"
    return RedirectResponse(back, status_code=303)


@app.post("/cycle/{period}/deliver")
def deliver_group(period: str, group: str = Form(...), force: str = Form(""),
                  db: Session = Depends(get_db)):
    from .delivery import deliver
    deliver(db, period, group, force=bool(force))
    return RedirectResponse(f"/cycle?period={period}", status_code=303)


@app.get("/delivery/{delivery_id}/file")
def delivery_file(delivery_id: int, db: Session = Depends(get_db)):
    from .db import Delivery
    d = db.get(Delivery, delivery_id)
    if not d or not d.local_path or not Path(d.local_path).exists():
        raise HTTPException(404)
    return FileResponse(d.local_path, media_type="application/zip",
                        filename=Path(d.local_path).name)


@app.get("/cycle.csv")
def cycle_csv(period: str = Query(""), db: Session = Depends(get_db)):
    from .board import expected_for
    from .cycle import current_period
    period = period or settings.default_period or current_period()
    rows = [[e.market, e.client, e.kind, ", ".join(e.products),
             e.account_ids, e.line_ids, e.starts_on or "", e.ends_on or "",
             e.buyer, e.reporter,
             e.state, e.report.reviewed_by if e.report else "",
             e.report.review_note if e.report else ""]
            for e in expected_for(db, period)]
    return _csv_response(f"report-qa-cycle-{period}.csv",
                         ["Partner", "Client", "Kind", "Products",
                          "Order", "Line items", "Starts", "Ends", "Buyer",
                          "Reporter", "Status",
                          "Reviewed by", "Note"], rows)


@app.get("/inbound", response_class=HTMLResponse)
def inbound_view(request: Request, db: Session = Depends(get_db)):
    """What has actually reached the app, accepted or not."""
    rows = db.scalars(select(Inbound).order_by(desc(Inbound.received_at)).limit(200)).all()
    return templates.TemplateResponse(request, "inbound.html", {
        "nav": "inbound", "rows": rows,
        "secret_set": settings.inbound_secret not in ("", "change-me"),
        "zap_url": f"/inbound/zapier?k={'*' * 8}",
    })


@app.get("/people", response_class=HTMLResponse)
def people_view(request: Request, role: str = Query("reporter"),
                db: Session = Depends(get_db)):
    """Campaign counts by buyer, reporter and trainer.

    Counted on clients, not order lines: one client with nine products is one
    report to pull and one thing to review, so counting lines would make the
    workload look several times bigger than it is for whoever runs multi-product
    accounts.
    """
    from .partners import find as find_partner, resolve_owner
    lines = db.scalars(select(OrderLine)).all()
    pcache: dict[str, Partner | None] = {}

    tally: dict[str, dict] = {}
    seen_clients: dict[str, set] = {}
    for l in lines:
        if _excluded(l.market):
            continue
        if l.market not in pcache:
            pcache[l.market] = find_partner(db, l.market)
        p = pcache[l.market]
        who = {"buyer": l.buyer or resolve_owner(p, l.product)[0],
               "reporter": p.reporting_team if p else "",
               "trainer": p.trainer if p else ""}.get(role, "")
        who = who or "(unassigned)"
        t = tally.setdefault(who, {"who": who, "clients": 0, "lines": 0,
                                   "partners": set(), "products": {}})
        t["lines"] += 1
        t["partners"].add(l.market)
        t["products"][l.product] = t["products"].get(l.product, 0) + 1
        key = (l.market.lower(), l.client.lower())
        s = seen_clients.setdefault(who, set())
        if key not in s:
            s.add(key)
            t["clients"] += 1

    from .product_codes import pill
    rows = []
    for t in tally.values():
        top = sorted(t["products"].items(), key=lambda x: -x[1])[:6]
        rows.append({**t, "partners": len(t["partners"]),
                     "chips": [dict(pill(name), n=n) for name, n in top]})
    rows.sort(key=lambda r: -r["clients"])
    biggest = max((r["clients"] for r in rows), default=1) or 1
    return templates.TemplateResponse(request, "people.html", {
        "nav": "people", "role": role, "rows": rows, "biggest": biggest,
        "total_clients": sum(r["clients"] for r in rows)})


@app.post("/report/{report_id}/note")
def save_note(report_id: int, request: Request, note: str = Form(""),
              db: Session = Depends(get_db)):
    """A note saves on its own, separate from sign-off.

    Tangled together, writing a note would also mark the report reviewed, and
    hitting Reviewed would wipe whatever was half-typed in the box.
    """
    rep = db.get(Report, report_id)
    if not rep:
        raise HTTPException(404)
    rep.review_note = note.strip()[:4000]
    db.commit()
    back = request.headers.get("referer") or f"/report/{report_id}/view"
    return RedirectResponse(back, status_code=303)


@app.post("/report/{report_id}/ack")
def ack_finding(report_id: int, request: Request, index: int = Form(...),
                on: str = Form(""), db: Session = Depends(get_db)):
    """Accept or un-accept one finding.

    Stored by index because a report can carry the same code more than once -
    two thumbnails missing is two findings, and accepting one should not
    silently accept the other.
    """
    rep = db.get(Report, report_id)
    if not rep:
        raise HTTPException(404)
    if not 0 <= index < len(rep.findings or []):
        raise HTTPException(400, "no such finding")
    acked = set(rep.acked or [])
    acked.add(index) if on else acked.discard(index)
    rep.acked = sorted(acked)
    db.commit()
    back = request.headers.get("referer") or f"/report/{report_id}/view"
    return RedirectResponse(back, status_code=303)


@app.post("/report/{report_id}/replace")
async def replace_report(report_id: int, request: Request,
                         file: UploadFile = File(...), who: str = Form(""),
                         db: Session = Depends(get_db)):
    """Swap in a corrected PDF and re-run every check against it.

    Acrobat cannot edit a file that lives on a server, so the round trip is
    download, fix, come back. This makes the return half one step instead of
    re-sending the whole batch: the report keeps its place on the board, its
    notes and its history, and the checks re-run so the new file is judged
    rather than assumed good.
    """
    from .checks import run_all
    from .checks.parser import meta_from_filename
    from .ingest import client_flight
    from .roster import expected_products

    rep = db.get(Report, report_id)
    if not rep:
        raise HTTPException(404)
    blob = await file.read()
    if not blob[:5] == b"%PDF-":
        raise HTTPException(400, "That is not a PDF.")

    path = Path(rep.stored_path) if rep.stored_path else None
    if path is None:
        store = settings.data_dir / f"batch-{rep.batch_id}"
        store.mkdir(parents=True, exist_ok=True)
        path = store / (rep.filename or f"report-{rep.id}.pdf")
    path.write_bytes(blob)

    exp = expected_products(db, rep.client, rep.account_ids, period=rep.period)
    flight = client_flight(db, rep.client, rep.account_ids)
    try:
        result = run_all(path, filename=rep.filename, expected_products=exp,
                         flight=flight, period=rep.period, market=rep.market or "")
    except Exception as exc:  # noqa: BLE001
        rep.severity = "fail"
        rep.findings = [{"code": "unreadable", "severity": "fail",
                         "title": "The replacement could not be read",
                         "detail": str(exc)}]
        rep.checks = []
        db.commit()
        return RedirectResponse(f"/report/{report_id}/view", status_code=303)

    rep.stored_path = str(path)
    rep.pages = result["pages"]
    rep.impressions = result["impressions"]
    rep.clicks = result["clicks"]
    rep.products = ", ".join(result.get("products") or [])
    rep.severity = result["severity"]
    rep.findings = result["findings"]
    rep.checks = result.get("checks") or []
    # A replacement is a new file, so previous acceptances and the sign-off no
    # longer refer to what is on screen. The note is kept - it is about the
    # client, not about that particular copy.
    rep.acked = []
    rep.review_state = "new"
    rep.reviewed_at = None
    rep.reviewed_by = who.strip() or rep.reviewed_by
    db.commit()
    return RedirectResponse(f"/report/{report_id}/view", status_code=303)


@app.get("/report/{report_id}/view", response_class=HTMLResponse)
def report_viewer(report_id: int, request: Request, db: Session = Depends(get_db)):
    rep = db.get(Report, report_id)
    if not rep:
        raise HTTPException(404)
    from .checks.rules import SKIP_WHY
    return templates.TemplateResponse(request, "viewer.html",
                                      {"nav": "cycle", "rep": rep,
                                       "skip_why": SKIP_WHY})


@app.post("/report/{report_id}/recheck")
def report_recheck(report_id: int, db: Session = Depends(get_db)):
    """Run every check again against the file already on disk.

    Findings are written once, at the moment a report arrives, and then stored.
    So a deploy that fixes a rule does not fix the reports that rule already
    got wrong - they keep showing yesterday's answer until something re-reads
    the PDF. This is that something, and it needs no re-upload.
    """
    from .checks.rules import run_all
    from .ingest import client_flight
    from .roster import expected_products

    rep = db.get(Report, report_id)
    if not rep:
        raise HTTPException(404)
    path = Path(rep.stored_path or "")
    if not path.exists():
        rep.findings = (rep.findings or []) + [{
            "code": "missing_file", "severity": "warn",
            "title": "The stored PDF is gone",
            "detail": "Checks could not be re-run because the file is no longer "
                      "on disk. Old PDFs are pruned after "
                      f"{settings.keep_pdf_months} months. Upload it again below."}]
        db.commit()
        return RedirectResponse(f"/report/{report_id}/view", status_code=303)

    exp = expected_products(db, rep.client, rep.account_ids, period=rep.period)
    flight = client_flight(db, rep.client, rep.account_ids)
    result = run_all(path, filename=rep.filename, expected_products=exp,
                     flight=flight, period=rep.period, market=rep.market or "")
    rep.findings = result["findings"]
    rep.checks = result.get("checks") or []
    rep.severity = result["severity"]
    rep.products = ", ".join(result.get("products") or [])
    # An acceptance was given to a finding that may no longer exist, and a
    # sign-off was given to a different answer. Both have to be re-earned.
    rep.acked = []
    if rep.review_state in ("reviewed", "waived"):
        rep.review_state = "new"
        rep.reviewed_at = None
    db.commit()
    return RedirectResponse(f"/report/{report_id}/view", status_code=303)


@app.get("/partners", response_class=HTMLResponse)
def partners_view(request: Request, db: Session = Depends(get_db)):
    from .partners import all_partners
    return templates.TemplateResponse(request, "partners.html", {
        "partners": all_partners(db), "nav": "partners"})


@app.post("/partners/import")
async def partners_import(file: UploadFile = File(...), db: Session = Depends(get_db)):
    from .partners import import_partners
    import_partners(db, await file.read())
    return RedirectResponse("/partners", status_code=303)


@app.post("/orders/import")
async def orders_import(file: UploadFile = File(...), period: str = Form(""),
                        db: Session = Depends(get_db)):
    res = import_orders(db, await file.read(), filename=file.filename or "orders.csv",
                        replace=True, period=period or None)
    n = res["kept"] if isinstance(res, dict) else res
    if isinstance(res, dict):
        msg = (f"Imported {n} order lines from an upload, "
               f"{res.get('rows_read', 0):,} rows read")
        if res.get("duplicate_rows"):
            msg += f", {res['duplicate_rows']:,} duplicate rows ignored"
        db.add(OrderSync(source=f"upload: {file.filename}", rows=n, ok=True,
                         message=msg + ".", guidance=res.get("guidance") or {}))
        db.commit()
    return RedirectResponse(f"/orders?imported={n}", status_code=303)


def _run_sync(claim_id: int) -> None:
    """The actual work, off the request."""
    db = SessionLocal()
    try:
        sync_orders(db, force=True, claim_id=claim_id)
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        db.rollback()
        try:
            from .orders_s3 import _close
            _close(db, claim_id)
            db.add(OrderSync(source="", ok=False, rows=0, state="done",
                             message=f"Sync crashed: {type(exc).__name__}: {exc}"))
            db.commit()
        except Exception:  # noqa: BLE001
            db.rollback()
    finally:
        db.close()


@app.post("/orders/sync")
def orders_sync(background: BackgroundTasks, db: Session = Depends(get_db)):
    """Start the sync and answer immediately.

    It downloads about 850 MB and parses a couple of million rows. Doing that
    inside the request meant the browser sat on an open connection for minutes
    with no response - which is what made the page look hung. The order list
    now shows a running state and refreshes itself.
    """
    from .orders_s3 import begin_sync
    claim = begin_sync(db)
    if claim is None:
        return RedirectResponse("/orders?sync=already", status_code=303)
    background.add_task(_run_sync, claim.id)
    return RedirectResponse("/orders?sync=started", status_code=303)


@app.post("/batch/{batch_id}/renotify")
def renotify(batch_id: int, db: Session = Depends(get_db)):
    from .notify import post_slack, send_digest
    batch = db.get(Batch, batch_id)
    if not batch:
        raise HTTPException(404)
    comp = completeness(db, batch.market, batch.period)
    post_slack(batch, comp)
    send_digest(batch, comp)
    return RedirectResponse(f"/batch/{batch_id}", status_code=303)
