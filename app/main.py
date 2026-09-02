from __future__ import annotations

import datetime as dt
import os
import re
import time as _time
from pathlib import Path
from urllib.parse import quote

from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form, HTTPException,
                     Query, Request, UploadFile)
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, desc, func, select
from sqlalchemy.orm import Session

from . import brand, selfcheck, version
from .config import settings
from .db import (Batch, Delivery, Inbound, KnownLogo, OrderLine, OrderSync,
                 Partner, Report, SessionLocal, WorkerBoot, init_db)
from .ingest import (finish_batch, parse_postmark, process_batch,
                    prune_old_pdfs, sweep_stale)
from .lookup import find as _find
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

# WHAT THE SERVER ITSELF SAW. Every guess at why the pages felt slow was made
# without a single server-side number to check it against - see app/timing.py.
from . import timing                                    # noqa: E402
from sqlalchemy import event as _sa_event               # noqa: E402
from sqlalchemy.engine import Engine as _SAEngine       # noqa: E402


# ON THE ENGINE CLASS, NOT ON ONE ENGINE.
#
# The tests rebuild app.db against a temporary sqlite file, which makes a new
# engine object - and a listener attached to the engine this module imported at
# start-up would then be watching something nobody uses. The count would come
# back zero and read exactly like a page that runs no queries.
def _qa_query_start(conn, cursor, statement, params, context, many):
    context._qa_started = _time.perf_counter()


def _qa_query_end(conn, cursor, statement, params, context, many):
    started = getattr(context, "_qa_started", None)
    if started is not None:
        timing.note_query(_time.perf_counter() - started)


# ONCE, however many times this module is imported. The tests reload it, and a
# listener registered twice counts every query twice - which would have the
# board reporting fifty-two queries it never ran.
if not timing.LISTENING:
    _sa_event.listen(_SAEngine, "before_cursor_execute", _qa_query_start)
    _sa_event.listen(_SAEngine, "after_cursor_execute", _qa_query_end)
    timing.LISTENING = True


# Long enough that normal pages never write a row, short enough that anything
# anybody would call slow does.
SLOW_SECONDS = 3.0


def _log_slow(request, status: int, took: float, box: dict) -> None:
    from .db import SlowRequest, SessionLocal as _SL
    db = _SL()
    try:
        load = timing.load_average()
        db.add(SlowRequest(
            path=str(request.url.path)[:200], method=request.method,
            status=status, seconds=round(took, 3),
            db_seconds=round(box["db"], 3), queries=box["queries"],
            phases=dict(box["phases"]), pid=os.getpid(),
            rss_mb=timing.rss_mb() or 0.0, load1=round(load[0], 2) if load else 0.0,
            build=version.BUILD))
        db.commit()
    except Exception:                                        # noqa: BLE001
        db.rollback()
    finally:
        db.close()


@app.middleware("http")
async def _stopwatch(request: Request, call_next):
    box = timing.start_counting()
    started = _time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        took = _time.perf_counter() - started
        timing.record(request.url.path, request.method, status, took,
                      box["queries"], box["db"], box["phases"])
        # A REQUEST SLOW ENOUGH TO COMPLAIN ABOUT GOES IN THE DATABASE.
        #
        # The list above is in memory and per worker, so seeing it means being
        # at the screen while it happens and catching the right one of the two.
        # This one can be read afterward.
        path = request.url.path
        # A SLOW HEALTH CHECK IS THE ONE WE MOST NEED WRITTEN DOWN.
        #
        # It was excluded because it is trivial and never interesting - and
        # then Render started mailing about health checks timing out and there
        # was no evidence at all, only a service that was plainly fine by the
        # time anybody looked. It is trivial, so ANY delay on it is news: the
        # threshold is one second rather than three, and it says what else the
        # box was doing.
        if path.startswith("/healthz"):
            if took >= 1.0:
                _log_slow(request, status, took, box)
        elif took >= SLOW_SECONDS:
            _log_slow(request, status, took, box)


# ------------------------------------------------------------ when it breaks
#
# "INTERNAL SERVER ERROR" IN TIMES NEW ROMAN AND NOTHING ELSE.
#
# That is what a 500 looked like: three words, no build, no page, no reason. So
# the only way to find out what had actually happened was to go and read
# Render's logs, and the only way to report one was a screenshot of three words
# that are the same three words for every fault there is.
#
# This is an internal tool behind a password. The people who see this page are
# the people who need to know what it says, so it says it: what broke, where,
# and which build it broke on - and it logs the whole traceback either way.
@app.exception_handler(Exception)
def _oops(request: Request, exc: Exception):
    import logging
    import traceback
    logging.getLogger("report-qa").error("500 on %s", request.url.path,
                                         exc_info=exc)
    # THIS TOOL'S OWN FRAMES, not the twenty of middleware above them. Those
    # are identical on every fault and push the one line that matters off the
    # bottom of the screenshot.
    frames = traceback.extract_tb(exc.__traceback__)
    mine = [f for f in frames if f"{os.sep}app{os.sep}" in (f.filename or "")]
    # And wherever it finally raised, which on a library call is not one of
    # ours and is usually the line that names the problem.
    if frames and (not mine or frames[-1] is not mine[-1]):
        mine = mine + [frames[-1]]
    if mine:
        tail = ("".join(traceback.format_list(mine)).rstrip() + "\n"
                + "".join(traceback.format_exception_only(type(exc), exc)).strip())
    else:
        tail = "".join(traceback.format_exception(
            type(exc), exc, exc.__traceback__)[-6:]).strip()
    from .version import BUILD
    body = f"""<!doctype html><meta charset="utf-8">
<title>Something broke - Report QA</title>
<style>body{{font:15px/1.6 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;
 margin:0;background:#0F2544;color:#E8EDF4}}
 main{{max-width:900px;margin:0 auto;padding:48px 24px}}
 h1{{font-size:26px;margin:0 0 6px}} p{{color:#A9B7CB;margin:0 0 20px}}
 pre{{background:#0A1B33;border:1px solid #1E3A5F;border-radius:10px;padding:16px;
 overflow:auto;font-size:12.5px;color:#D6E0EC;white-space:pre-wrap}}
 a{{color:#F0B429}} code{{color:#F0B429}}</style>
<main>
<h1>Something broke on this page.</h1>
<p>Nothing was lost - this is the page failing to draw, not the data.
Build <code>{BUILD}</code>, path <code>{request.url.path}</code>.
Send this whole page and it can be fixed without guessing.</p>
<pre>{_esc(tail)}</pre>
<p><a href="/cycle">Back to the board</a></p>
</main>"""
    return HTMLResponse(body, status_code=500)


def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

# ---------------------------------------------------------------- the door
# ONE SHARED PASSWORD, OR NONE AT ALL.
#
# Set SITE_PASSWORD and every page asks for it once and remembers on that
# browser. Blank leaves the site open, which is what it has been. This is an
# internal tool behind a link nobody outside has; what is worth stopping is a
# forwarded link, not a colleague, so accounts would be ceremony for nothing.
GATE_COOKIE = "qa_pass"
GATE_MAX_AGE = 60 * 60 * 24 * 30


def _gate_token() -> str:
    import hashlib
    secret = settings.site_password.strip()
    # The cookie carries a hash, not the password. A cookie is readable by
    # anybody with the laptop, and the password is one somebody chose and may
    # well have used elsewhere.
    return hashlib.sha256(("report-qa:" + secret).encode()).hexdigest()[:32]


@app.middleware("http")
async def _password_gate(request: Request, call_next):
    from fastapi.responses import HTMLResponse as _HTML

    secret = settings.site_password.strip()
    path = request.url.path
    if not secret or any(path.startswith(p.strip())
                         for p in settings.open_paths.split(",") if p.strip()):
        return await call_next(request)
    if request.cookies.get(GATE_COOKIE) == _gate_token():
        return await call_next(request)

    if request.method == "POST" and path == "/unlock":
        form = await request.form()
        # compare_digest, so a wrong password does not leak how much of it was
        # right in how long the answer took.
        import hmac
        if hmac.compare_digest((form.get("password") or "").strip(), secret):
            to = form.get("next") or "/"
            if not to.startswith("/") or to.startswith("//"):
                to = "/"
            resp = RedirectResponse(to, status_code=303)
            resp.set_cookie(GATE_COOKIE, _gate_token(), max_age=GATE_MAX_AGE,
                            samesite="lax", path="/", httponly=True,
                            secure=request.url.scheme == "https")
            return resp
        return _HTML(_lock_page(path, bad=True), status_code=401)
    return _HTML(_lock_page(str(request.url.path)), status_code=401)


@app.post("/unlock")
async def unlock(request: Request):
    """The same door, as a real route.

    The middleware answers this while the browser is locked out; once it is
    in, the middleware waves the request through and something has to be here
    to catch it. Without this, submitting the form twice - or a stale tab
    posting it - returned a 404 that read as the site being broken.
    """
    import hmac

    from fastapi.responses import HTMLResponse as _HTML
    secret = settings.site_password.strip()
    form = await request.form()
    to = form.get("next") or "/"
    if not to.startswith("/") or to.startswith("//"):
        to = "/"
    if not secret or hmac.compare_digest((form.get("password") or "").strip(),
                                         secret):
        resp = RedirectResponse(to, status_code=303)
        if secret:
            resp.set_cookie(GATE_COOKIE, _gate_token(), max_age=GATE_MAX_AGE,
                            samesite="lax", path="/", httponly=True,
                            secure=request.url.scheme == "https")
        return resp
    return _HTML(_lock_page(to, bad=True), status_code=401)


def _lock_page(nxt: str, bad: bool = False) -> str:
    import html as _html
    return f"""<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Report QA</title>
<style>
 body{{margin:0;min-height:100vh;display:grid;place-items:center;
  background:#0E2233;color:#FDFBF7;
  font:400 15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}}
 form{{width:min(360px,90vw);text-align:left}}
 h1{{font-size:20px;margin:0 0 4px;letter-spacing:.02em}}
 p{{margin:0 0 18px;color:#8FA5B8;font-size:13.5px}}
 input{{width:100%;box-sizing:border-box;font:inherit;padding:11px 14px;
  border-radius:24px;border:1px solid #2A4055;background:#0A1B29;color:#FDFBF7}}
 input:focus{{outline:2px solid #E8B54B;outline-offset:1px}}
 button{{margin-top:10px;width:100%;font:inherit;font-weight:600;padding:11px 14px;
  border-radius:24px;border:0;background:#E8B54B;color:#14293C;cursor:pointer}}
 .bad{{color:#F6B0A8;font-size:13px;margin:10px 0 0}}
</style>
<form method="post" action="/unlock">
  <h1>Report QA</h1>
  <p>Internal tool. Enter the password to continue.</p>
  <input type="password" name="password" autofocus required
         autocomplete="current-password" placeholder="Password">
  <input type="hidden" name="next" value="{_html.escape(nxt, quote=True)}">
  <button type="submit">Unlock</button>
  {'<p class="bad">That password is not right.</p>' if bad else ''}
</form>"""

_HERE = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(_HERE / "static")), name="static")

templates = Jinja2Templates(directory=str(_HERE / "templates"))


# HOW MUCH OF THE PAGE WAS DRAWING IT.
#
# Jinja renders inside TemplateResponse, so wrapping this one call times every
# page in the tool without touching a single route. Building the rows and
# rendering them are separate problems with separate fixes, and a total that
# does not separate them is what sent the last two builds after the wrong one.
_render = templates.TemplateResponse


