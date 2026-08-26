from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from urllib.parse import quote

from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form, HTTPException,
                     Query, Request, UploadFile)
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, desc, func, select
from sqlalchemy.orm import Session

from . import brand, version
from .config import settings
from .db import (Batch, Inbound, KnownLogo, OrderLine, OrderSync, Partner,
                 Report, SessionLocal, init_db)
from .ingest import (finish_batch, parse_postmark, process_batch,
                    prune_old_pdfs, sweep_stale)
from .orders_s3 import last_sync, sync as sync_orders
from .roster import completeness, import_orders

app = FastAPI(title="Report QA")

# THE BOARD IS A MEGABYTE AND A HALF OF HTML and it was going over the wire raw.
# A hundred and forty-six partner cards and three hundred report rows is
# repetitive markup that compresses about fifteen to one, so this is the
# cheapest second anybody gets back - and it costs the server almost nothing
# next to building the page in the first place.
from fastapi.middleware.gzip import GZipMiddleware      # noqa: E402
app.add_middleware(GZipMiddleware, minimum_size=1024)

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


def _human_hours(h):
    from .pace import humanise
    return humanise(h)


def _working_days(h):
    from .pace import working_days
    return working_days(h)


templates.env.filters["humanhours"] = _human_hours
templates.env.filters["workingdays"] = _working_days
# Chrome that every page needs and no view should have to remember to pass.
# ---------------------------------------------------------------- who is here
#
# A NAME IN A COOKIE, NOT A LOGIN.
#
# The board is behind the office network and everyone on it is allowed to do
# everything here, so a password would protect nothing and cost a support
# request every time somebody forgot theirs. What sign-off actually needs is
# attribution: a name against "I looked at this". Typing it into every row is
# what made people stop doing it.
#
# So the name is remembered in a plain cookie on that browser. It is not a
# security boundary and is not treated as one - nothing is authorised by it.
USER_COOKIE = "qa_user"
USER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365


def whoami(request: Request) -> str:
    return (request.cookies.get(USER_COOKIE) or "").strip()[:128]


def _remember(response, who: str) -> None:
    response.set_cookie(USER_COOKIE, who.strip()[:128],
                        max_age=USER_COOKIE_MAX_AGE, samesite="lax", path="/")


# WHERE THE REPORT PAGE WAS OPENED FROM.
#
# Read off the referer, which is right exactly once - the first arrival from
# the board. Accepting a finding, saving a note, re-checking or replacing the
# file all redirect back to the same page, and from then on the referer IS that
# page. So the way back went blank and Reviewed left you sitting on the report
# you had just signed off. A short-lived cookie carries it across those.
BACK_COOKIE = "qa_back"
BACK_COOKIE_MAX_AGE = 60 * 60 * 8


def _back_cookie(request: Request) -> str:
    v = (request.cookies.get(BACK_COOKIE) or "").strip()[:512]
    # Same-site paths only. A cookie is user input like any other.
    if not v.startswith("/") or v.startswith("//") or "/report/" in v:
        return ""
    return v