def _timed_template_response(*a, **kw):
    started = _time.perf_counter()
    try:
        return _render(*a, **kw)
    finally:
        timing.mark("render", _time.perf_counter() - started)


templates.TemplateResponse = _timed_template_response


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


from .cycle import month_label as _month_label      # noqa: E402

templates.env.filters["month"] = _month_label


def _human_hours(h):
    from .pace import humanize
    return humanize(h)


def _working_days(h):
    from .pace import working_days
    return working_days(h)


def _sum_orders(rows) -> dict:
    """The totals row under the order lines.

    Safe to add up, which is the whole reason it can exist: one line item
    selling two products carries its money against the FIRST of them and
    nothing against the second, so "CTV + Video Ads" counts once here rather
    than twice. None stays out of the sum - "the order does not say" is not a
    figure of nothing.
    """
    out = {"orders": 0, "ran": 0, "impressions": 0.0, "budget": 0.0,
           "total_impressions": 0.0, "total_budget": 0.0}
    orders = set()
    for r in rows:
        for oid in str(r.get("order") or "").split(","):
            if oid.strip():
                orders.add(oid.strip())
        if r.get("ran"):
            out["ran"] += 1
        for key in ("impressions", "budget", "total_impressions", "total_budget"):
            v = r.get(key)
            if v:
                out[key] += float(v)
    out["orders"] = len(orders)
    return out


templates.env.filters["humanhours"] = _human_hours
templates.env.filters["workingdays"] = _working_days
templates.env.filters["sum_orders"] = _sum_orders


def _io_kind(status: str) -> str:
    """Which color an order status gets on the board.

    Green live, orange paused, red cancelled, blue complete. Read off the
    words rather than off a fixed list, because the export writes "Cancelled"
    on the order header and "IO Cancelled" on a line item and they mean the
    same thing.
    """
    s = (status or "").strip().lower()
    if "cancel" in s:
        return "cancelled"
    if "complete" in s:
        return "complete"
    if "paus" in s:
        return "paused"
    if "live" in s:
        return "live"
    return "other"


templates.env.filters["iokind"] = _io_kind
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
# security boundary and is not treated as one - nothing is authorized by it.
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
    # IS THE CODE ON THIS BOX ONE BUILD? A deploy here is a zip of the files
    # that changed, and the day one gets missed the box runs half of one build
    # and half of another. That is not a state any test can catch, because a
    # test always has the whole tree. It shows at the top of every page.
    half_deployed=selfcheck.check,
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
    # BEFORE ANYTHING ELSE: is this box running one build, or two halves of
    # two? Nothing below this line can tell the difference, and the symptom is
    # an ImportError on a page nobody has opened yet.
    selfcheck.check()
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
        # AND THE HEARTBEAT. The order export, the daily serve file and the
        # breakout sheet are all read on this, so none of them depends on
        # somebody pressing something.
        from .clock import start as start_clock
        start_clock()
    except Exception:
        import traceback; traceback.print_exc()

    # THIS WORKER CAME UP. Written down because a restart is the likeliest
    # reason for a slow page and the only one that erases its own evidence.
    db = SessionLocal()
    try:
        db.add(WorkerBoot(pid=os.getpid(), build=version.BUILD,
                          service=version.service()))
        db.commit()
        # Two workers restarting a few times a day for a year is still only a
        # few thousand rows, but there is no reason to keep last month's.
        cutoff = dt.datetime.utcnow() - dt.timedelta(days=14)
        db.query(WorkerBoot).filter(WorkerBoot.at < cutoff).delete()
        db.commit()
    except Exception:                                        # noqa: BLE001
        db.rollback()
    finally:
        db.close()


@app.get("/healthz")
async def healthz():
    """Includes the build so you can confirm what is actually live without
    trusting the dashboard, which looks identical either way.

    ASYNC ON PURPOSE, AND IT DOES NOTHING.

    A plain `def` endpoint is handed to the threadpool, so it queues behind
    whatever else is in there - and the one time this route matters is the one
    time the box is busy. Render gives up on it after five seconds and calls
    the service down, which is what happened overnight while reports were
    coming in: a health check that failed on a service that was working
    perfectly, just busy.

    Answered on the event loop, it needs one slice of the interpreter rather
    than a free thread. Nothing in here touches the database or the disk: the
    question this route answers is "is this process alive", and anything else
    it asked could fail for a reason that is not that.
    """
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
    # AND WHETHER THIS BOX IS RUNNING ONE BUILD. Empty is the answer you want;
    # anything in it names a file that was missed on the way up.
    out["half_deployed"] = selfcheck.check()
    out["ok"] = not out["half_deployed"]
    db = SessionLocal()
    try:
        from .recheck import stale_count
        out["awaiting_recheck"] = stale_count(db)
    except Exception as exc:  # noqa: BLE001
        out["awaiting_recheck"] = f"unknown: {exc}"
    finally:
        db.close()
    return out


def _boot_rows(db: Session, hours: int = 24) -> list[WorkerBoot]:
    since = dt.datetime.utcnow() - dt.timedelta(hours=hours)
    return db.scalars(select(WorkerBoot).where(WorkerBoot.at >= since)
                      .order_by(desc(WorkerBoot.at)).limit(80)).all()


@app.get("/why-slow", response_class=HTMLResponse)
def why_slow(request: Request, db: Session = Depends(get_db)):
    """What the server thinks it is spending its time on.

    Every theory about the slowness so far has been argued from local timings
    against a copy of the data, which said 1.4 seconds while the real thing
    said a minute. This is the page that settles it from the box itself.
    """
    boots = _boot_rows(db)
    hour_ago = dt.datetime.utcnow() - dt.timedelta(hours=1)
    recent_boots = [b for b in boots if b.at >= hour_ago]
    boots_hour = len(recent_boots)
    # A WORKER STARTING IS NOT THE SERVICE RESTARTING.
    #
    # One row is written per worker PROCESS, and there are three of them - so a
    # single deploy writes three rows and "restarted 15 times in the last hour"
    # is five deploys wearing a costume. That reads as a box falling over on a
    # day somebody was only installing builds.
    #
    # Starts within a minute of each other are one event, which is what a
    # deploy looks like from in here.
    events = []
    for b in sorted(recent_boots, key=lambda x: x.at):
        if events and (b.at - events[-1][-1].at).total_seconds() <= 90:
            events[-1].append(b)
        else:
            events.append([b])
    restarts_hour = len(events)
    # How many workers are actually up: distinct processes in the newest event.
    workers = len({b.pid for b in events[-1]}) if events else 0
    try:
        from .recheck import running_jobs, stale_count
        # THE SAME NUMBER THE BOARD SHOWS. This asked unscoped - every period,
        # signed-off ones included - so the board said 799 and this page said
        # 2,029 about the same queue, which makes both of them untrustworthy.
        queue = stale_count(db, scoped=True, skip_signed=True)
        queue_all = stale_count(db)
        jobs = running_jobs(db)
    except Exception:                                        # noqa: BLE001
        queue, queue_all, jobs = None, None, {}
    from .db import SlowRequest
    slow = db.scalars(select(SlowRequest)
                      .order_by(desc(SlowRequest.at)).limit(60)).all()
    return templates.TemplateResponse(request, "why_slow.html", {
        "request": request, "nav": "whyslow",
        "summary": timing.summary(),
        "verdict": timing.verdict(restarts_hour, workers),
        "recent": timing.recent(60), "slow": slow,
        "slow_seconds": SLOW_SECONDS,
        "boots": boots, "boots_hour": boots_hour,
        "restarts_hour": restarts_hour, "workers": workers,
        "queue": queue, "queue_all": queue_all, "jobs": jobs,
        "last_sync": last_sync(db),
        "now": dt.datetime.utcnow(),
    })


@app.get("/healthz/slow")
def healthz_slow(db: Session = Depends(get_db)):
    """The same numbers as JSON, for pasting into a message."""
    boots = _boot_rows(db)
    hour_ago = dt.datetime.utcnow() - dt.timedelta(hours=1)
    boots_hour = sum(1 for b in boots if b.at >= hour_ago)
    from .db import SlowRequest
    slow = db.scalars(select(SlowRequest)
                      .order_by(desc(SlowRequest.at)).limit(15)).all()
    return {"ok": True, **version.info(), **timing.summary(),
            "boots_last_hour": boots_hour, "boots_last_24h": len(boots),
            "verdict": timing.verdict(boots_hour),
            "recent": timing.recent(25),
            "slow": [{"at": s.at.isoformat(timespec="seconds"), "path": s.path,
                      "seconds": s.seconds, "db_seconds": s.db_seconds,
                      "queries": s.queries, "phases": s.phases,
                      "load1": s.load1, "rss_mb": s.rss_mb, "pid": s.pid}
                     for s in slow]}


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
    # AND THE OTHER WAY ROUND: A PARTNER ON THE ROSTER WITH NO ORDERS AT ALL.
    #
    # The export used to be one file for the whole board, so "did every partner
    # come through" was not a question. It is one file per partner now, and a
    # file that did not land is invisible: the partner simply has no orders, no
    # expected reports, and nothing on the board saying so. This is the check
    # that says which - and it is what to look at before deleting the old
    # whole-board export.
    # What the serving file says about the cycle being worked, if one is loaded.
    from .board import MIN_DAYS_IN_MONTH
    from .cycle import current_period
    from .serving import served_days
    _p = settings.default_period or current_period()
    # A PARTNER WITH NO ORDERS IS NOT EVIDENCE OF ANYTHING. 125 of 158 came
    # back on the first look, and most of them simply have nothing running -
    # so the panel was crying wolf at a number nobody could act on.
    #
    # THE SERVING FILE IS THE EVIDENCE. A client that DELIVERED impressions
    # this month and has no order line at all cannot be a campaign that went
    # dark or a spelling the two tools disagree on: something was running and
    # there is nothing here to judge it against. That is what a file failing to
    # land looks like, said by name.
    from .serving import served_but_no_order, tailing_off
    missing_orders = served_but_no_order(db, _p)
    # AND THE ONES SET ASIDE AS A CAMPAIGN TRAILING OFF. Counted rather than
    # silently dropped - a panel that quietly stops mentioning things is one
    # nobody can check.
    missing_tail = tailing_off(db, _p)
    from .serving import matched_on_base_name
    name_split = matched_on_base_name(db, _p)
    _days = served_days(db, _p)
    if _days:
        from .serving import unmatched as _unmatched
        from .serving import unmatched_count as _unmatched_n
        served = {"period": _p, "clients": len(_days),
                  "ran": sum(1 for n in _days.values() if n >= MIN_DAYS_IN_MONTH),
                  "unmatched": _unmatched(db, _p),
                  # THE NUMBER, not a sample of it. "40+" hid the difference
                  # between a few dark campaigns and the board losing three
                  # hundred rows.
                  "unmatched_n": _unmatched_n(db, _p)}
    else:
        served = None
    # THE SERVING UPLOAD'S OWN RESULT, in the serving panel. It shares the sync
    # log with the order export, and a serving file that would not parse was
    # reporting itself up in the S3 box as "last sync failed" - about a sync
    # nobody ran.
    # HOW MUCH ROOM IS LEFT. A full disk does not announce itself: it comes back
    # as an unrelated write failing somewhere else, which is how "Downloaded but
    # could not import: OSError [Errno 28]" happened on a file that was fine.
    from .orders_s3 import disk_free
    _free, _total = disk_free()
    disk = {"free": _free, "total": _total,
            "pct": round((_total - _free) / _total * 100) if _total else 0}
    serve_log = db.scalars(
        select(OrderSync).where(OrderSync.source.like("serving upload:%"))
        .order_by(OrderSync.id.desc()).limit(1)).first()
    return templates.TemplateResponse(request, "orders.html", {
        "lines": lines, "sync": sync_rec, "guidance": guidance, "running": running,
        # PARTNERS ON THE ROSTER WITH NO ORDERS AT ALL. See _partners_with_no_orders.
        "no_orders": _partners_with_no_orders(db),
        "s3": settings.s3_configured,
        "served": served, "min_days": MIN_DAYS_IN_MONTH, "serve_log": serve_log,
        "nav": "orders", "view": view, "legend": legend,
        "clients": clients, "no_roster": no_roster, "disk": disk,
        "missing_orders": missing_orders, "missing_tail": missing_tail,
        "name_split": name_split,
        "period": _p, "serving_prefix": settings.serving_file_prefix,
        # HOW OFTEN IT LOOKS, so "when will my new file show up" is answered on
        # the page rather than by asking. Every panel here says when it last
        # read something and none of them said when it will look again.
        "sync_every": max(settings.sync_every_minutes or 0, 5)
                      if settings.sync_every_minutes else 0,
        # PRODUCT NAMES THE MAP HAS NEVER SEEN. Read off the loaded lines
        # rather than off the last sync message, so it is true whether or not
        # anybody was watching when the export came in.
        "unmapped": _unmapped_products(db),
        # ONE BOX FOR "IS THIS ORDER IN THE LISTS". Everything needed to answer
        # it was already loaded and there was no way to ask.
        "find_q": (request.query_params.get("find") or "").strip(),
        "found": (_find(db, request.query_params.get("find") or "", _p)
                  if (request.query_params.get("find") or "").strip() else None),
        "env_report": settings.env_report(),
        "plan": pull_plan(db), "tap_max_days": TAP_MAX_DAYS,
        # Three different things can start a sync, and none of them used to
        # say so - which is why finding one running looked like the tool
        # deciding to do something on its own.
        "triggers": _sync_triggers(),
        "strategy": _strategy_with_reasons(db),
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


def _partners_with_no_orders(db: Session) -> list[str]:
    """Roster partners the loaded order list has never heard of.

    "AM I MISSING ORDERS?" HAD NO ANSWER BEFORE THIS. The order export is split
    across several files - one per partner for some, and one "all" file
    covering everybody else - and if one of those stops arriving, or a partner
    is in none of them, their clients simply are not on the board. No error, no
    empty table, nothing: the cycle is just quietly smaller.

    The reporting breakout sheet knows every partner there is. Anything on it
    with not one order line loaded is either genuinely dormant this month or
    missing from the export, and those two are worth telling apart by hand.
    """
    from .board import excluded
    from .partners import all_partners
    have = {_ident_key(m) for (m,) in
            db.execute(select(OrderLine.market).distinct()).all() if m}
    out = []
    for p in all_partners(db):
        name = (p.partner or "").strip()
        if not name or excluded(name) or _ident_key(name) in have:
            continue
        out.append(name)
    return sorted(out)


def _unmapped_products(db: Session) -> list[tuple[str, int]]:
    """[(product name, how many clients carry it)] for names the map misses.

    An unmapped product no longer deletes its client from the board - it is
    kept under the name the export gave it - but nothing can judge a report
    against it either, so it has to be visible or it stays unmapped forever.
    """
    from .roster import is_mapped

    out: dict[str, int] = {}
    for product, in db.execute(select(OrderLine.product)).all():
        if product and not is_mapped(product):
            out[product] = out.get(product, 0) + 1
    return sorted(out.items(), key=lambda kv: (-kv[1], kv[0]))[:20]


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
# ONE TABLE, FIFTY AT A TIME.
#
# The open reports and the signed-off ones were two tables in two sections,
# which meant the same report moved house the moment somebody signed it - and
# finding it again took knowing that it had. They are one grid now, with the
# signed-off ones filtered out by default and a page of fifty.
PAGE_SIZE = 50
# AND TWENTY PARTNER CARDS. A hundred and fifty of them is four screens of
# scrolling before the reports table, which is the part of the page anybody is
# actually working in.
CARD_PAGE = 20


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
    from .orders_s3 import NOT_A_SYNC
    row = db.scalars(select(OrderSync)
                     .where(OrderSync.state != "running",
                            ~OrderSync.source.like(NOT_A_SYNC))
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
            # WHEN, not just whether. "Package again" gave no way to tell a
            # link made before this morning's corrections from one made after.
            "at": d.created_at,
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
               q: str = Query(""), done_: str = Query("", alias="done"),
               page: int = Query(1), cards: int = Query(1),
               # THE CARD FILTERS, READ HERE RATHER THAN ONLY IN THE BROWSER.
               #
               # They were browser state: the JS ticked the boxes off the URL
               # and then hid cards. With twenty cards a page that only ever
               # filtered twenty, so a saved view for Lockwood opened on page
               # one, found no Lockwood card among the twenty, and hid all of
               # them - a named view that reliably showed nothing. Values are
               # pipe-separated, which is what the browser writes.
               partner: str = Query(""), buyer: str = Query(""),
               reporter: str = Query(""), trainer: str = Query(""),
               status: str = Query(""), only: str = Query(""),
               # ROWS SOMEBODY PUT ON THIS CYCLE THEMSELVES. Read HERE and not
               # in the browser, for the same reason the search is: the table
               # is fifty rows a page, so a filter that only sees what is
               # rendered found one of thirteen and said so with a straight
               # face.
               hand: str = Query(""),
               db: Session = Depends(get_db)):
    from .board import (MIN_DAYS_IN_MONTH, STATE_LABEL, by_group, expected_for,
                        summary)
    from .checks.products import every_product
    from .cycle import current_period, cycle_for, recent_periods
    from .delivery import (delivery_jobs, latest_deliveries, out_of_sync,
                           out_of_sync_why)
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
    # THE FILTER MENUS ARE BUILT FROM THIS, not from what survives the filter.
    # Offering only the values still showing means one pick and the menu can
    # never take you anywhere else.
    every_group = list(groups)
    if group:
        groups = [g for g in groups if g.group == group]

    # Narrowed BEFORE the page is cut, so a filter reaches the whole cycle and
    # not the twenty cards that happen to be on screen.
    def _picked(val: str) -> list:
        return [x.strip() for x in (val or "").split("|") if x.strip()]

    def _any_of(field: str, want: list) -> bool:
        return any(p.strip() in want for p in (field or "").split(",") if p.strip())

    if _picked(partner):
        want = _picked(partner)
        groups = [g for g in groups if g.group in want]
    for val, attr in ((buyer, "buyer"), (reporter, "reporter"),
                      (trainer, "trainer")):
        if _picked(val):
            want = _picked(val)
            groups = [g for g in groups if _any_of(getattr(g, attr, ""), want)]
    if _picked(status):
        want = _picked(status)
        groups = [g for g in groups
                  if ("Good to go" if g.ready else "Open") in want]
    if "arrived" in _picked(only):
        groups = [g for g in groups if g.counts.missing < len(g.expected)]
    card_filters = {"partner": _picked(partner), "buyer": _picked(buyer),
                    "reporter": _picked(reporter), "trainer": _picked(trainer),
                    "status": _picked(status), "only": _picked(only)}
    # THE CARDS PAGE TOO, and the search reaches past the page.
    #
    # A search that only looked at the twenty cards on screen would be worse
    # than no search at all, so a query narrows the partners first and the page
    # applies to what is left.
    if q.strip():
        hit = [g for g in groups
               if any(_matches(e, q) for e in g.expected)
               or all(w in g.group.lower() for w in q.lower().split())]
        if hit:
            groups = hit
    card_total = len(groups)
    card_pages = max(1, (card_total + CARD_PAGE - 1) // CARD_PAGE)
    cards = max(1, min(cards, card_pages))
    shown_groups = groups[(cards - 1) * CARD_PAGE:cards * CARD_PAGE]

    rows = [e for g in groups for e in g.expected]
    if state:
        rows = [e for e in rows if e.state == state]
    # SEARCHING THE WHOLE CYCLE, NOT THE PAGE.
    #
    # The box above the table filtered the rows the browser had, and the table
    # is capped at 150 of 763 - so "paul" said "28 of 150 rows" and looked like
    # it had searched everything. Enter now sends the search here, where the
    # other 613 are.
    if q.strip():
        rows = [e for e in rows if _matches(e, q)]
    # Counted before the filter, so the chip can say how many there are even
    # while it is on and the rest are hidden.
    hand_total = sum(1 for e in rows if e.forced_by)
    if hand:
        rows = [e for e in rows if e.forced_by]
    # ONE GRID, WITH THE FINISHED WORK FILTERED OUT RATHER THAN MOVED AWAY.
    #
    # Signed-off reports used to live in their own collapsed section at the
    # bottom, so a report changed which table it was in the moment somebody
    # signed it, and finding it again meant knowing that. It is a filter now:
    # hidden by default, in the same grid the moment the filter is cleared.
    # THREE BUCKETS, AND PENDING IS WHERE THE WORK IS.
    #
    #   pending    still open - the default, because that is the job
    #   completed  signed off and clear
    #   all        both, in one grid
    #
    # They used to be two tables in two sections, so a report changed which
    # table it was in the moment somebody signed it and finding it again meant
    # knowing that.
    bucket = done_ if done_ in {"pending", "completed", "all"} else (
        "all" if done_ in {"1", "yes"} else "pending")
    show_done = bucket in {"completed", "all"}
    # A ROW WITH A FILE WAITING ON IT IS OPEN, whatever its sign-off says.
    # Parking a newer file deliberately leaves the sign-off alone - the copy
    # that was signed off is still the copy the partner gets - so the row was
    # landing in Completed, and its amber tag with it, in the bucket nobody
    # reads.
    done_hidden = sum(1 for e in rows if not e.open_row)
    open_count = len(rows) - done_hidden
    if bucket == "pending":
        rows = [e for e in rows if e.open_row]
    elif bucket == "completed":
        rows = [e for e in rows if not e.open_row]
    # ARRIVED FIRST. Two thirds of a cycle has not been sent yet, so in market
    # order the reports there is something to DO about sit below a screenful of
    # "Not received". Stable, so market and client order is kept inside each
    # half - and a signed-off report sorts last of all when they are shown.
    rows.sort(key=lambda e: (1 if e.ready else 0, 0 if e.report else 1))

    # The reports table was 24,851 of the page's 30,342 DOM nodes and four
    # seconds of browser time. The server was never the slow part.
    total = len(rows)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    shown = rows if show_all else rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]
    from .product_codes import pill
    chips = {e.ident: [pill(p) for p in e.products] for e in shown}
    # A pinned period outside the last thirteen months would not be in the
    # dropdown, and the board would show a cycle you could not switch back to.
    periods = recent_periods()
    if period not in periods:
        periods = sorted(set(periods) | {period}, reverse=True)
    return templates.TemplateResponse(request, "cycle.html", {
        "nav": "cycle", "cycle": cyc, "period": period, "chips": chips,
        "periods": periods, "groups": shown_groups, "all_groups": groups,
        # THE WHOLE CYCLE'S FILTER OPTIONS, not this page's.
        #
        # The dropdowns were built from the cards on screen, so with twenty a
        # page the Partner filter offered twenty and called it "All (20)" - a
        # partner on page five could not be picked or even seen to exist.
        "opts": _card_options(every_group),
        "card_filters": card_filters,
        "card_page": cards, "card_pages": card_pages, "card_total": card_total,
        "rows": shown, "row_total": total,
        "show_done": show_done, "done_hidden": done_hidden,
        "bucket": bucket, "open_count": open_count,
        "page": page, "pages": pages, "page_size": PAGE_SIZE,
        "show_all": show_all, "row_cap": ROW_CAP,
        # Where an order id goes when you click it. The board says a campaign
        # is owed a report; the next question is always what that order says.
        "io_order_url": settings.io_order_url,
        "summary": summary(exp), "state_label": STATE_LABEL,
        # "763 not received" does not answer the question anybody has, which is
        # whether that is a morning's work or the rest of the week.
        "pace": pace(db, period, summary(exp)["missing"]),
        "filter_group": group, "filter_state": state, "q": q,
        "deliveries": latest_deliveries(db, period),
        # WHICH PACKAGED PARTNERS ARE SHOWING A LINK TO OLD FILES. Reports get
        # corrected all cycle and the folder only changes when somebody presses
        # sync, so a partner can be handing out a perfectly good link to last
        # Tuesday's work.
        "stale_pack": {g.group: out_of_sync(g) for g in groups},
        # AND WHY EACH OF THEM IS BEHIND. Twenty-six client names
        # said nothing about what happened to twenty-six reports.
        "stale_why": {g.group: out_of_sync_why(g) for g in groups},
        # Packaging runs in the background, so the card has to say where it is.
        "packing": delivery_jobs(db),
        # The finished links, at the top. A partner that is done sorts in with
        # 145 others, so the one thing you came to the page for - the link you
        # are about to send - was found by scrolling.
        "delivered": delivered,
        "views": _saved_views(db),
        "not_owed": sorted(not_owed, key=lambda r: (r["market"] or "",
                                                    r["client"] or "")),
        # WHAT THE ADD-A-ROW FORM OFFERS. Typed-in partner names were the
        # obvious first cut and the wrong one: the board is keyed on the
        # partner's real name, and a name spelled a hair differently makes a
        # row that groups with nothing.
        "all_markets": sorted({m for g in groups for m in (g.markets or [])}
                              | {g.group for g in groups if g.group}),
        "all_products": every_product(),
        # The hand-added chip beside the search: how many there are, and
        # whether it is on.
        "hand_total": hand_total, "hand_on": bool(hand),
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


def _card_options(groups) -> dict:
    """Every value each card filter could offer, across the whole cycle."""
    out = {"partner": set(), "buyer": set(), "reporter": set(),
           "trainer": set(), "status": set()}
    for g in groups:
        if g.group:
            out["partner"].add(g.group)
        for key, val in (("buyer", g.buyer), ("reporter", g.reporter),
                         ("trainer", g.trainer)):
            for part in (val or "").split(","):
                part = part.strip()
                if part:
                    out[key].add(part)
        # THE TWO VALUES A CARD ACTUALLY CARRIES. This offered every report
        # state - Not received, Errors, In review - and a card is labeled
        # "Good to go" or "Open", so picking any of them matched no card at
        # all and the board went empty.
        out["status"].add("Good to go" if g.ready else "Open")
    return {k: "|".join(sorted(v)) for k, v in out.items()}


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
    if state not in {"new", "reviewed", "waived", "needs_fix"}:
        raise HTTPException(400, "unknown review state")
    name = who.strip() or whoami(request)
    # CLEARING A VERDICT NEEDS NOBODY'S NAME. Every other state records who
    # said so; this one is the absence of anybody having said anything.
    if (not name and state != "new") or not ids:
        return RedirectResponse(back, status_code=303)
    now = dt.datetime.utcnow()
    # Capped. The form is built from what is on screen, but the request is not
    # trusted to be, and a runaway list should not become a table scan.
    for rep in db.scalars(select(Report).where(Report.id.in_(ids[:500]))).all():
        rep.review_state = state
        rep.reviewed_by = "" if state == "new" else name
        rep.reviewed_at = None if state == "new" else now
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
    # BACK TO "NEW" MEANS BACK TO NOTHING.
    #
    # This is how a sign-off is taken back, and the only two verdicts on offer
    # were the two other verdicts - so undoing a Reviewed meant marking it
    # Needs fix, which is a different statement about the report and keeps it
    # out of the partner's folder. Leaving the name on a row with no verdict is
    # the same mistake one field along: it reads as signed by somebody.
    rep.reviewed_by = "" if state == "new" else name
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
                  ref: str = Form(""), products: list[str] = Form([]),
                  db: Session = Depends(get_db)):
    """Check a row off for this cycle with no PDF behind it.

    SEO is done outside TapClicks, so there is no report to upload and the row
    sat at "Not received" all month, holding its partner off ready. This marks
    it handled for THIS cycle only - next month it is back on the board asking
    for a report.
    """
    from .board import _key as board_key
    from .db import CycleDone
    if kind not in {"monthly", "lifetime", "seo"}:
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
    # TWO WAYS OFF THE BOARD, AND THEY MEAN DIFFERENT THINGS.
    #
    # "done" is somebody did the work and there is no PDF to show for it - SEO,
    # mostly. "none" is that no report was owed at all, which is the answer a
    # paused order needs: the export cannot tell paused-on-the-2nd from
    # paused-on-the-30th, and it takes a person to say the campaign did not run
    # this month. Both clear the row for THIS cycle only.
    name = who.strip() or whoami(request) or "checked off"
    if row is None:
        row = CycleDone(period=period, ident=ident)
        db.add(row)
    row.market, row.client, row.kind = market, client, kind
    # THREE OVERRIDES, AND THEY ARE NOT THE SAME STATEMENT.
    #   done    somebody did the work, there is just no PDF (SEO)
    #   none    no report was owed - it did not run
    #   needed  the rules took it off and they are wrong about this one
    row.reason = action if action in {"done", "none", "needed"} else "done"
    row.note = note.strip()[:255]
    # THE ORDER NUMBER, so the row that gets built carries it and a search for
    # the order finds the row. A hand-added row with no order id on it is
    # findable only by the client's name, which is the spelling the two tools
    # disagree about in the first place.
    if ref.strip():
        row.ref = ref.strip()[:64]
    # WHAT THE REPORT IS SUPPOSED TO CARRY. A hand-added row arrived naming no
    # products, because nothing knew them - so it read as a blank beside a
    # hundred rows that name their buy, and the product checks had nothing to
    # judge the PDF against. The person adding the row knows.
    picked = [p.strip() for p in products if p and p.strip()]
    if picked:
        row.products = ", ".join(dict.fromkeys(picked))[:512]
    row.marked_by = name
    row.marked_at = dt.datetime.utcnow()
    db.commit()
    resp = RedirectResponse(back, status_code=303)
    if who.strip():
        _remember(resp, who)
    return resp


@app.post("/cycle/{period}/deliver")
def deliver_group(request: Request, period: str, group: str = Form(...),
                  force: str = Form(""), tag: str = Form(""),
                  back_to: str = Form(""), ready_only: str = Form(""),
                  db: Session = Depends(get_db)):
    # IT RUNS IN THE BACKGROUND NOW.
    #
    # It uploads every PDF in the partner one after another, which on a big one
    # is several minutes - and it was doing that inside this request, so the
    # browser sat on a spinner with no way to tell a slow upload from a dead
    # one. The card shows "12 of 30" while it works and the link appears on it
    # when it finishes.
    from .delivery import start_delivery
    # A TAG MAKES A SECOND LINK, beside the partner's own one rather than
    # instead of it. Kept to what will read as a folder name six months from
    # now, because that is what it becomes.
    tag = re.sub(r"[^A-Za-z0-9 _-]", "", tag).strip()[:40]
    start_delivery(db, period, group, force=bool(force), tag=tag,
                   ready_only=bool(ready_only))
    # PACKAGING LIVES ON THE LINKS PAGE NOW, so that is where it goes back to.
    if back_to == "links":
        # STRAIGHT TO THE ROW, ALREADY RUNNING. The sync is started above, so
        # what is wanted here is to watch it, not to find the button again.
        return RedirectResponse(
            f"/cycle/links?period={period}&new={quote(group)}#g-{_anchor(group)}",
            status_code=303)
    back = f"/cycle?period={period}"
    if group:
        back += f"&group={quote(group)}"
    return RedirectResponse(back, status_code=303)


def _matches(e, q: str) -> bool:
    """Every word has to appear somewhere on the row, in any order.

    The same rule the box on the page uses, so pressing Enter widens the search
    rather than changing it.
    """
    hay = " ".join(str(x or "") for x in (
        e.market, e.group, e.client, e.kind, e.account_ids, e.line_ids,
        e.buyer, e.reporter, e.state,
        ", ".join(e.products or []),
        " ".join(f.get("title", "") for f in
                 ((e.report.open_findings if e.report else []) or [])),
    )).lower()
    return all(w in hay for w in q.lower().split())


def _rename(rep, arrived: str, db=None) -> None:
    """File this report under its built name, and remember the one it came in
    under when that name was missing or wrong about the order id.

    A file that arrived without its order id came out of a folder somebody put
    together by hand, and that is worth seeing on the report rather than being
    quietly corrected.
    """
    from .checks.parser import meta_from_filename as _mfn
    from .naming import ids_for_report

    if db is not None:
        ids = ids_for_report(db, rep)
        if ids:
            rep.account_ids = ids
    built = canonical_filename(rep)
    arrived = (arrived or "").strip()
    if built != arrived:
        was = _mfn(arrived)
        mine = {i for i in (rep.account_ids or "").replace(",", " ").split()}
        theirs = set((was.get("account_ids") or "").split())
        if mine and theirs != mine:
            rep.renamed_from = arrived[:512]
    rep.filename = built


@app.get("/cycle/audit", response_class=HTMLResponse)
@app.post("/cycle/audit", response_class=HTMLResponse)
def cycle_audit(request: Request, period: str = Form(""), group: str = Form(""),
                rows: str = Form(""), db: Session = Depends(get_db)):
    """Where the board and somebody's hand-kept list disagree.

    The board is built from the order export; the reporting tracker is built
    from what people know. Checking one against the other meant reading 42 rows
    of a spreadsheet against 42 rows of a web page.
    """
    from .audit import audit
    from .board import STATE_LABEL
    from .cycle import current_period

    from .db import AuditList

    period = period or settings.default_period or current_period()
    saved = db.scalars(select(AuditList)
                       .where(AuditList.period == period)).first()

    # KEPT, SO A REFRESH DOES NOT LOSE IT.
    #
    # Four hundred rows of somebody's tracker, pasted, compared, and gone the
    # moment the page reloads - or worse, the browser offering to send it all
    # again. The way this check actually gets used is: read it, go and fix
    # three rows, look again. That needs the list to still be here.
    if request.method == "POST":
        if rows.strip():
            if saved is None:
                saved = AuditList(period=period)
                db.add(saved)
            saved.rows = rows[:400_000]
            saved.group = group[:255]
            saved.saved_by = whoami(request)
            saved.saved_at = dt.datetime.utcnow()
            db.commit()
        elif saved is not None:
            db.delete(saved)                 # submitted empty: forget it
            db.commit()
            saved = None
    elif saved is not None:
        # Coming back to the page: the list that is here is the list to run.
        rows, group = saved.rows, saved.group

    result = audit(db, period, rows, group) if rows.strip() else None
    # THE CALLS ALREADY MADE ON THIS CYCLE'S LIST, so a row somebody has
    # already ruled on comes back with the ruling on it rather than as a fresh
    # question.
    from .db import AuditCall
    calls = {(c.ref, c.kind): c for c in db.scalars(
        select(AuditCall).where(AuditCall.period == period)).all()}
    return templates.TemplateResponse(request, "audit.html", {
        "nav": "audit", "period": period, "group": group, "saved": saved,
        "rows_text": rows, "result": result, "state_label": STATE_LABEL,
        # WHEN THE EXPORT WAS READ. Half the answers on this page end with "or
        # the export is out of date", and there was nothing on the page that
        # said whether that was even a possibility.
        "synced": last_sync(db),
        "calls": calls, "me": whoami(request)})


@app.post("/cycle/audit/call")
def cycle_audit_call(request: Request, period: str = Form(""),
                     ref: str = Form(""), kind: str = Form("monthly"),
                     client: str = Form(""), market: str = Form(""),
                     market_hint: str = Form(""),
                     call: str = Form(""), note: str = Form(""),
                     who: str = Form(""), db: Session = Depends(get_db)):
    """Approve or reject one row of the pasted list.

    APPROVED PUTS IT ON THE BOARD. Not a note about a row that stays missing -
    the same override the "not owed" panel uses, so the client appears in the
    cycle with a report expected and whatever was typed here as the reason.
    That is what makes this a decision rather than a comment.

    REJECTED IS ALSO A DECISION, and it sticks: the row is still on the list
    next month, and this is what stops forty of them being worked out again
    from scratch every time somebody opens the page.
    """
    from .db import AuditCall, CycleDone
    from .cycle import current_period

    # ANSWERED IN PLACE, NOT BY REDRAWING THE PAGE.
    #
    # A redirect back here re-runs the whole comparison - the board is rebuilt
    # from every order line and the pasted list is parsed again - to change one
    # cell. That was the best part of a minute per decision, and it threw away
    # the scroll position, so working down a list of forty meant scrolling back
    # to where you were forty times.
    #
    # The page posts this with fetch and updates the row itself. Without
    # JavaScript the form still submits and still redirects, which is the same
    # slow correct behavior it had before.
    wants_json = "application/json" in (request.headers.get("accept") or "")

    def done(ok: bool = True):
        if wants_json:
            return JSONResponse({"ok": ok, "call": call, "note": note,
                                 "who": name if ok else "",
                                 "at": _eastern(dt.datetime.utcnow(), "%b %-d")})
        return RedirectResponse("/cycle/audit", status_code=303)

    period = period or settings.default_period or current_period()
    ref = (ref or "").strip()[:255]
    name = (who or "").strip() or whoami(request)
    if not ref or call not in ("approved", "rejected", "clear"):
        return done(False)

    row = db.scalars(select(AuditCall).where(
        AuditCall.period == period, AuditCall.ref == ref,
        AuditCall.kind == kind)).first()

    def drop_the_override():
        """Take back the board row an earlier approve on this line put there.

        CHANGING YOUR MIND HAS TO REACH THE BOARD. Approving writes a "keep
        this on the cycle" override, and rejecting or clearing later only
        rewrote the word on this page. The override stayed, so the list said
        Rejected while the board still carried the row somebody had rejected.
        Two screens, two answers, and the one nobody was looking at was the one
        the reporters work from.
        """
        prev = row.client if row is not None else ""
        for mark in db.scalars(select(CycleDone).where(
                CycleDone.period == period, CycleDone.kind == kind,
                CycleDone.reason == "needed")).all():
            hit = _ident_key(mark.client) == _ident_key(client or prev)
            if not hit and ref:
                # The approve found the client off the order id, so a reject
                # arriving with only the id has to find it the same way.
                for l in db.scalars(select(OrderLine)).all():
                    if ref in (l.account_ids or "").replace(",", " ").split():
                        hit = _ident_key(l.client) == _ident_key(mark.client)
                        if hit:
                            break
            if hit:
                db.delete(mark)

    if call == "clear":
        drop_the_override()
        if row is not None:
            db.delete(row)
        db.commit()
        return done()

    if row is None:
        row = AuditCall(period=period, ref=ref, kind=kind)
        db.add(row)
    row.client = (client or "")[:255]
    row.call = call
    row.note = (note or "")[:2000]
    row.who = name[:128]
    row.at = dt.datetime.utcnow()

    # AND APPROVED ACTUALLY PUTS IT ON THE BOARD.
    #
    # THE PARTNER COMES FROM THE ORDER LINE, not from the pasted list - the
    # list carries a partner CODE ("7MOU SG") and the board is keyed on the
    # partner's real name. Looked up by order id, which is the one thing both
    # sides agree on.
    #
    # And when there is no order line at all, there is nothing to put back:
    # the board is built from the order export, so a client the export has
    # never heard of cannot have a row. The decision is still recorded, and the
    # page says which of the two happened rather than looking like it worked.
    # A REJECT AFTER AN APPROVE TAKES THE ROW BACK OFF.
    if call == "rejected":
        drop_the_override()

    if call == "approved" and not market:
        for l in db.scalars(select(OrderLine)).all():
            ids = (l.account_ids or "").replace(",", " ").split()
            if ref in ids or (client and _ident_key(l.client) == _ident_key(client)):
                market, client = l.market, l.client
                break
    # AN APPROVE HAS TO WORK WHEN THE EXPORT HAS NEVER HEARD OF THE CLIENT.
    #
    # That is the case it exists for. 53872 is not in the order export, which
    # is exactly why it is on this table - and the approve found no order line,
    # so it set no market, wrote nothing, and said APPROVED. Silently doing
    # nothing is the worst of the three possible behaviors.
    #
    # The market code off the tracker row is the fallback. It is not the
    # partner's real name, but it names them well enough to group the row, and
    # the board materializes the override into a row of its own - see
    # expected_for.
    if call == "approved" and client and not market:
        market = (market_hint or "").strip() or "(not in the export)"
    if call == "approved" and market and client:
        ident = f"{_ident_key(market)}|{_ident_key(client)}|{kind}"
        mark = db.scalars(select(CycleDone).where(
            CycleDone.period == period, CycleDone.ident == ident)).first()
        if mark is None:
            mark = CycleDone(period=period, ident=ident)
            db.add(mark)
        mark.market = market[:255]
        mark.client = client[:255]
        mark.kind = kind
        mark.reason = "needed"
        # So the board can show the order number and a search can find it.
        mark.ref = (ref or "")[:64]
        mark.marked_by = name[:128]
        mark.note = (note or "Approved from the list check")[:255]
        mark.marked_at = dt.datetime.utcnow()
        row.note = (row.note or "")
        row.client = client[:255]
    db.commit()
    return done()


def _ident_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


@app.get("/rules", response_class=HTMLResponse)
def rules_view(request: Request):
    """The rules the board applies, in words.

    They were only in the code, which means the only way to answer "why is this
    not asking for a report" was to read it - and every one of these rules came
    out of a conversation about a row somebody did not expect.
    """
    from .board import MIN_DAYS_IN_MONTH, SHORT_CAMPAIGN_DAYS
    from .checks.rules import GOAL_BAND
    # AND EVERY FLAG IT RAISES, on a second tab. The findings list on a report
    # only ever shows what went wrong on that report, so "what does this thing
    # actually look for" could only be answered by having seen enough reports.
    from .flag_catalog import flags
    ctx = {"nav": "rules", "min_days": MIN_DAYS_IN_MONTH,
           "short_days": SHORT_CAMPAIGN_DAYS, "flags": flags(),
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
    # PACKAGING MOVED HERE FROM THE CARD. The card is where you judge reports;
    # this is where you hand links over, and re-packaging belongs beside the
    # link it replaces rather than three screens away from it.
    from .delivery import tagged_deliveries
    from .db import DeliveryJob
    ready_now = {g.group for g in groups if g.ready}
    running = {}
    for j in db.scalars(select(DeliveryJob).where(
            DeliveryJob.period == period, DeliveryJob.state == "running")).all():
        if not j.stalled:
            running[j.partner_group] = {"done": j.done, "total": j.total,
                                        "note": j.note}
    extra = tagged_deliveries(db, period)
    # WHICH DRIVE FOLDER EACH MARKET IS FILED IN, and the chance to say.
    #
    # The folder is matched by name against a drive organized by hand over ten
    # years. That works until somebody fixes a folder - pulls one out of
    # another, renames it, merges two - and then a best guess is not good
    # enough against work somebody did on purpose.
    from .db import Partner
    from .delivery import _key_market
    pinned = {_key_market(p.partner): (getattr(p, "drive_folder_id", "") or "")
              for p in db.scalars(select(Partner)).all()}
    markets_of = {}
    for g in groups:
        seen_m = []
        for e in g.expected:
            if e.market and e.market not in seen_m:
                seen_m.append(e.market)
        markets_of[g.group] = [
            {"market": m, "pin": pinned.get(_key_market(m), "")}
            for m in sorted(seen_m)]
    for l in delivered["links"]:
        l["ready"] = l["group"] in ready_now
        l["running"] = running.get(l["group"])
        l["extra"] = [{"tag": d.tag, "url": d.share_url or "",
                       "archive": d.archive_url or "", "reports": d.reports or 0}
                      for d in extra.get(l["group"], [])]
        l["markets"] = markets_of.get(l["group"], [])
        # How many of this partner are still open, so "Good to go only" can say
        # what it would leave behind - and not appear at all when it would
        # leave nothing.
        g = next((x for x in groups if x.group == l["group"]), None)
        l["open"] = sum(1 for e in g.expected if not e.ready) if g else 0
    # And partners that are ready but have never been packaged - the button for
    # those was on the card too.
    packaged = {l["group"] for l in delivered["links"]}
    # AND WHAT STATE EACH ONE IS IN, so a partner sitting in this list is
    # diagnosable from the list rather than by going back to the board.
    fails = {d.group: d for d in db.scalars(
        select(Delivery).where(Delivery.period == period, Delivery.ok.is_(False))
        .order_by(Delivery.id)).all()}
    # A PARTNER WITH TWO OF TWENTY DONE APPEARED NOWHERE.
    #
    # This list was "ready and not packaged", so a partner still being worked
    # through was on neither list - not here, because it is not finished, and
    # not above, because nothing has gone out. Which is exactly the partner
    # somebody wants to send the finished two for.
    waiting = []
    for g in groups:
        if g.group in packaged:
            continue
        ready_n = sum(1 for e in g.expected if e.ready)
        if not ready_n:
            continue
        bad = fails.get(g.group)
        waiting.append({
            "group": g.group,
            "reports": len(g.expected),
            "ready": ready_n,
            "open": len(g.expected) - ready_n,
            "target": g.target or settings.delivery_target,
            "running": running.get(g.group),
            "why": (bad.message or "") if bad else "",
            "when": bad.created_at if bad else None,
        })
    waiting.sort(key=lambda w: w["group"].lower())
    return templates.TemplateResponse(request, "links.html", {
        "nav": "links", "cycle": cycle_for(period), "period": period,
        "anchor": _anchor,
        "periods": periods, "delivered": delivered, "new": new,
        "waiting": waiting, "running": running,
        "configured": settings.delivery_configured,
    })


def _anchor(name: str) -> str:
    """A partner's name as something that can be an id and a URL fragment."""
    return re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")


DRIVE_FOLDER_ID = re.compile(r"(?:folders/|[?&]id=)([A-Za-z0-9_-]{10,})")


@app.post("/cycle/pin-folder")
def pin_drive_folder(period: str = Form(""), market: str = Form(...),
                     folder: str = Form(""), db: Session = Depends(get_db)):
    """Say which Drive folder a market's reports are filed in.

    Matching a folder by name works until somebody fixes one by hand. Pasting
    the folder's own address settles it, and takes the guess out of the loop
    entirely for that market.
    """
    from .db import Partner
    from .delivery import _key_market

    want = _key_market(market)
    row = None
    for p in db.scalars(select(Partner)).all():
        if _key_market(p.partner) == want:
            row = p
            break
    if row is None:
        raise HTTPException(404, f"{market} is not on the roster.")
    # A URL OR A BARE ID. Nobody has the id to hand; everybody has the address
    # bar, and pasting that is one action instead of three.
    txt = (folder or "").strip()
    m = DRIVE_FOLDER_ID.search(txt)
    row.drive_folder_id = (m.group(1) if m else txt)[:128]
    db.commit()
    return RedirectResponse(f"/cycle/links?period={period}", status_code=303)


@app.get("/cycle/packing")
def packing_status(period: str = Query(""), db: Session = Depends(get_db)):
    """Which packaging runs are still going, for the page watching one.

    The links page used to reload itself with a meta refresh written into a
    list item, which browsers honour in the document head and nowhere else. So
    a finished job sat on its first frame until somebody pressed refresh - the
    one thing a progress indicator must never do.
    """
    from .delivery import delivery_jobs
    jobs = {k: v for k, v in delivery_jobs(db).items()
            if not period or v.get("period") == period}
    return {"jobs": jobs}


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
    from .partners import find as find_partner, first_name, resolve_owner, role_names
    pool = role_names(db, role)
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
        # FIRST NAMES, AND IT IS ALSO WHAT GROUPS THE ROWS.
        #
        # The sheet spells the same person two ways - "Lauren" on one partner
        # and "Lauren Hunter" on another - and counted as written that is two
        # people with half a workload each. One tab per role, so the trainer
        # Katie and the buyer Katie are never in the same list to be confused.
        who = first_name(who, pool) or "(unassigned)"
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
    # asking for a signature after is asking the same question twice, and
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
    # AND AN AUTO-REVIEW GOES BACK TO THE BOARD, like pressing Reviewed does.
    #
    # Ticking the last finding is the last thing you do with a report, so
    # landing on it again means scrolling the board back to where you were -
    # the same walk pressing the button by hand used to be.
    if auto:
        return RedirectResponse(_back_cookie(request) or "/cycle", status_code=303)
    back = request.headers.get("referer") or f"/report/{report_id}/view"
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


def _is_seo_row(db: Session, client: str, account_ids: str,
                period: str) -> bool:
    """Is this row nothing but SEO?

    SEO is pulled outside TapClicks and does not look like a Digital Marketing
    Report, so the checks cannot judge it. Read off the client's order lines
    rather than asked of the person uploading, because it is the same answer
    every month and one more box to tick is one more box to forget.
    """
    from .partners import is_seo
    from .roster import client_lines

    hit = client_lines(db, client, account_ids)
    products = [l.product for l in (hit or []) if l.product]
    return bool(products) and all(is_seo(p) for p in products)


@app.post("/cycle/upload")
async def upload_for_expected(period: str = Form(""), market: str = Form(""),
                              client: str = Form(""), account_ids: str = Form(""),
                              kind: str = Form("monthly"),
                              skip_checks: str = Form(""),
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
    # A REPORT THE CHECKS CANNOT JUDGE.
    #
    # SEO is pulled outside TapClicks and looks nothing like a Digital
    # Marketing Report - no line item grid, no creative previews, none of the
    # widgets the rules are written about. Run through them it fails on nearly
    # every one, and a screen full of red that is all wrong is worse than no
    # screen at all.
    #
    # So it is stored, named and packaged like anything else, with the checks
    # not run rather than run and disbelieved. Ticked automatically when the
    # row is SEO-only, so nobody has to remember.
    # THE ROW SAYS SO NOW, rather than the client's whole product list.
    #
    # SEO has its own row on the board, so a client running SEO and Social
    # Mirror has two, and "is every product this client runs SEO" was the wrong
    # question - it answered no for exactly the mixed client whose SEO upload
    # most needs the checks skipped.
    # An SEO-only client's row IS the SEO row, so an upload against it belongs
    # there whichever kind the form happened to post.
    is_seo_report = kind == "seo" or _is_seo_row(db, client, account_ids, period)
    no_checks = (skip_checks == "1") or is_seo_report

    # If one already exists for this client and cycle, this is a replacement
    # and should go through the route that knows how to handle one.
    from .ingest import _rkey
    for r in db.scalars(select(Report).where(Report.period == period)).all():
        if bool(r.is_lifetime) != is_lifetime:
            continue
        # A client's SEO report and their digital one are two files, not two
        # copies of one. Matching across them made the second upload look like
        # a replacement for the first.
        if bool(getattr(r, "is_seo", False)) != is_seo_report:
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
                                   if is_lifetime and period else None),
                           period=period)
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
    from .recheck import sibling_for, sibling_of
    logo = header_logo_hash(path)
    logo_bad = is_generic(db, logo)
    logo_seen = bool(db.scalar(select(func.count()).select_from(KnownLogo)))
    if no_checks:
        # Read for its page count and nothing else. A report nobody is judging
        # still has to say how long it is and go into the folder under a
        # sensible name.
        from .checks.parser import quick_meta
        result = quick_meta(path, file.filename or safe)
    else:
      try:
        result = run_all(path, filename=file.filename,
                         # The row it was uploaded against.
                         for_client=client, expected_products=exp,
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
                     orders_current=orders_ok,
                     # The other half of the pair, if this client is getting
                     # both. Looked up from what is known - the row this is
                     # about does not exist yet.
                     sibling=sibling_for(db, client, period, market,
                                         is_lifetime))
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
        checks_skipped=no_checks, is_seo=is_seo_report,
        rules_version=_rv())
    db.add(rep)
    db.flush()
    attach_owners(db, rep)
    # NAMED HERE TOO, NOT ONLY ON THE FEED.
    #
    # The feed renames every report it takes in and the replace route renames
    # what it is handed, but a file uploaded by hand kept whatever it was
    # called - and TapClicks calls every file you download by hand "Digital
    # Marketing Report.pdf". Two of those landed in a partner's Dropbox folder
    # as "Digital Marketing Report.pdf" and "Digital Marketing Report -
    # Lifetime.pdf", which is not a name anybody can file by.
    _rename(rep, file.filename or "", db)
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
                                   if rep.is_lifetime and rep.period else None),
                           period=rep.period)
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
    from .recheck import sibling_for, sibling_of
    logo = header_logo_hash(target)
    logo_bad = is_generic(db, logo)
    logo_seen = bool(db.scalar(select(func.count()).select_from(KnownLogo)))
    result = run_all(target, filename=rep.filename,
                     for_client=rep.client, expected_products=exp,
                     flight=flight,
                     flight_lines=flight_lines(db, rep.client, rep.account_ids),
                     is_lifetime=bool(rep.is_lifetime),
                     period=rep.period, market=rep.market or "",
                     expected_why=why, expected_any=any_of,
                     quiet_products=quiet,
                     logo_hash=logo, logo_generic=logo_bad,
                     logo_known=logo_seen, budgets=budgets, ordered=ordered,
                     orders_current=orders_ok,
                     sibling=sibling_of(db, rep))
    _old_findings = list(rep.findings or [])
    _old_acked = list(rep.acked or [])
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
    # A DIFFERENT FILE NEEDS A NEW SIGN-OFF. IT DOES NOT NEED THE SAME SIX
    # BOXES TICKED AGAIN.
    #
    # A sign-off is about a particular copy. An acceptance is about a FINDING -
    # "that CTR flag is a known quirk of this client's GA page" - and it is as
    # true of the new file as it was of the old one. Carried by what the
    # finding says, so anything the new file fixed drops its tick and anything
    # that moved down the list keeps it.
    from .recheck import remap_acks
    rep.acked = remap_acks(_old_findings, _old_acked, rep.findings)
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
    _rename(rep, file.filename or "", db)
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
                                   if rep.is_lifetime and rep.period else None),
                           period=rep.period)
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
    from .recheck import sibling_for, sibling_of
    logo = header_logo_hash(path)
    logo_bad = is_generic(db, logo)
    logo_seen = bool(db.scalar(select(func.count()).select_from(KnownLogo)))
    try:
        result = run_all(path, filename=rep.filename,
                         for_client=rep.client, expected_products=exp,
                         flight=flight,
                         flight_lines=flight_lines(db, rep.client, rep.account_ids),
                         is_lifetime=bool(rep.is_lifetime),
                         period=rep.period, market=rep.market or "",
                     expected_why=why, expected_any=any_of,
                     quiet_products=quiet,
                     logo_hash=logo, logo_generic=logo_bad,
                     logo_known=logo_seen, budgets=budgets, ordered=ordered,
                     orders_current=orders_ok,
                     sibling=sibling_of(db, rep))
    except Exception as exc:  # noqa: BLE001
        rep.severity = "fail"
        rep.findings = [{"code": "unreadable", "severity": "fail",
                         "title": "The replacement could not be read",
                         "detail": str(exc)}]
        rep.checks = []
        db.commit()
        return RedirectResponse(f"/report/{report_id}/view", status_code=303)

    _old_findings = list(rep.findings or [])
    _old_acked = list(rep.acked or [])
    rep.stored_path = str(path)
    rep.pages = result["pages"]
    rep.impressions = result["impressions"]
    rep.clicks = result["clicks"]
    rep.products = ", ".join(result.get("products") or [])
    rep.severity = result["severity"]
    rep.findings = result["findings"]
    rep.checks = result.get("checks") or []
    # A replacement is a new file, so it needs a new sign-off. The ticks are
    # kept: an acceptance is about a finding, not about a copy, and re-ticking
    # the same known false alarms on every corrected pull is how somebody
    # learns to stop reading them. The note is kept for the same reason - it is
    # about the client.
    from .recheck import remap_acks
    rep.acked = remap_acks(_old_findings, _old_acked, rep.findings)
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
    #
    # AND WHEN IT SAYS NOTHING, IT SAYS WHY. The panel simply vanished when
    # there was nothing to compare against, which is indistinguishable from
    # the panel being broken - "where did pacing go" is not a question a page
    # should leave you holding. There are four ways to have nothing to pace and
    # they need four different things done about them.
    pacing, pacing_why = [], ""
    try:
        if not rep.stored_path or not Path(rep.stored_path).exists():
            pacing_why = "the PDF is not on disk"
        else:
            from .checks.parser import pdf_text
            from .checks.served import pacing_rows
            from .cycle import cycle_for as _cyc
            # client_flight LIVES IN ingest, NOT IN roster.
            #
            # It was imported from roster, which does not have it, so every
            # single report raised ImportError here and the bare except below
            # turned that into an empty panel. Pacing did not go quiet on some
            # reports - it had been dead on all of them since build 82, and
            # nothing said so because the failure looked exactly like having
            # nothing to say. That is what the reason line underneath is for.
            from .ingest import client_flight
            from .roster import _ran_during, client_lines, ordered_for
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
                        if rep.is_lifetime and rep.period else None),
                period=rep.period)
            ordered = ordered_for(db, rep.client, rep.account_ids, rep.period,
                                  lifetime=bool(rep.is_lifetime),
                                  window=life_flight)
            if ordered:
                pacing = pacing_rows(pdf_text(Path(rep.stored_path)), ordered)
                if not pacing:
                    pacing_why = ("nothing this client bought is paced on a "
                                  "number - " + ", ".join(sorted(ordered)) +
                                  " " + ("is" if len(ordered) == 1 else "are") +
                                  " sold flat, not against impressions or spend")
            else:
                lines = client_lines(db, rep.client, rep.account_ids) or []
                ran = [l for l in lines
                       if not rep.period or _ran_during(l, rep.period)]
                if not lines:
                    pacing_why = ("no order line matches this client - the "
                                  "orders may not be loaded, or the report's "
                                  "order ids do not match any of them")
                elif not ran:
                    pacing_why = (f"none of this client's {len(lines)} order "
                                  f"lines ran in {rep.period}")
                elif not any(getattr(l, "live", True) for l in ran):
                    pacing_why = ("every line item on this client's orders is "
                                  "paused or cancelled")
                else:
                    pacing_why = ("the orders carry no monthly budget and no "
                                  "impression goal for this client")
    except Exception as exc:                # a pacing panel is never worth a 500
        import logging
        logging.getLogger("report-qa").exception(
            "pacing panel failed for report %s", rep.id)
        pacing = []
        pacing_why = f"the pacing panel could not be built ({type(exc).__name__})"
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
                                       "pacing_why": pacing_why,
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
    # WHETHER THE SHEET IS STILL BEING READ. A sync that quietly stopped looks
    # exactly like a roster nobody has changed.
    from .roster_sheet import configured as _sheet_on, last_read, sheet_id
    return templates.TemplateResponse(request, "partners.html", {
        "partners": rows, "nav": "partners",
        "sheet_on": _sheet_on(), "sheet_log": last_read(db),
        "sync_every": settings.sync_every_minutes,
        "sheet_url": (f"https://docs.google.com/spreadsheets/d/{sheet_id()}/edit"
                      if _sheet_on() else ""),
        "tally": sorted(tally.items()), "just_set": just_set})