templates.env.globals.update(
    head_tags=brand.HEAD_TAGS,
    build_label=version.label(),
    build_notes=version.BUILD_NOTES,
    build_service=version.service(),
    whoami=whoami,
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
        from .partners import backfill_targets, seed_if_empty
        seed_if_empty(db)
        # A roster uploaded without the Delivery column left every target
        # blank, and blank means Drive - which is how a Dropbox partner's
        # client got a Drive link.
        backfill_targets(db)
    except Exception:
        import traceback; traceback.print_exc(); db.rollback()
    finally:
        db.close()

    # Anything judged by older checking code gets re-read in the background.
    # Without this a fixed rule only reaches the reports that arrive after the
    # deploy, and everything already on the board keeps its wrong answer.
    try:
        from .recheck import start_sweeper
        start_sweeper()
    except Exception:
        import traceback; traceback.print_exc()


@app.get("/healthz")
def healthz():
    """Includes the build so you can confirm what is actually live without
    trusting the dashboard, which looks identical either way."""
    return {"ok": True, **version.info(), "rules": version.rules_version()}


@app.get("/healthz/deep")
def healthz_deep():
    """The same, plus how many reports still carry an older answer.

    SEPARATE FROM /healthz ON PURPOSE. The platform pings /healthz every few
    seconds with a five-second timeout, and a COUNT over the reports table on
    every one of those - while the sweeper has the box busy re-reading eight
    hundred PDFs - is a health check that fails because the service is working.
    Render mailed about exactly that. The number is worth having; it is not
    worth having on the liveness probe.
    """
    out = {"ok": True, **version.info(), "rules": version.rules_version()}
    db = SessionLocal()
    try:
        from .recheck import stale_count
        out["awaiting_recheck"] = stale_count(db)
    except Exception as exc:  # noqa: BLE001
        out["awaiting_recheck"] = f"unknown: {exc}"
    finally:
        db.close()
    return out


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
    # inline, not attachment: these get looked at far more often than saved.
    #
    # And never cached. The URL does not change when a corrected PDF replaces
    # the old one, so the browser kept showing the file that had just been
    # fixed - findings updated, page did not.
    return FileResponse(rep.stored_path, media_type="application/pdf",
                        headers={"Content-Disposition":
                                 f'inline; filename="{rep.filename}"',
                                 "Cache-Control": "no-store, must-revalidate",
                                 "Pragma": "no-cache"})


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
    # COLUMNS, NOT OBJECTS. Thirteen thousand order lines built as ORM
    # instances - each one decoding a JSON flights column that nothing on this
    # page reads - was most of a second before a row of it was drawn.
    lines = [l for l in db.execute(
        select(OrderLine.market, OrderLine.client, OrderLine.product,
               OrderLine.account_ids, OrderLine.line_ids, OrderLine.buyer,
               OrderLine.starts_on, OrderLine.ends_on, OrderLine.needs_lifetime,
               OrderLine.live, OrderLine.budget, OrderLine.impressions)
        .order_by(OrderLine.market, OrderLine.client)).all()
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
        "plan": pull_plan(db), "tap_max_days": TAP_MAX_DAYS,
        # Three different things can start a sync, and none of them used to
        # say so - which is why finding one running looked like the tool
        # deciding to do something on its own.
        "triggers": _sync_triggers(),
        "strategy": pull_strategy(db),
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
# How many report rows to render before asking. A board of 1,244 expected
# reports put 24,851 DOM nodes on the page and took the browser four seconds,
# against a third of a second on the server.
ROW_CAP = 150


def _logo_is_generic(db: Session, rep) -> bool:
    from .checks.logo import is_generic
    return is_generic(db, rep.logo_hash or "")


def _orders_syncing(db: Session) -> bool:
    """Is a re-read running right now?

    The sync answers immediately and does the work in the background, so
    pressing the button and landing back on an unchanged banner reads as
    nothing having happened. It had; it just had not finished.
    """
    from .db import OrderSync
    return bool(db.scalar(select(OrderSync).where(OrderSync.state == "running")))


def _orders_stale(db: Session) -> bool:
    """Were the loaded orders read by an older version of the import code?

    The export is parsed once and only the answer is kept, so a fix to the
    import - a product mapping, the paused-line rule, the per-order flights -
    does nothing until the file is read again. The sweeper re-reads it on its
    own, but when that has not happened yet the board goes on showing findings
    from the old answer and there is no sign anywhere that it is doing so.
    That silence is what turned one bug into three rounds of screenshots.
    """
    from .db import OrderSync
    from .version import product_map_version
    row = db.scalars(select(OrderSync).where(OrderSync.state != "running")
                     .order_by(desc(OrderSync.id)).limit(1)).first()
    return bool(row and row.ok and (row.map_version or "") != product_map_version())


def _delivered(db: Session, period: str, groups) -> dict:
    """The finished partners, and their links, for the top of the board.

    A delivered partner sorts in among a hundred and forty-five others, so the
    one thing somebody came to the page for - the link they are about to send -
    was found by scrolling. It belongs where the counts are.
    """
    from .delivery import latest_deliveries

    dels = latest_deliveries(db, period)
    links, failed = [], 0
    # Walked over the DELIVERIES, not over the groups. A partner whose orders
    # moved can drop off this cycle's expected list after its reports went out,
    # and taking the link off the page with it is not an improvement.
    for name, d in dels.items():
        if not d.ok:
            failed += 1
            continue
        links.append({
            "group": name,
            "url": d.share_url or "",
            # No Drive or Dropbox configured yet, so the delivery is a zip on
            # this box. There is no URL to copy - the link is this app's own
            # download route, which only works for somebody logged in here.
            "download": f"/delivery/{d.id}/file" if not d.share_url else "",
            "target": d.target or "", "reports": d.reports or 0,
            "archive": (d.archive_url or "") if d.archive_url != d.share_url else "",
        })
    links.sort(key=lambda x: x["group"].lower())
    ready = sum(1 for g in groups if g.ready and g.group not in dels)
    return {"links": links, "count": len(links), "groups": len(groups),
            "ready": ready, "failed": failed}


def _stale_here(db: Session, period: str, groups) -> dict:
    """How many reports this board has, and how many carry an older answer.

    Two grouped queries, not two per partner. Asking per group was 290 COUNT
    queries on a 145-partner board and it was the reason the page had started
    taking a moment to come up.
    """
    from sqlalchemy import func
    from .board import markets_by_group
    from .version import rules_version

    # Counted WITHOUT the signed-off ones, because that is what the button
    # does. It said "Re-check ... 8 reports" and then "6 of 8" on a partner
    # with one report still pending.
    signed = Report.review_state.in_(("reviewed", "waived"))
    # Signed off AND judged by older code. Not swept automatically - re-reading
    # finished work while a rule changes three times a day is how the queue
    # never empties and the board keeps un-reviewing itself - so it is counted
    # here instead, for a deliberate pass before delivery.
    # BOTH numbers count the same population: the reports the button will act
    # on. Counting stale over ALL of them while the button skipped the
    # signed-off ones left the amber dot on for ever - press it, it does the
    # work it can, and the count it is judged by never moves.
    stale = Report.rules_version != rules_version()
    rows = db.execute(
        select(Report.market,
               func.sum(case((signed, 0), else_=1)),
               func.sum(case((signed, 0), (stale, 1), else_=0)))
        .where(Report.period == period)
        .group_by(Report.market)).all()
    have_by_market = {m or "": int(n or 0) for m, n, _s in rows}
    stale_by_market = {m or "": int(st or 0) for m, _n, st in rows}

    index = markets_by_group(db)
    signed_stale = db.scalar(
        select(func.count()).select_from(Report)
        .where(Report.period == period, signed,
               Report.rules_version != rules_version())) or 0
    out = {"total": sum(stale_by_market.values()), "by_group": {}, "have": {},
           "signed_stale": int(signed_stale)}
    for g in groups:
        markets = index.get(g.group) or [g.group]
        if g.group not in markets:
            markets = markets + [g.group]
        have = sum(have_by_market.get(m, 0) for m in markets)
        if not have:
            continue
        out["have"][g.group] = have
        n = sum(stale_by_market.get(m, 0) for m in markets)
        if n:
            out["by_group"][g.group] = n
    return out


def _recheck_jobs(db: Session) -> dict:
    """Running re-checks, keyed by the group they are working on.

    The card needs to know its OWN job. Without that the button sat there
    looking untouched after it had been pressed, because the work happens in
    the background and the redirect comes back instantly.
    """
    from .recheck import running_jobs
    out = {"all": {}, "by_group": {}}
    for j in running_jobs(db).values():
        if j.get("group"):
            out["by_group"][j["group"]] = j
        else:
            out["all"] = j
    return out


@app.get("/", response_class=HTMLResponse)
@app.get("/cycle", response_class=HTMLResponse)
def cycle_view(request: Request, period: str = Query(""), group: str = Query(""),
               state: str = Query(""), rows_: str = Query("", alias="rows"),
               db: Session = Depends(get_db)):
    from .board import (MIN_DAYS_IN_MONTH, STATE_LABEL, by_group, expected_for,
                        summary)
    from .cycle import current_period, cycle_for, recent_periods
    from .delivery import delivery_jobs, latest_deliveries
    from .pace import pace

    show_all = rows_ == "all"
    period = period or settings.default_period or current_period()
    prune_old_pdfs(db)          # cheap, and keeps the disk from filling silently
    cyc = cycle_for(period)
    # Rows this cycle does NOT owe, and why. A report that quietly stops being
    # expected is indistinguishable from one the tool forgot about, so the
    # reasons go on the page rather than only into the code.
    not_owed: list = []
    exp = expected_for(db, period, skipped=not_owed)
    groups = by_group(db, period, exp)
    # Counted before the filter. A partner filter narrows what is listed below;
    # it must not make the cycle look like it has one partner in it.
    delivered = _delivered(db, period, groups)
    if group:
        groups = [g for g in groups if g.group == group]
    rows = [e for g in groups for e in g.expected]
    if state:
        rows = [e for e in rows if e.state == state]
    # Signed off and nothing failing: done, and in the way. It goes to its own
    # section at the bottom so the top of the page is only what is still open.
    done = [e for e in rows if e.ready]
    rows = [e for e in rows if not e.ready]
    # ARRIVED FIRST. Two thirds of a cycle has not been sent yet, so in market
    # order the reports there is something to DO about sit below a screenful of
    # "Not received" - and the row cap can cut them off the page entirely.
    # Stable, so market and client order is kept inside each half.
    rows.sort(key=lambda e: 0 if e.report else 1)
    # The reports table was 24,851 of the page's 30,342 DOM nodes and four
    # seconds of browser time. The server was never the slow part.
    cap = None if show_all else ROW_CAP
    shown, done_shown = rows[:cap] if cap else rows, done[:cap] if cap else done
    from .product_codes import pill
    # Both tables need their pills, and the signed-off one is rendered from the
    # same macro - built from `rows` alone it came out with no products at all.
    chips = {e.ident: [pill(p) for p in e.products]
             for e in list(shown) + list(done_shown)}
    # A pinned period outside the last thirteen months would not be in the
    # dropdown, and the board would show a cycle you could not switch back to.
    periods = recent_periods()
    if period not in periods:
        periods = sorted(set(periods) | {period}, reverse=True)
    return templates.TemplateResponse(request, "cycle.html", {
        "nav": "cycle", "cycle": cyc, "period": period, "chips": chips,
        "periods": periods, "groups": groups,
        "rows": shown, "row_total": len(rows),
        "done": done_shown, "done_total": len(done),
        "show_all": show_all, "row_cap": ROW_CAP,
        "summary": summary(exp), "state_label": STATE_LABEL,
        # "763 not received" does not answer the question anybody has, which is
        # whether that is a morning's work or the rest of the week.
        "pace": pace(db, period, summary(exp)["missing"]),
        "filter_group": group, "filter_state": state,
        "deliveries": latest_deliveries(db, period),
        # Packaging runs in the background, so the card has to say where it is.
        "packing": delivery_jobs(db),
        # The finished links, at the top. A partner that is done sorts in with
        # 145 others, so the one thing you came to the page for - the link you
        # are about to send - was found by scrolling.
        "delivered": delivered,
        "views": _saved_views(db),
        "not_owed": sorted(not_owed, key=lambda r: (r["market"] or "",
                                                    r["client"] or "")),
        "min_days": MIN_DAYS_IN_MONTH,
        "orders_stale": _orders_stale(db),
        "orders_syncing": _orders_syncing(db),
        # How many reports on this board still carry an older answer, and per
        # partner so a card can offer to fix just that one.
        "stale": _stale_here(db, period, groups),
        "jobs": _recheck_jobs(db),
        "notify": settings.notify_status,
        "configured": settings.delivery_configured,
        "today": dt.date.today(),
    })


SAVED_KEYS = ("q", "only", "partner", "buyer", "reporter", "trainer", "status", "state")


def _saved_views(db: Session) -> list:
    from .db import SavedView
    return list(db.scalars(select(SavedView).order_by(SavedView.name)).all())


@app.post("/views")
def save_view(request: Request, name: str = Form(""), query: str = Form(""),
              db: Session = Depends(get_db)):
    """Name the filters you are looking at, so you can get back to them."""
    from urllib.parse import parse_qsl, urlencode

    from .db import SavedView

    back = request.headers.get("referer") or "/cycle"
    name = name.strip()[:120]
    if not name:
        return RedirectResponse(back, status_code=303)
    # The period is deliberately dropped. A view saved while looking at July
    # should open on whatever cycle you are on, or it becomes wrong the moment
    # the month turns.
    keep = [(k, v) for k, v in parse_qsl(query.lstrip("?"))
            if k in SAVED_KEYS and v]
    row = db.scalar(select(SavedView).where(SavedView.name == name))
    if row is None:
        row = SavedView(name=name)
        db.add(row)
    row.query = urlencode(keep)[:2048]
    row.created_by = whoami(request)
    row.created_at = dt.datetime.utcnow()
    db.commit()
    return RedirectResponse(back, status_code=303)


@app.post("/views/{view_id}/delete")
def delete_view(view_id: int, request: Request, db: Session = Depends(get_db)):
    from .db import SavedView
    row = db.get(SavedView, view_id)
    if row is not None:
        db.delete(row)
        db.commit()
    return RedirectResponse(request.headers.get("referer") or "/cycle", status_code=303)


@app.post("/me")
def set_me(request: Request, who: str = Form("")):
    """Remember who is at this browser, or forget them."""
    back = request.headers.get("referer") or "/cycle"
    resp = RedirectResponse(back, status_code=303)
    if who.strip():
        _remember(resp, who)
    else:
        resp.delete_cookie(USER_COOKIE, path="/")
    return resp


@app.get("/report/{report_id}/logo.png")
def report_logo(report_id: int, db: Session = Depends(get_db)):
    """The exact corner the fingerprint was taken from.

    Marking a logo is a decision about a picture, so the page shows the
    picture rather than asking somebody to take the fingerprint on trust.
    """
    from .checks.logo import crop_png
    rep = db.get(Report, report_id)
    if not rep or not rep.stored_path or not Path(rep.stored_path).exists():
        raise HTTPException(404)
    png = crop_png(rep.stored_path)
    if not png:
        raise HTTPException(404)
    # The URL carries ?v=<file token>, so a cached copy can only ever be the
    # crop from the file that token names. Without it the browser held the old
    # logo for a day after a replacement and showed it beside the new report.
    return Response(content=png, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=86400, immutable"})


@app.post("/report/{report_id}/logo/refresh")
def report_logo_refresh(report_id: int, request: Request,
                        db: Session = Depends(get_db)):
    """Take the page-one fingerprint again.

    A re-check reuses the stored hash rather than re-taking it - pdftoppm on
    every report in an 838-deep queue is what put Render's health check over.
    So a report whose fingerprint came back empty the first time stayed empty
    for good, and its logo could never be marked. This is the way out, one
    report at a time.
    """
    from .checks.logo import header_logo_hash
    rep = db.get(Report, report_id)
    if not rep or not rep.stored_path or not Path(rep.stored_path).exists():
        raise HTTPException(404)
    rep.logo_hash = header_logo_hash(rep.stored_path)
    db.commit()
    return RedirectResponse(f"/report/{report_id}/view#logo", status_code=303)


@app.post("/logo/{logo}/mark")
def mark_logo(logo: str, request: Request, kind: str = Form("generic"),
              db: Session = Depends(get_db)):
    """Record what this mark is, once, for every report that carries it.

    The check does not guess. Guessing was tried - a logo on three or more
    markets could not be any one partner's, so it must be the tool's - and
    Seven Mountains disproved it in a day, running three markets on this board
    with one perfectly correct logo across all of them.
    """
    if not re.fullmatch(r"[0-9a-f]{4,32}", logo or ""):
        raise HTTPException(400, "not a logo fingerprint")
    row = db.scalar(select(KnownLogo).where(KnownLogo.logo_hash == logo))
    if kind == "clear":
        if row is not None:
            db.delete(row)
    else:
        if row is None:
            row = KnownLogo(logo_hash=logo)
            db.add(row)
        row.kind = "generic" if kind == "generic" else "ok"
        row.marked_by = whoami(request)
        row.marked_at = dt.datetime.utcnow()
    db.commit()
    # Every report carrying this mark now has an out-of-date answer.
    n = 0
    for rep in db.scalars(select(Report).where(Report.logo_hash == logo)).all():
        rep.rules_version = ""
        n += 1
    if n:
        db.commit()
    # AND RE-CHECK THEM NOW, RATHER THAN CLEARING A STAMP AND HOPING.
    #
    # Clearing rules_version only put them in the sweep's queue, and the sweep
    # skips anything already signed off and stops running once its queue drains
    # - so marking a logo could sit there doing nothing visible until the next
    # deploy. The mark is the whole point: it should reach the other reports
    # that carry it while you are still looking at the screen.
    from .recheck import start_job
    start_job(db, key=f"logo:{logo}", stale_only=True, logo=logo)
    back = request.headers.get("referer") or "/cycle"
    back = back.split("#")[0]
    back += ("&" if "?" in back else "?") + f"logo_queued={n}"
    return RedirectResponse(back + "#logo", status_code=303)


@app.post("/reports/review")
def review_many(request: Request, ids: list[int] = Form([]), state: str = Form(""),
                who: str = Form(""), db: Session = Depends(get_db)):
    """Sign off a set of reports at once.

    Most of a cycle is reports where every check passed, and ticking them one
    at a time is the longest single job on the board. This is not a shortcut
    past reading them - it is for the screenful you have just read and agree
    with, which is why the page defaults the selection to nothing and gives
    you a one-click way to take only the ones that passed.
    """
    back = request.headers.get("referer") or "/cycle"
    if state not in {"reviewed", "waived", "needs_fix"}:
        raise HTTPException(400, "unknown review state")
    name = who.strip() or whoami(request)
    if not name or not ids:
        return RedirectResponse(back, status_code=303)
    now = dt.datetime.utcnow()
    # Capped. The form is built from what is on screen, but the request is not
    # trusted to be, and a runaway list should not become a table scan.
    for rep in db.scalars(select(Report).where(Report.id.in_(ids[:500]))).all():
        rep.review_state = state
        rep.reviewed_by = name
        rep.reviewed_at = now
        rep.signoff_cleared_at = None
    db.commit()
    resp = RedirectResponse(back, status_code=303)
    if who.strip():
        _remember(resp, who)
    return resp


@app.post("/report/{report_id}/review")
def review_report(report_id: int, request: Request, state: str = Form(...),
                  who: str = Form(""), back: str = Form(""),
                  db: Session = Depends(get_db)):
    rep = db.get(Report, report_id)
    if not rep:
        raise HTTPException(404)
    if state not in {"new", "reviewed", "waived", "needs_fix"}:
        raise HTTPException(400, "unknown review state")
    # Typed name wins, then the one this browser remembers. Falling back the
    # other way round would mean a signed-in person could never sign for
    # somebody sitting next to them.
    name = who.strip() or whoami(request)
    rep.review_state = state
    rep.reviewed_by = name
    rep.reviewed_at = dt.datetime.utcnow() if state != "new" else None
    rep.signoff_cleared_at = None        # a fresh decision, whatever went before
    db.commit()
    # BACK TO THE BOARD, NOT BACK TO THIS REPORT.
    #
    # Signing one off is the last thing you do with it, so landing on the same
    # page again means scrolling the board back to where you were, every time.
    # The board this report was opened from is carried on the form.
    target = back if back.startswith("/") and not back.startswith("//") else ""
    # The cookie, not the referer, as the fallback. The referer here is the
    # report page itself, which is the one place this must not land.
    resp = RedirectResponse(target or _back_cookie(request) or "/cycle",
                            status_code=303)
    # First sign-off of the day remembers you, so there is no separate step to
    # find before the thing you came to do.
    if who.strip():
        _remember(resp, who)
    return resp


@app.post("/cycle/done")
def mark_row_done(request: Request, period: str = Form(...),
                  market: str = Form(""), client: str = Form(""),
                  kind: str = Form("monthly"), action: str = Form("done"),
                  who: str = Form(""), note: str = Form(""),
                  db: Session = Depends(get_db)):
    """Check a row off for this cycle with no PDF behind it.

    SEO is done outside TapClicks, so there is no report to upload and the row
    sat at "Not received" all month, holding its partner off ready. This marks
    it handled for THIS cycle only - next month it is back on the board asking
    for a report.
    """
    from .board import _key as board_key
    from .db import CycleDone
    if kind not in {"monthly", "lifetime"}:
        raise HTTPException(400, "unknown report kind")
    back = request.headers.get("referer") or f"/cycle?period={period}"
    ident = f"{board_key(market)}|{board_key(client)}|{kind}"
    if not board_key(market) or not board_key(client):
        raise HTTPException(400, "no row to mark")
    row = db.scalar(select(CycleDone).where(CycleDone.period == period,
                                            CycleDone.ident == ident))
    if action == "clear":
        if row is not None:
            db.delete(row)
        db.commit()
        return RedirectResponse(back, status_code=303)
    name = who.strip() or whoami(request) or "checked off"
    if row is None:
        row = CycleDone(period=period, ident=ident)
        db.add(row)
    row.market, row.client, row.kind = market, client, kind
    row.note = note.strip()[:255]
    row.marked_by = name
    row.marked_at = dt.datetime.utcnow()
    db.commit()
    resp = RedirectResponse(back, status_code=303)
    if who.strip():
        _remember(resp, who)
    return resp


@app.post("/cycle/{period}/deliver")
def deliver_group(period: str, group: str = Form(...), force: str = Form(""),
                  db: Session = Depends(get_db)):
    # IT RUNS IN THE BACKGROUND NOW.
    #
    # It uploads every PDF in the partner one after another, which on a big one
    # is several minutes - and it was doing that inside this request, so the
    # browser sat on a spinner with no way to tell a slow upload from a dead
    # one. The card shows "12 of 30" while it works and the link appears on it
    # when it finishes.
    from .delivery import start_delivery
    start_delivery(db, period, group, force=bool(force))
    back = f"/cycle?period={period}"
    if group:
        back += f"&group={quote(group)}"
    return RedirectResponse(back, status_code=303)


@app.get("/rules", response_class=HTMLResponse)
def rules_view(request: Request):
    """The rules the board applies, in words.

    They were only in the code, which means the only way to answer "why is this
    not asking for a report" was to read it - and every one of these rules came
    out of a conversation about a row somebody did not expect.
    """
    from .board import MIN_DAYS_IN_MONTH, SHORT_CAMPAIGN_DAYS
    from .checks.rules import GOAL_BAND
    ctx = {"nav": "rules", "min_days": MIN_DAYS_IN_MONTH,
           "short_days": SHORT_CAMPAIGN_DAYS,
           "goal_band": int(GOAL_BAND * 100)}
    if request.query_params.get("frag"):
        return templates.TemplateResponse(request, "rules_body.html", ctx)
    return templates.TemplateResponse(request, "rules.html", ctx)


@app.get("/lifetimes", response_class=HTMLResponse)
def lifetimes_view(request: Request, db: Session = Depends(get_db)):
    """Every lifetime report that has gone out.

    A lifetime is owed once, when the campaign ends, so the question the board
    cannot answer on its own is "has this one already had theirs?". This is the
    record - and it is what stops a client being asked for a second copy.
    """
    from .board import lifetimes_delivered
    rows = lifetimes_delivered(db)
    return templates.TemplateResponse(request, "lifetimes.html", {
        "nav": "lifetimes", "rows": rows})


@app.get("/cycle/links")
def cycle_links(request: Request, period: str = Query(""), new: str = Query(""),
                db: Session = Depends(get_db)):
    """Every finished partner's client link for this cycle, on its own page."""
    from .board import by_group
    from .cycle import current_period, cycle_for, recent_periods

    period = period or settings.default_period or current_period()
    groups = by_group(db, period)
    delivered = _delivered(db, period, groups)
    periods = recent_periods()
    if period not in periods:
        periods = sorted(set(periods) | {period}, reverse=True)
    # The one just packaged goes first, however the rest are sorted. It is the
    # reason this page is open.
    if new:
        delivered["links"].sort(key=lambda l: l["group"] != new)
    # WHERE EACH PARTNER SHOULD HAVE GONE, beside where it went. A blank
    # delivery target means Drive, so a Dropbox partner whose roster row had
    # lost its target was packaged to Drive and the link looked perfectly fine.
    want = {g.group: (g.target or settings.delivery_target) for g in groups}
    for l in delivered["links"]:
        should = want.get(l["group"], "")
        l["should"] = should
        l["mismatch"] = bool(should and l["target"] and should != l["target"])
    return templates.TemplateResponse(request, "links.html", {
        "nav": "links", "cycle": cycle_for(period), "period": period,
        "periods": periods, "delivered": delivered, "new": new,
        "configured": settings.delivery_configured,
    })


@app.get("/delivery/{delivery_id}/file")
def delivery_file(delivery_id: int, db: Session = Depends(get_db)):
    from .db import Delivery
    d = db.get(Delivery, delivery_id)
    if not d or not d.local_path or not Path(d.local_path).exists():
        raise HTTPException(404)
    return FileResponse(d.local_path, media_type="application/zip",
                        filename=Path(d.local_path).name)


@app.post("/cycle/recheck")
def cycle_recheck(period: str = Form(""), group: str = Form(""),
                  scope: str = Form("stale"), db: Session = Depends(get_db)):
    """Re-check a partner, or the whole cycle, now.

    The background sweep gets to everything on its own; this is for when
    "eventually" is not soon enough - a fix has just gone out and somebody
    wants that partner's board right before they hand a link over.
    """
    from .recheck import start_job
    period = period or settings.default_period or ""
    key = f"{period}:{group}" if group else f"{period}:*"
    # A partner button means "make this partner right", which is every report
    # it has - "Re-check 2" on a card headed "14 reports" reads as a bug even
    # when 2 is the true number of stale ones.
    # scope=signed is the deliberate pass over reports that ARE signed off and
    # were judged by older code. Everything else leaves them alone.
    start_job(db, key, group=group or None, period=period or None,
              stale_only=(scope != "all"),
              signed_only=(scope == "signed"),
              # A partner button means "bring this partner up to date", and a
              # report somebody signed off is up to date. It said "6 of 8" on a
              # partner with one report still pending.
              skip_signed=bool(group))
    back = f"/cycle?period={period}" + (f"&group={quote(group)}" if group else "")
    return RedirectResponse(back, status_code=303)


@app.get("/cycle.csv")
def cycle_csv(period: str = Query(""), db: Session = Depends(get_db)):
    from .board import expected_for
    from .cycle import current_period
    period = period or settings.default_period or current_period()
    rows = [[e.market, e.client, e.kind, ", ".join(e.products),
             e.account_ids, e.line_ids, e.starts_on or "", e.ends_on or "",
             e.buyer, e.reporter,
             e.state, e.report.reviewed_by if e.report else e.done_by,
             e.report.review_note if e.report else e.done_note]
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

    # ACCEPTING THE LAST ONE IS A REVIEW.
    #
    # Going through every finding and ticking it off IS reading the report -
    # asking for a signature afterwards is asking the same question twice, and
    # the answer was a screen full of reports sitting at "in, unreviewed" that
    # somebody had in fact been through.
    #
    # Only on the way in, and only from "new": un-ticking something does not
    # tear up a sign-off, and a report already marked needs fix or waived has
    # a decision on it that this must not overwrite.
    auto = False
    name = whoami(request)
    if on and name and rep.review_state == "new" and not rep.open_findings:
        rep.review_state = "reviewed"
        rep.reviewed_by = name
        rep.reviewed_at = dt.datetime.utcnow()
        rep.signoff_cleared_at = None
        auto = True
    db.commit()
    back = request.headers.get("referer") or f"/report/{report_id}/view"
    if auto and "#" not in back:
        back += "#signoff"
    return RedirectResponse(back, status_code=303)


def file_token(rep) -> str:
    """A token that changes when the stored PDF does.

    The viewer embeds /report/<id>/file, and that URL is identical before and
    after a replacement - so the browser showed the old file next to the new
    findings until somebody hard-refreshed.
    """
    if not rep.stored_path:
        return "0"                  # Path("") stats the working directory
    try:
        st = Path(rep.stored_path).stat()
        return f"{int(st.st_mtime)}-{st.st_size}"
    except OSError:
        return "0"


def canonical_filename(rep) -> str:
    """The name this report should be filed under - built, not inherited.

    It used to be whatever the file arrived as, minus a browser's "(1)". That
    is right for the feed, whose names are already correct, and wrong for
    everything else: a report uploaded by hand as "Digital Marketing Report.pdf"
    kept that name onto the board, into the zip and into the partner's folder,
    where nothing can be filed by it.
    """
    from .naming import canonical_name
    return canonical_name(rep)


def _names_another_report(rep, uploaded: str) -> str:
    """A refusal message if the uploaded file is named for a different report.

    Only when the name actually carries order ids and none of them are this
    report's. "download.pdf" says nothing about which client it is, and
    refusing it would defeat the point.
    """
    if not uploaded:
        return ""
    from .checks.parser import meta_from_filename
    meta = meta_from_filename(uploaded)
    ids = {i for i in (meta.get("account_ids") or "").split() if i}
    mine = {i for i in (rep.account_ids or "").replace(",", " ").split() if i}
    if not ids or not mine or (ids & mine):
        return ""
    return (f"That file is named for order {', '.join(sorted(ids))}, and this "
            f"report is order {', '.join(sorted(mine))} - "
            f"{rep.client}. Open the right report, or rename the file if the "
            f"name is the thing that is wrong.")


@app.post("/cycle/upload")
async def upload_for_expected(period: str = Form(""), market: str = Form(""),
                              client: str = Form(""), account_ids: str = Form(""),
                              kind: str = Form("monthly"),
                              file: UploadFile = File(...),
                              db: Session = Depends(get_db)):
    """Put a report against a row that is still waiting for one.

    Some reports never come through the feed - pulled by hand, sent to the
    wrong address, rebuilt after a fix. Without this the only way to get one
    onto the board was to have it re-mailed through Zapier, and the row sat at
    "Not received" while the PDF was on somebody's desktop.
    """
    from .checks import run_all
    from .cycle import cycle_for
    from .ingest import client_flight, flight_lines, open_batch
    from .roster import (attach_owners, expected_any, expected_products,
                     expected_why, ordered_for, quiet_products,
                     budgets_for)
    from .version import rules_version as _rv

    blob = await file.read()
    if not blob[:5] == b"%PDF-":
        raise HTTPException(400, "That is not a PDF.")
    period = period or settings.default_period or ""
    is_lifetime = kind == "lifetime"

    # If one already exists for this client and cycle, this is a replacement
    # and should go through the route that knows how to handle one.
    from .ingest import _rkey
    for r in db.scalars(select(Report).where(Report.period == period)).all():
        if bool(r.is_lifetime) != is_lifetime:
            continue
        ids, _n = _rkey(client, account_ids, is_lifetime)
        mine, _m = _rkey(r.client, r.account_ids, bool(r.is_lifetime))
        if ids & mine:
            return RedirectResponse(f"/report/{r.id}/view", status_code=303)

    batch = open_batch(db, market, period)
    if batch is None:
        batch = Batch(market=market, period=period, source="manual",
                      status="done", email_subject=f"Uploaded by hand · {market}")
        db.add(batch)
        db.flush()

    store = settings.data_dir / f"batch-{batch.id}"
    store.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._ -]", "_", file.filename or "")[:180] or f"{client}.pdf"
    path = store / safe
    path.write_bytes(blob)

    flight = client_flight(db, client, account_ids,
                           cutoff=(cycle_for(period).lifetime_cutoff
                                   if is_lifetime and period else None))
    exp = expected_products(db, client, account_ids, period=period,
                            lifetime=is_lifetime, window=flight)
    ordered = ordered_for(db, client, account_ids, period,
                          lifetime=is_lifetime, window=flight)
    why = expected_why(db, client, account_ids, period=period)
    any_of = expected_any(db, client, account_ids, period=period)
    quiet = quiet_products(db, client, account_ids, period=period,
                           lifetime=is_lifetime)
    budgets = budgets_for(db, client, account_ids, period=period)
    orders_ok = not _orders_stale(db)
    # The corner of page one, and which other markets print the same mark.
    # Computed here rather than inside the checks because it takes a database
    # question, and a check is handed facts rather than going looking.
    from .checks.logo import header_logo_hash, is_generic
    logo = header_logo_hash(path)
    logo_bad = is_generic(db, logo)
    logo_seen = bool(db.scalar(select(func.count()).select_from(KnownLogo)))
    try:
        result = run_all(path, filename=file.filename, expected_products=exp,
                         flight=flight,
                         flight_lines=flight_lines(db, client, account_ids),
                         # The kind chosen on the form. A person saying this is
                         # a lifetime beats anything the file is called.
                         is_lifetime=is_lifetime,
                         period=period, market=market,
                     expected_why=why, expected_any=any_of,
                     quiet_products=quiet,
                     logo_hash=logo, logo_generic=logo_bad,
                     logo_known=logo_seen, budgets=budgets, ordered=ordered,
                     orders_current=orders_ok)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"That PDF could not be read: {exc}")

    meta = result["meta"]
    rep = Report(
        batch_id=batch.id, period=period, source="manual",
        filename=file.filename or safe, stored_path=str(path),
        # The row it was uploaded against wins over what the filename says -
        # somebody chose this row on purpose.
        client=client or meta.get("client", ""),
        account_ids=account_ids or meta.get("account_ids", ""),
        market=market, is_lifetime=is_lifetime,
        pages=result["pages"], impressions=result["impressions"],
        clicks=result["clicks"],
        products=", ".join(result.get("products") or []),
        severity=result["severity"], findings=result["findings"],
        checks=result.get("checks") or [], acked=[], review_state="new",
        rules_version=_rv())
    db.add(rep)
    db.flush()
    attach_owners(db, rep)
    db.commit()
    return RedirectResponse(f"/report/{rep.id}/view", status_code=303)


@app.get("/report/{report_id}/pending/file")
def pending_file(report_id: int, db: Session = Depends(get_db)):
    """The waiting file, so a decision can be made by looking at it."""
    rep = db.get(Report, report_id)
    if not rep or not rep.has_pending or not Path(rep.pending_path).exists():
        raise HTTPException(404)
    return FileResponse(rep.pending_path, media_type="application/pdf",
                        headers={"Content-Disposition":
                                 f'inline; filename="{rep.pending_name}"',
                                 "Cache-Control": "no-store"})


@app.post("/report/{report_id}/pending/{action}")
def resolve_pending(report_id: int, action: str, db: Session = Depends(get_db)):
    """Take the newer file that arrived, or throw it away.

    One of the two has to happen deliberately: the whole reason it is waiting
    is that overwriting this report would have thrown away a sign-off or
    somebody's manual upload without asking.
    """
    from .checks import run_all
    from .cycle import cycle_for
    from .ingest import client_flight, flight_lines
    from .roster import (budgets_for, expected_any, expected_products,
                     expected_why, ordered_for, quiet_products)
    from .version import rules_version as _rv

    rep = db.get(Report, report_id)
    if not rep:
        raise HTTPException(404)
    if not rep.has_pending:
        return RedirectResponse(f"/report/{report_id}/view", status_code=303)

    incoming = Path(rep.pending_path)
    if action == "discard":
        try:
            incoming.unlink()
        except OSError:
            pass
        rep.pending_path = rep.pending_name = ""
        rep.pending_at = None
        db.commit()
        return RedirectResponse(f"/report/{report_id}/view", status_code=303)

    if action != "accept":
        raise HTTPException(404)
    if not incoming.exists():
        rep.pending_path = rep.pending_name = ""
        rep.pending_at = None
        db.commit()
        raise HTTPException(400, "That file is no longer on disk.")

    # Overwrite in place, under the name this report already has, so every
    # link to it keeps working.
    target = Path(rep.stored_path) if rep.stored_path else incoming
    if target != incoming:
        target.write_bytes(incoming.read_bytes())
        try:
            incoming.unlink()
        except OSError:
            pass

    # A lifetime is measured against the campaign that ended, so its flight
    # stops at this cycle's lifetime window rather than at whatever else the
    # client still has running.
    flight = client_flight(db, rep.client, rep.account_ids,
                           cutoff=(cycle_for(rep.period).lifetime_cutoff
                                   if rep.is_lifetime and rep.period else None))
    exp = expected_products(db, rep.client, rep.account_ids, period=rep.period,
                            lifetime=bool(rep.is_lifetime), window=flight)
    ordered = ordered_for(db, rep.client, rep.account_ids, rep.period,
                          lifetime=bool(rep.is_lifetime), window=flight)
    why = expected_why(db, rep.client, rep.account_ids, period=rep.period)
    any_of = expected_any(db, rep.client, rep.account_ids, period=rep.period)
    quiet = quiet_products(db, rep.client, rep.account_ids, period=rep.period,
                           lifetime=bool(rep.is_lifetime))
    budgets = budgets_for(db, rep.client, rep.account_ids, period=rep.period)
    orders_ok = not _orders_stale(db)
    from .checks.logo import header_logo_hash, is_generic
    logo = header_logo_hash(target)
    logo_bad = is_generic(db, logo)
    logo_seen = bool(db.scalar(select(func.count()).select_from(KnownLogo)))
    result = run_all(target, filename=rep.filename, expected_products=exp,
                     flight=flight,
                     flight_lines=flight_lines(db, rep.client, rep.account_ids),
                     is_lifetime=bool(rep.is_lifetime),
                     period=rep.period, market=rep.market or "",
                     expected_why=why, expected_any=any_of,
                     quiet_products=quiet,
                     logo_hash=logo, logo_generic=logo_bad,
                     logo_known=logo_seen, budgets=budgets, ordered=ordered,
                     orders_current=orders_ok)
    rep.stored_path = str(target)
    rep.logo_hash = logo
    rep.pages = result["pages"]
    rep.impressions = result["impressions"]
    rep.clicks = result["clicks"]
    rep.products = ", ".join(result.get("products") or [])
    rep.severity = result["severity"]
    rep.findings = result["findings"]
    rep.checks = result.get("checks") or []
    rep.rules_version = _rv()
    # A different file, so the sign-off and the acceptances describe something
    # that is no longer on screen.
    rep.acked = []
    rep.review_state = "new"
    rep.reviewed_at = None
    rep.source = ""                       # it is the feed's copy now
    rep.pending_path = rep.pending_name = ""
    rep.pending_at = None
    db.commit()
    return RedirectResponse(f"/report/{report_id}/view", status_code=303)


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
    from .cycle import cycle_for
    from .ingest import client_flight, flight_lines
    from .roster import (budgets_for, expected_any, expected_products,
                     expected_why, ordered_for, quiet_products)

    rep = db.get(Report, report_id)
    if not rep:
        raise HTTPException(404)
    blob = await file.read()
    if not blob[:5] == b"%PDF-":
        raise HTTPException(400, "That is not a PDF.")

    # Whatever the file is called on your machine, it is filed under the name
    # this report already has. A corrected copy comes back as "... (1).pdf" or
    # "download.pdf" or whatever Acrobat felt like, and none of that should
    # decide which client the report belongs to.
    #
    # The one thing worth objecting to is a file named for a DIFFERENT report,
    # because replacing the wrong one silently is the expensive mistake here.
    wrong = _names_another_report(rep, file.filename or "")
    if wrong:
        raise HTTPException(400, wrong)

    # A NAME THAT KNOWS MORE THAN THE REPORT DOES IS WORTH READING.
    #
    # The rule above is "whatever it is called on your machine, it is filed
    # under the name this report already has" - which is right for a browser's
    # "download (2).pdf" and wrong when somebody has deliberately renamed the
    # file to correct it. Uploading "Lifetime_All Seasons Powersports 53908"
    # onto a report that had neither an order id nor a lifetime flag left both
    # of them missing. It is already established above that the name is not
    # some other report's, so what it adds is taken.
    up = meta_from_filename(file.filename or "")
    if up.get("account_ids") and not rep.account_ids:
        rep.account_ids = up["account_ids"]
    if up.get("is_lifetime"):
        rep.is_lifetime = True
    if up.get("client") and not rep.client:
        rep.client = up["client"]
    rep.filename = canonical_filename(rep)
    path = Path(rep.stored_path) if rep.stored_path else None
    if path is None:
        store = settings.data_dir / f"batch-{rep.batch_id}"
        store.mkdir(parents=True, exist_ok=True)
        path = store / rep.filename
    path.write_bytes(blob)

    # A lifetime is measured against the campaign that ended, so its flight
    # stops at this cycle's lifetime window rather than at whatever else the
    # client still has running.
    flight = client_flight(db, rep.client, rep.account_ids,
                           cutoff=(cycle_for(rep.period).lifetime_cutoff
                                   if rep.is_lifetime and rep.period else None))
    exp = expected_products(db, rep.client, rep.account_ids, period=rep.period,
                            lifetime=bool(rep.is_lifetime), window=flight)
    ordered = ordered_for(db, rep.client, rep.account_ids, rep.period,
                          lifetime=bool(rep.is_lifetime), window=flight)
    why = expected_why(db, rep.client, rep.account_ids, period=rep.period)
    any_of = expected_any(db, rep.client, rep.account_ids, period=rep.period)
    quiet = quiet_products(db, rep.client, rep.account_ids, period=rep.period,
                           lifetime=bool(rep.is_lifetime))
    budgets = budgets_for(db, rep.client, rep.account_ids, period=rep.period)
    orders_ok = not _orders_stale(db)
    from .checks.logo import header_logo_hash, is_generic
    logo = header_logo_hash(path)
    logo_bad = is_generic(db, logo)
    logo_seen = bool(db.scalar(select(func.count()).select_from(KnownLogo)))
    try:
        result = run_all(path, filename=rep.filename, expected_products=exp,
                         flight=flight,
                         flight_lines=flight_lines(db, rep.client, rep.account_ids),
                         is_lifetime=bool(rep.is_lifetime),
                         period=rep.period, market=rep.market or "",
                     expected_why=why, expected_any=any_of,
                     quiet_products=quiet,
                     logo_hash=logo, logo_generic=logo_bad,
                     logo_known=logo_seen, budgets=budgets, ordered=ordered,
                     orders_current=orders_ok)
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
    from .version import rules_version as _rv
    rep.rules_version = _rv()
    db.commit()
    return RedirectResponse(f"/report/{report_id}/view", status_code=303)


@app.get("/report/{report_id}/view", response_class=HTMLResponse)
def report_viewer(report_id: int, request: Request, db: Session = Depends(get_db)):
    rep = db.get(Report, report_id)
    if not rep:
        raise HTTPException(404)
    from .checks.logo import logo_reports
    from .checks.rules import SKIP_WHY
    from .version import rules_version
    peers = logo_reports(db, rep.logo_hash or "", exclude_id=rep.id)

    # PACING: what the month was bought to do, against what the report says it
    # did. Read here rather than stored with the findings because it is a
    # number to look at, not a verdict - it says nothing at all on most reports
    # and should not be another row in the checks list.
    pacing = []
    try:
        if rep.stored_path and Path(rep.stored_path).exists():
            from .checks.parser import pdf_text
            from .checks.served import pacing_rows
            from .cycle import cycle_for as _cyc
            from .roster import client_flight, ordered_for
            # A LIFETIME PACES AGAINST THE WHOLE CAMPAIGN, a monthly against
            # the month. Same panel, different question - and comparing a
            # nine-month report to one month's budget is how a perfectly normal
            # lifetime reads as 800% over.
            #
            # AND AGAINST THAT CAMPAIGN ONLY. Field Of Dreams' lifetime covers
            # Mobile Conquesting to 13 Jul; a Display order starting 28 Jul was
            # counted into the same goal and made the report 41% short of
            # 750,000 impressions it was never going to carry.
            life_flight = client_flight(
                db, rep.client, rep.account_ids,
                cutoff=(_cyc(rep.period).lifetime_cutoff
                        if rep.is_lifetime and rep.period else None))
            ordered = ordered_for(db, rep.client, rep.account_ids, rep.period,
                                  lifetime=bool(rep.is_lifetime),
                                  window=life_flight)
            if ordered:
                pacing = pacing_rows(pdf_text(Path(rep.stored_path)), ordered)
    except Exception:                       # a pacing panel is never worth a 500
        pacing = []
    try:
        queued = int(request.query_params.get("logo_queued") or 0)
    except ValueError:
        queued = 0
    came_from = request.headers.get("referer") or ""
    if "/report/" in came_from or not came_from.startswith("http"):
        came_from = ""
    from urllib.parse import urlparse as _urlparse
    if came_from:
        u = _urlparse(came_from)
        came_from = u.path + (f"?{u.query}" if u.query else "")
    # THE WAY BACK HAS TO SURVIVE THE PAGE RELOADING ITSELF.
    #
    # It was read off the referer, which is right exactly once - the first
    # arrival from the board. Accepting a finding, saving a note, re-checking
    # the file or replacing it all redirect back to this same page, and from
    # then on the referer IS this page, so the way back was blank and Reviewed
    # left you sitting on the report you had just signed off. Remembered in a
    # cookie so those round trips do not lose it.
    if not came_from:
        came_from = _back_cookie(request)
    resp = templates.TemplateResponse(request, "viewer.html",
                                      {"nav": "cycle", "rep": rep,
                                       "back": came_from,
                                       "skip_why": SKIP_WHY,
                                       "saved_as": canonical_filename(rep),
                                       # The page-one logo, so it can be
                                       # judged by somebody looking at it.
                                       "logo_hash": rep.logo_hash or "",
                                       # The logo panel needs a FILE, not a
                                       # fingerprint - see viewer.html.
                                       "has_file": bool(rep.stored_path)
                                       and Path(rep.stored_path).exists(),
                                       "logo_generic": _logo_is_generic(db, rep),
                                       # Who else carries it. A mark is a
                                       # statement about all of them, so they
                                       # are named on the page that takes it.
                                       "logo_peers": peers,
                                       "logo_queued": queued,
                                       "pacing": pacing,
                                       # Said HERE as well as on the board. A
                                       # product finding is disputed on this
                                       # page, so the one fact that settles it
                                       # belongs on this page.
                                       "orders_stale": _orders_stale(db),
                                       "orders_syncing": _orders_syncing(db),
                                       # Changes when the file does, so the
                                       # embedded viewer cannot show a copy it
                                       # cached before the replacement.
                                       "file_v": file_token(rep),
                                       "stale": bool(rep.rules_version)
                                       and rep.rules_version != rules_version()})
    if came_from:
        resp.set_cookie(BACK_COOKIE, came_from, max_age=BACK_COOKIE_MAX_AGE,
                        samesite="lax", path="/")
    return resp


@app.post("/report/{report_id}/recheck")
def report_recheck(report_id: int, db: Session = Depends(get_db)):
    """Re-read this one now, rather than waiting for the sweep to reach it."""
    from .recheck import recheck

    rep = db.get(Report, report_id)
    if not rep:
        raise HTTPException(404)
    out = recheck(db, rep, manual=True)
    if not out.get("ok"):
        rep.findings = (rep.findings or []) + [{
            "code": "missing_file", "severity": "warn",
            "title": "The stored PDF is gone",
            "detail": "Checks could not be re-run because the file is no longer "
                      "on disk. Old PDFs are pruned after "
                      f"{settings.keep_pdf_months} months. Upload it again below."}]
        db.commit()
    return RedirectResponse(f"/report/{report_id}/view", status_code=303)


@app.get("/cycle/recheck/status")
def cycle_recheck_status(period: str = Query(""), db: Session = Depends(get_db)):
    """What the running re-checks are doing, for the page to poll.

    Without this the count only moved when somebody reloaded, so a job that had
    stopped and a job that was working looked exactly the same.
    """
    from .recheck import running_jobs, stale_count
    period = period or settings.default_period or ""
    jobs = running_jobs(db)
    if period:
        jobs = {k: v for k, v in jobs.items() if not v["period"] or v["period"] == period}
    return {"jobs": list(jobs.values()),
            "stale": stale_count(db, period=period or None)}


@app.get("/partners", response_class=HTMLResponse)
def partners_view(request: Request, db: Session = Depends(get_db)):
    from .partners import all_partners
    rows = all_partners(db)
    # Where each partner delivers, counted. A market added since the last
    # roster export is not named in it at all and silently defaults to Drive.
    tally: dict[str, int] = {}
    for p in rows:
        t = (p.delivery_target or "drive")
        tally[t] = tally.get(t, 0) + 1
    try:
        just_set = int(request.query_params.get("set") or 0)
    except ValueError:
        just_set = 0
    return templates.TemplateResponse(request, "partners.html", {
        "partners": rows, "nav": "partners",
        "tally": sorted(tally.items()), "just_set": just_set})


@app.post("/partners/{partner_id}/target")
def partner_target(partner_id: int, target: str = Form(...),
                   db: Session = Depends(get_db)):
    """Where this partner's clients get their link.

    It comes off the roster, and a market added since the last export is not in
    it at all - which meant a silent default to Drive, and a Dropbox partner's
    client handed a Drive link with nothing on screen that looked wrong.
    """
    from .db import Partner
    from .partners import forget_partners
    if target not in {"drive", "dropbox", "local"}:
        raise HTTPException(400, "unknown delivery target")
    p = db.get(Partner, partner_id)
    if p is None:
        raise HTTPException(404)
    p.delivery_target = target
    db.commit()
    forget_partners()
    return RedirectResponse("/partners", status_code=303)


@app.post("/partners/target-bulk")
def partner_target_bulk(contains: str = Form(...), target: str = Form(...),
                        db: Session = Depends(get_db)):
    """Set the delivery target for every partner whose name matches.

    Setting a group of markets one dropdown at a time is how one of them gets
    missed, and the one that gets missed is the one whose client is handed the
    wrong link.
    """
    from .db import Partner
    from .partners import forget_partners
    if target not in {"drive", "dropbox", "local"}:
        raise HTTPException(400, "unknown delivery target")
    needle = (contains or "").strip().lower()
    if not needle:
        raise HTTPException(400, "nothing to match on")
    n = 0
    for p in db.scalars(select(Partner)).all():
        if needle in (p.partner or "").lower() or needle in (p.group or "").lower():
            if (p.delivery_target or "") != target:
                p.delivery_target = target
                n += 1
    db.commit()
    forget_partners()
    return RedirectResponse(f"/partners?set={n}", status_code=303)


@app.post("/partners/import")
async def partners_import(file: UploadFile = File(...), db: Session = Depends(get_db)):
    from .partners import import_partners
    import_partners(db, await file.read())
    return RedirectResponse("/partners", status_code=303)


# TapClicks will not export more than this many days in one go.
TAP_MAX_DAYS = 2000


def pull_plan(db: Session, today: dt.date | None = None,
              max_days: int = TAP_MAX_DAYS) -> list[dict]:
    """Per partner: the range its export needs, and how to split it.

    The end date is TODAY for everybody - a campaign that launched this morning
    has to be in the file. The start is the earliest line item still running,
    because TapClicks filters on the start date rather than on delivery.

    Where that span is longer than TapClicks will export in one go, it is cut
    into consecutive windows, OLDEST FIRST, each of them the maximum length
    except the last. Cutting from the recent end instead would leave the odd
    remainder on the oldest window, which is the one nobody wants to run twice.
    """
    today = today or dt.date.today()
    out = []
    for market, earliest, n in pull_range_rows(db):
        if earliest is None:
            continue
        span = (today - earliest).days + 1
        windows = []
        start = earliest
        while start <= today:
            end = min(start + dt.timedelta(days=max_days - 1), today)
            windows.append((start, end))
            start = end + dt.timedelta(days=1)
        out.append({"market": market or "(no partner)", "from": earliest,
                    "to": today, "days": span, "lines": n,
                    "windows": windows, "pulls": len(windows)})
    return out


def _sync_triggers() -> dict:
    from .orders_s3 import TRIGGERS
    return TRIGGERS


def pull_strategy(db: Session, today: dt.date | None = None,
                  max_days: int = TAP_MAX_DAYS) -> dict:
    """The cheapest way to pull everything, given the tool's two options.

    TapClicks will export ONE partner or ALL partners, and at most 2,000 days.
    One pull per partner is a hundred and forty-six runs, which nobody is
    going to do daily. Two all-partner windows is two runs but twice the whole
    board's rows, which is the thing being avoided.

    The cheap answer is neither. 2,000 days back from today is a cutoff, and
    almost every partner's oldest still-running line item is after it. So:

        one ALL-PARTNERS pull covering the most recent 2,000 days
        plus one SINGLE-PARTNER pull for each partner that started earlier

    The stragglers only need the part BEFORE the cutoff - the bulk pull already
    has the rest of them - so each is a narrow slice of one partner's history,
    not another copy of the board. Rows are the whole board once plus a
    handful, and runs are one plus however few stragglers there are.
    """
    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=max_days - 1)

    stragglers = []
    for market, earliest, n in pull_range_rows(db):
        if earliest is None or earliest >= cutoff:
            continue
        # Only the part the bulk pull does not already cover.
        windows, start, last = [], earliest, cutoff - dt.timedelta(days=1)
        while start <= last:
            end = min(start + dt.timedelta(days=max_days - 1), last)
            windows.append((start, end))
            start = end + dt.timedelta(days=1)
        stragglers.append({"market": market or "(no partner)", "from": earliest,
                           "to": last, "lines": n, "windows": windows,
                           "pulls": len(windows)})

    covered = sum(1 for _m, e, _n in pull_range_rows(db) if e and e >= cutoff)
    extra = sum(s["pulls"] for s in stragglers)
    return {"cutoff": cutoff, "today": today, "max_days": max_days,
            "bulk": (cutoff, today), "stragglers": stragglers,
            "covered": covered, "runs": 1 + extra, "extra_runs": extra}


def pull_range_rows(db: Session) -> list[tuple]:
    """(partner, earliest start still running, live line items), oldest first.

    Only lines that are STILL RUNNING. A campaign that finished in 2011 does
    not need to be in the pull, and letting it set a partner's date is how one
    dead line item keeps a daily sync at a couple of million rows.
    """
    rows = db.execute(
        select(OrderLine.market,
               func.min(OrderLine.starts_on),
               func.count(OrderLine.id))
        .where(OrderLine.live.is_(True))
        .group_by(OrderLine.market)).all()
    return sorted(((m, e, n) for m, e, n in rows),
                  key=lambda r: (r[1] or dt.date.max, r[0] or ""))


@app.get("/report/{report_id}/orders")
def report_orders(report_id: int, request: Request, db: Session = Depends(get_db)):
    """Every order line this report is being judged against, as stored.

    Not the summary the finding prints - the actual rows, with the product they
    were mapped to, whether they are live, their windows, and which sync loaded
    them. Three rounds of "why am I still seeing this" all came down to the
    stored rows being older than the code, and there was no way to look at them
    without me guessing from a screenshot.
    """
    from .checks.products import map_order_products
    from .roster import _ran_during, client_lines
    from .version import product_map_version

    rep = db.get(Report, report_id)
    if not rep:
        raise HTTPException(404)
    lines = client_lines(db, rep.client, rep.account_ids) or []
    rows = []
    for l in sorted(lines, key=lambda x: (x.product or "", x.account_ids or "")):
        rows.append({
            "product": l.product or "(unmapped)",
            "raw": l.campaign or "",
            # What TODAY's code would map that raw name to. When this differs
            # from the stored product, the row was written by an older import
            # and that is the whole answer.
            "would_be": ", ".join(map_order_products(l.campaign or "")) or "(nothing)",
            "orders": l.account_ids or "", "lines": l.line_ids or "",
            "live": bool(getattr(l, "live", True)),
            "flights": getattr(l, "flights", None) or [],
            "starts": l.starts_on, "ends": l.ends_on,
            "budget": l.budget,
            "ran": _ran_during(l, rep.period) if rep.period else None,
        })
    from .orders_s3 import running_sync
    sync = db.scalars(select(OrderSync).where(OrderSync.state != "running")
                      .order_by(desc(OrderSync.id)).limit(1)).first()
    ctx = {
        "nav": "cycle", "rep": rep, "rows": rows, "sync": sync,
        "map_now": product_map_version(),
        "stale": bool(sync and sync.ok and
                      (sync.map_version or "") != product_map_version()),
        # Pressing Re-read the orders used to land back on a page that looked
        # exactly the same, because the sync takes minutes. These two say what
        # happened.
        "running": running_sync(db),
        "started": request.query_params.get("sync") in ("started", "already"),
        "frag": bool(request.query_params.get("frag")),
    }
    # frag=1 is the same content with no page around it, for the modal on the
    # report. One template, so the two cannot drift apart.
    if request.query_params.get("frag"):
        return templates.TemplateResponse(request, "report_orders_body.html", ctx)
    return templates.TemplateResponse(request, "report_orders.html", ctx)


@app.get("/orders/pull-range.csv")
def pull_range_csv(db: Session = Depends(get_db)):
    """The earliest start date each partner's pull actually needs.

    TapClicks filters the export on the line item's START date, not on
    delivery, so a campaign that began in 2018 and is still running is only in
    the file if the range reaches back to 2018. That is why the guidance says
    2018 for the whole board - but it is one date for a hundred and forty-six
    partners, and almost none of them need it.

    Per partner, most of them need a range measured in months. Pull those
    narrow and the handful of genuinely old ones on their own, and a daily sync
    stops being a couple of million rows.
    """
    import csv as _csv
    import io as _io

    plan = pull_plan(db)
    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["Partner", "Pull from", "Pull to", "Days", "Pulls needed",
                "Live line items", "Windows"])
    for r in plan:
        w.writerow([r["market"], r["from"].isoformat(), r["to"].isoformat(),
                    r["days"], r["pulls"], r["lines"],
                    "; ".join(f"{a} to {b}" for a, b in r["windows"])])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             'attachment; filename="pull-range-by-partner.csv"'})