@app.post("/partners/sheet")
def partners_sheet_sync(db: Session = Depends(get_db)):
    """Read the breakout sheet now, rather than on the next order sync."""
    from .roster_sheet import sync_roster
    sync_roster(db, force=True)
    return RedirectResponse("/partners", status_code=303)


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


def pull_range_rows(db: Session, today: dt.date | None = None) -> list[tuple]:
    """(partner, earliest date the pull has to reach, line items), oldest first.

    THE ORDER'S START, NOT THE LINE ITEM'S - AND NOT "IS IT LIVE".

    This asked for the earliest start among line items marked live, and both
    halves of that were wrong.

    ReThink Media Group is the case. Order 4701, Memorial Hospital, IO Live,
    dated 2018-11-01 to 2026-12-31. Its four oldest line items are IO Complete
    and ended between 2021 and 2023; the three live ones start in 2021 and
    2024. So the old rule answered 2021-08-11 and the page said one pull would
    do - while everything that order did in its first three years fell outside
    the range and was never loaded.

    That matters because a lifetime covers the CAMPAIGN. When 4701 finally ends
    its report has to reach back to 2018, and the check that says "this
    lifetime does not go back to the campaign start" would have been comparing
    against a start date three years too late.

    So the question is: which orders are still being reported on, and how far
    back do THEY go. An order still open, or that ended recently enough to
    still owe a report, needs its whole history in the pull - line items that
    finished years ago included, because they are part of what the lifetime
    covers.
    """
    today = today or dt.date.today()
    # Two months back, to the first. This month's cycle reports on last month,
    # and a lifetime for a campaign that ended then covers all of it.
    y, m = today.year, today.month - 2
    while m < 1:
        m += 12
        y -= 1
    keep_from = dt.date(y, m, 1)

    # WHEN THE ORDER FINISHES, falling back to the line's own end where the
    # export did not carry the order's. One OR per column let a line with no
    # end date of its own through on an order that closed in 2012.
    # A CANCELLED ORDER'S END DATE IS NOT WHEN IT STOPPED.
    #
    # Nothing on the export says when somebody hit cancel, so a campaign called
    # off in 2021 can still be dated to 2027 - and "ends after the cutoff" kept
    # it on this list forever. Whitefield Media has no live order at all and
    # was asking for a pull back to 23 March 2020 on the strength of one.
    #
    # So a row nobody is running any more is judged on the LINE ITEM's own end,
    # which is the last date anything was actually bought to deliver. A live
    # row is judged on the order's end as before, because an open-ended one
    # genuinely has no end yet.
    from sqlalchemy import and_ as _and, case, or_ as _or
    order_end = func.coalesce(OrderLine.order_ends_on, OrderLine.ends_on)
    running = _and(OrderLine.canceled.is_(False), OrderLine.complete.is_(False))
    keep = _or(
        _and(running, _or(order_end.is_(None), order_end >= keep_from)),
        _and(_or(running == False, running.is_(None)),   # noqa: E712
             OrderLine.ends_on.isnot(None), OrderLine.ends_on >= keep_from),
    )
    # HOW FAR BACK THIS ORDER REACHES, when its two date pairs disagree.
    #
    # The order header is the right thing to ask - a lifetime covers the whole
    # order, and line items that finished years ago are dropped at import, so
    # the header is the only surviving trace of how far back the order goes.
    # The trouble is that this export's headers are not always describing their
    # own order:
    #
    #   4701   header 2018-11-01 -> 2026-12-31, line 2018-11-01 -> 2021-07-31
    #          agrees, and the header is what reaches the old complete lines
    #   36184  header 2024-02-07 -> 2026-12-31, line 2018-01-01 -> 2023-04-20
    #          header starts SIX YEARS AFTER its own line item
    #   55987  header 2018-03-21 -> 2018-05-18, line 2026-06-29 -> 2026-07-31
    #          header does not overlap its line item at all, and that 2018 is
    #          what put Manning Media on this list asking for a six-year pull
    #
    # 55987's line item is the one the IO tool shows, so the header is simply
    # wrong there. What separates it from 4701 is that its header ENDS before
    # its own line item does - a header that cannot contain its line item is
    # not describing it. So the header is used only when it could contain the
    # line, and then only if it reaches further back.
    line_start = func.coalesce(OrderLine.starts_on, OrderLine.order_starts_on)
    head_start = func.coalesce(OrderLine.order_starts_on, OrderLine.starts_on)
    head_ok = _or(OrderLine.order_ends_on.is_(None), OrderLine.ends_on.is_(None),
                  OrderLine.order_ends_on >= OrderLine.ends_on)
    reach = case((_and(head_ok, head_start < line_start), head_start),
                 else_=line_start)
    rows = db.execute(
        select(OrderLine.market, func.min(reach), func.count(OrderLine.id))
        .where(keep)
        .group_by(OrderLine.market)).all()
    return sorted(((m_, e, n) for m_, e, n in rows),
                  key=lambda r: (r[1] or dt.date.max, r[0] or ""))


def _strategy_with_reasons(db: Session) -> dict:
    """The pull plan, with the line items behind each straggler's date.

    "Manning Media, 2018-03-21" and "Whitfield Media, 2020-03-23" are claims
    about particular orders, and the only way to judge one is to see it. There
    are never more than a handful of stragglers, so the reasons come with them
    rather than being a download somebody has to know exists.
    """
    st = pull_strategy(db)
    for s in st.get("stragglers", []):
        rows = pull_range_why(db, s.get("market") or "")
        s["why"] = [r for r in rows if r["kept"]]
        s["dropped"] = sum(1 for r in rows if not r["kept"])
    return st


def pull_range_why(db: Session, market: str, today: dt.date | None = None) -> list:
    """The line items that set a partner's pull date, oldest first.

    "Whitefield Media, 23 March 2020" is a claim about an order somebody has to
    be able to find. This is that order - and the way to audit the whole list
    without asking me which ones are wrong.
    """
    today = today or dt.date.today()
    y, m = today.year, today.month - 2
    while m < 1:
        m += 12
        y -= 1
    keep_from = dt.date(y, m, 1)
    out = []
    for l in db.scalars(select(OrderLine).where(OrderLine.market == market)).all():
        oend = l.order_ends_on or l.ends_on
        running = not (getattr(l, "canceled", False) or getattr(l, "complete", False))
        if running:
            kept = oend is None or oend >= keep_from
            why = "still running" if kept else "the order ended before the window"
        else:
            kept = bool(l.ends_on and l.ends_on >= keep_from)
            why = ("finished recently enough to still owe a report" if kept
                   else "nothing on it has been running since "
                        + keep_from.isoformat())
        # BOTH WINDOWS, because they disagree and the disagreement is the story.
        # A header that ENDS before its own line item cannot be describing it,
        # and that is the test the pull date uses - so the panel marks exactly
        # the rows where the header was set aside.
        head = (l.order_starts_on, l.order_ends_on)
        odd = bool(head[1] and l.ends_on and head[1] < l.ends_on)
        reach = l.starts_on or l.order_starts_on
        if not odd and head[0] and reach and head[0] < reach:
            reach = head[0]           # the header reaches further and is credible
        out.append({"orders": l.account_ids or "", "lines": l.line_ids or "",
                    "product": l.product or "", "client": l.client or "",
                    "starts": reach,
                    "ends": l.ends_on or l.order_ends_on, "kept": kept,
                    "order_starts": head[0], "order_ends": head[1], "odd": odd,
                    "status": getattr(l, "status", "") or "", "why": why})
    out.sort(key=lambda r: (not r["kept"], r["starts"] or dt.date.max))
    return out