@app.post("/orders/budgets")
async def orders_budgets(file: UploadFile = File(...),
                         db: Session = Depends(get_db)):
    """Fill in the money columns from a sheet, without touching anything else.

    MERGE, NEVER REPLACE. A file covering one product put through the normal
    import would delete every order for every other product and leave that one
    standing. This only ever updates a budget on a line that is already there.
    """
    from .budgets import import_budgets
    res = import_budgets(db, await file.read(), file.filename or "budgets.xlsx")
    msg = (f"Budgets from {file.filename}: {res['rows_read']:,} rows read, "
           f"{res['lines_updated']:,} order line(s) updated "
           f"({res['matched_on_line_item']:,} matched on line item id, "
           f"{res['matched_on_order']:,} on order id).")
    if res["not_on_the_board"]:
        msg += (f" {res['not_on_the_board']:,} line item(s) in the sheet are not "
                f"on the board - the sheet is ahead of the last sync.")
    db.add(OrderSync(source=f"budgets: {file.filename}", rows=res["lines_updated"],
                     ok=True, message=msg))
    db.commit()
    return RedirectResponse(f"/orders?budgets={res['lines_updated']}",
                            status_code=303)


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
        if res.get("header_overruled"):
            msg += (f", {res['header_overruled']:,} line item(s) kept on their "
                    f"own status against an order header that disagreed")
        db.add(OrderSync(source=f"upload: {file.filename}", rows=n, ok=True,
                         message=msg + ".", guidance=res.get("guidance") or {}))
        db.commit()
    return RedirectResponse(f"/orders?imported={n}", status_code=303)


def _run_sync(claim_id: int) -> None:
    """The actual work, off the request."""
    db = SessionLocal()
    try:
        sync_orders(db, force=True, claim_id=claim_id, trigger="button")
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
def orders_sync(background: BackgroundTasks, back: str = Form(""),
                db: Session = Depends(get_db)):
    """Start the sync and answer immediately.

    It downloads about 850 MB and parses a couple of million rows. Doing that
    inside the request meant the browser sat on an open connection for minutes
    with no response - which is what made the page look hung. The order list
    now shows a running state and refreshes itself.
    """
    from .orders_s3 import begin_sync
    claim = begin_sync(db, trigger="button")
    # Only a path on this app, so the button cannot be turned into an open
    # redirect by anybody who can post a form at it.
    home = back if back.startswith("/") and not back.startswith("//") else "/orders"
    sep = "&" if "?" in home else "?"
    if claim is None:
        return RedirectResponse(f"{home}{sep}sync=already", status_code=303)
    background.add_task(_run_sync, claim.id)
    return RedirectResponse(f"{home}{sep}sync=started", status_code=303)


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