class _OneFlight:
    """One line item's own window, shaped like an order line.

    So the "did it run in this month" test is the same code for a single line
    item as for the merged row - a second copy of that rule is a second answer
    waiting to disagree with the board.
    """

    def __init__(self, starts, ends):
        self.flights = [[starts, ends]]
        self.starts_on, self.ends_on = starts, ends


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
        product = l.product or "(unmapped)"

        def stale(raw: str) -> str:
            """What today's code would map this raw name to, IF IT DISAGREES.

            One line item can sell two products - "CTV + Video Ads" is one buy
            and two rows - so a raw name mapping to more than one product is
            normal and says nothing. Comparing the stored product against the
            whole list flagged every single one of those rows: order 49813 had
            "reads as CTV, Video today" pinned to a row correctly stored as
            CTV, which is a warning about nothing on the row it is warning
            about. The question is whether the stored product is still ONE OF
            the answers, not whether it is the only one.
            """
            now = map_order_products(raw or "")
            if product in now:
                return ""
            return ", ".join(now) or "(nothing)"

        base = {
            "product": product, "raw": l.campaign or "",
            # WHAT THE MONTH WAS BOUGHT TO DO. The panel exists to answer
            # "what is this finding actually judging", and the goal is half of
            # every pacing answer - it was the one column not on the table.
            "spend": getattr(l, "spend", None),
        }
        # ONE ROW PER ORDER AND LINE ITEM.
        #
        # The stored row is one answer per client and product, merged across
        # every order that sells it, because that is the question the checks
        # ask. Read by a person it is a lie of omission: Long Jewelers' Social
        # Mirror showed orders 48135 and 53342 above one span running April to
        # December, when 48135 finished on 28 February. Merged dates cannot say
        # which order they came from, so here the line items are unpacked.
        for d in (getattr(l, "detail", None) or []):
            raw = d.get("raw") or base["raw"]
            rows.append({
                **base, "merged": False,
                "order": d.get("order") or "", "line": d.get("line") or "",
                # JUDGED ON THIS LINE ITEM'S OWN RAW NAME, not the merged
                # row's. The merged row keeps whichever name was read first,
                # so a line item selling "Connected TV Ads" was being told it
                # reads as something else today because a DIFFERENT line item
                # on the same order says "CTV + Video Ads".
                "raw": raw, "would_be": stale(raw),
                "starts": d.get("starts"), "ends": d.get("ends"),
                "order_starts": d.get("order_starts"),
                "order_ends": d.get("order_ends"),
                "status": d.get("status") or "",
                "live": bool(d.get("live")), "canceled": bool(d.get("canceled")),
                "complete": bool(d.get("complete")),
                "paused": bool(d.get("paused")),
                "budget": d.get("budget"), "impressions": d.get("impressions"),
                "total_budget": d.get("total_budget"),
                "total_impressions": d.get("total_impressions"),
                "ran": (_ran_during(_OneFlight(d.get("starts"), d.get("ends")),
                                    rep.period) if rep.period else None),
            })
        if not (getattr(l, "detail", None) or []):
            # No line items kept - a row loaded before they were. Show the
            # merged answer rather than nothing, and say which it is.
            rows.append({
                **base, "merged": True, "would_be": stale(l.campaign or ""),
                "order": l.account_ids or "", "line": l.line_ids or "",
                "starts": l.starts_on, "ends": l.ends_on,
                "order_starts": getattr(l, "order_starts_on", None),
                "order_ends": getattr(l, "order_ends_on", None),
                "status": getattr(l, "status", "") or "",
                "live": bool(getattr(l, "live", True)),
                "canceled": bool(getattr(l, "canceled", False)),
                "complete": bool(getattr(l, "complete", False)),
                "paused": bool(getattr(l, "paused", False)),
                "budget": l.budget, "impressions": getattr(l, "impressions", None),
                "total_budget": getattr(l, "total_budget", None),
                "total_impressions": getattr(l, "total_impressions", None),
                "ran": _ran_during(l, rep.period) if rep.period else None,
            })
    rows.sort(key=lambda r: (r["product"], r.get("starts") or "",
                             str(r.get("order") or "")))
    # Only the first row of each product group prints the product, so the eye
    # reads the group and not the same word eight times.
    seen_product = ""
    for r in rows:
        r["first"] = r["product"] != seen_product
        seen_product = r["product"]

    # ORDERS THAT DID NOT RUN IN THIS MONTH ARE NOT WHAT THIS REPORT IS ABOUT.
    #
    # 48135 ended on 28 February and was sitting on a July report's order list
    # looking like evidence. They are still worth being able to see - it is the
    # same client and the same product - so they move below the fold instead of
    # disappearing. A lifetime covers every month, so it keeps all of them.
    def regroup(items):
        seen = ""
        for r in items:
            r["first"] = r["product"] != seen
            seen = r["product"]
        return items

    other: list = []
    if rep.period and not getattr(rep, "is_lifetime", False):
        other = regroup([r for r in rows if r["ran"] is False])
        rows = regroup([r for r in rows if r["ran"] is not False])
    # A CANCELLED LINE IS NOT PART OF WHAT THIS REPORT IS JUDGED AGAINST.
    #
    # It is not owed on the report and it is out of every goal the pacing panel
    # asks about, so a red pill in the middle of the list reads as the finding
    # - when it is the one row nothing is being decided from. Paragon Casino
    # Resort's lifetime is two Social Mirror line items, one of them cancelled,
    # and the cancelled one is the loudest thing on the table.
    #
    # Below the fold rather than gone, with its money still on it: which half
    # was called off is exactly what somebody opens this panel to find out.
    dead = regroup([r for r in rows if r["canceled"]])
    rows = regroup([r for r in rows if not r["canceled"]])
    from .orders_s3 import NOT_A_SYNC, running_sync
    sync = db.scalars(select(OrderSync)
                      .where(OrderSync.state != "running",
                             ~OrderSync.source.like(NOT_A_SYNC))
                      .order_by(desc(OrderSync.id)).limit(1)).first()
    ctx = {
        "nav": "cycle", "rep": rep, "rows": rows, "other": other,
        "dead": dead, "sync": sync,
        "io_order_url": settings.io_order_url,
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


@app.get("/orders/{oid}/lines", response_class=HTMLResponse)
def order_lines(oid: str, request: Request, db: Session = Depends(get_db)):
    """Every line item stored under one order id, as the last import read it.

    WRITTEN BECAUSE A DATE IN A REASON HAD NO ADDRESS. "Nothing on order 55048
    starts until 2026-09-12" is a true statement about a row nobody could look
    at, with the IO tool open on screen saying 25 August - and the only way to
    find out which of the two was stale was to guess from a screenshot. The
    order number on the list check opens this beside it now.
    """
    from .version import product_map_version

    oid = re.sub(r"[^0-9]", "", oid or "")[:12]
    if not oid:
        raise HTTPException(404)
    rows = []
    for l in db.scalars(select(OrderLine)).all():
        ids = (l.account_ids or "").replace(",", " ").split()
        if oid not in ids:
            continue
        base = {"client": l.client or "", "product": l.product or "(unmapped)"}
        detail = [d for d in (getattr(l, "detail", None) or [])
                  if isinstance(d, dict)
                  and str(d.get("order") or "").strip() == oid]
        if detail:
            for d in detail:
                rows.append({**base, "line": d.get("line") or "",
                             "raw": d.get("raw") or l.campaign or "",
                             "starts": d.get("starts"), "ends": d.get("ends"),
                             "order_starts": d.get("order_starts"),
                             "order_ends": d.get("order_ends"),
                             "status": d.get("status") or "",
                             "live": bool(d.get("live")),
                             "canceled": bool(d.get("canceled")),
                             "complete": bool(d.get("complete")),
                             "paused": bool(d.get("paused")),
                             "budget": d.get("budget"),
                             "impressions": d.get("impressions")})
        else:
            # A row written before the line items were kept. Its merged span is
            # all there is, and saying so beats showing nothing.
            rows.append({**base, "line": l.line_ids or "",
                         "raw": l.campaign or "",
                         "starts": l.starts_on, "ends": l.ends_on,
                         "order_starts": getattr(l, "order_starts_on", None),
                         "order_ends": getattr(l, "order_ends_on", None),
                         "status": getattr(l, "status", "") or "",
                         "live": bool(getattr(l, "live", True)),
                         "canceled": bool(getattr(l, "canceled", False)),
                         "complete": bool(getattr(l, "complete", False)),
                         "paused": bool(getattr(l, "paused", False)),
                         "budget": l.budget,
                         "impressions": getattr(l, "impressions", None)})
    rows.sort(key=lambda r: (str(r.get("starts") or ""), r["product"]))

    from .orders_s3 import NOT_A_SYNC
    sync = db.scalars(select(OrderSync)
                      .where(OrderSync.state != "running",
                             ~OrderSync.source.like(NOT_A_SYNC))
                      .order_by(desc(OrderSync.id)).limit(1)).first()
    # AND WHY IT IS NOT HERE, when it is not. An empty table reads as an empty
    # feed, and the import already wrote down what it did with the rows.
    gone = ""
    if not rows:
        why = (getattr(sync, "dropped_orders", None) or {}).get(oid)
        gone = why if isinstance(why, str) else ", and ".join(why or ())
    return templates.TemplateResponse(request, "order_lines.html", {
        "nav": "orders", "oid": oid, "rows": rows, "sync": sync,
        "dropped": gone,
        "io_line_url": settings.io_line_url,
        "stale": bool(sync and sync.ok and
                      (sync.map_version or "") != product_map_version())})


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


@app.get("/orders/pull-range-why.csv")
def pull_range_why_csv(db: Session = Depends(get_db)):
    """Every partner's pull date, with the line items that set it.

    "Whitefield Media, 23 March 2020" is a claim about an order somebody has to
    be able to find, and one wrong row is worth knowing about because it means
    a pull reaching back six years for nothing. This is the whole list with its
    reasons, so it can be audited without anyone guessing which ones are wrong.
    """
    import csv as _csv
    import io as _io

    buf = _io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["Partner", "Pull from", "Counted", "Client", "Product",
                "Orders", "Line items", "Status", "Starts", "Ends", "Why"])
    for market, earliest, _n in pull_range_rows(db):
        for r in pull_range_why(db, market or ""):
            w.writerow([market, earliest.isoformat() if earliest else "",
                        "yes" if r["kept"] else "no", r["client"], r["product"],
                        r["orders"], r["lines"], r["status"],
                        r["starts"].isoformat() if r["starts"] else "",
                        r["ends"].isoformat() if r["ends"] else "", r["why"]])
    return Response(content=buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition":
                             'attachment; filename="pull-range-why.csv"'})


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


@app.post("/orders/serving")
async def serving_import(request: Request, file: UploadFile = File(...),
                         period: str = Form(""),
                         db: Session = Depends(get_db)):
    """Load the serving file - what actually delivered, by client, by day.

    THE ONE FACT THE ORDER EXPORT CANNOT GIVE. It carries a flight and a
    status, so a line paused on the 2nd reads exactly like one paused on the
    30th, and the board has been guessing off two dates ever since.
    """
    from .roster import _rows_from_csv, _rows_from_xlsx
    from .serving import import_serving

    name = file.filename or "serving.csv"
    raw = await file.read()
    try:
        rows = (_rows_from_xlsx(raw, None) if name.lower().endswith((".xlsx", ".xlsm"))
                else _rows_from_csv(raw))
        res = import_serving(db, rows, period=period or None)
    except Exception as exc:  # noqa: BLE001 - the message IS the answer here
        db.rollback()
        db.add(OrderSync(source=f"serving upload: {name}", rows=0, ok=False,
                         message=f"Could not read the serving file: {exc}"))
        db.commit()
        return RedirectResponse("/orders?serving=failed", status_code=303)
    msg = (f"Serving file: {res['clients']:,} clients across "
           f"{', '.join(res['periods']) or 'no month'}, "
           f"{res['rows_read']:,} rows read. A day counts when there is "
           f"delivery on it ({res['counted_on']}). Columns used: "
           + ", ".join(f"{f} = {h}" for f, h in res["columns"].items()))
    db.add(OrderSync(source=f"serving upload: {name}", rows=res["clients"],
                     ok=True, message=msg + "."))
    db.commit()
    return RedirectResponse(f"/orders?serving={res['clients']}", status_code=303)


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
