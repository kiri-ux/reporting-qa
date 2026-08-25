"""Re-run the checks on reports that were judged by older code.

Findings are written once, when a report arrives, and then stored. That is the
right design - a report page has to load instantly and a 300-page PDF takes
seventeen seconds to read - but it means a deploy that fixes a rule does not
fix the reports that rule already got wrong. They keep showing yesterday's
answer until something re-reads the PDF.

So each report records the fingerprint of the code that judged it, and this
module re-checks anything stamped with an older one, a few at a time, in the
background. Two things it is careful about:

  * An acceptance is attached to a FINDING, not to a position in a list. Acks
    were stored as indexes, and when the findings change the indexes shift -
    so an automatic sweep would silently move somebody's tick from "CTV
    excluded from the CTR base" onto a real failure. They are re-mapped by
    what was accepted.
  * A sign-off is a person saying "I looked at this answer". It survives a
    re-check that changes nothing they would have cared about, and is reset
    when a failure appears that was not there when they signed.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import settings
from .db import KnownLogo, Report, SessionLocal
from .version import rules_version

log = logging.getLogger("report-qa.recheck")

# A report re-reads in about half a second - 0.3s for a seven-page one, 1.1s
# for nineteen pages. The old pacing rested twenty seconds after four seconds
# of work, which turned a ten-minute job into two hours and meant a queue that
# never drained while builds were going out several times a day.
#
# So the rest is proportional to the work instead: never longer than the batch
# took, capped at ten seconds. That is still under a 50% duty cycle on a box
# that is also serving pages, and it drains twelve hundred reports in under
# twenty minutes.
BATCH = 25
MAX_REST_SECONDS = 10.0


def _key(f: dict) -> tuple:
    """What identifies a finding across a re-check.

    The code alone is too coarse - a report can carry four "Row CTR does not
    match" findings and accepting one must not accept the others. The title
    carries the specific row, so the pair is stable enough to re-map by and
    specific enough not to leak an acceptance onto something else.
    """
    return ((f.get("code") or ""), (f.get("title") or ""))


def remap_acks(old_findings: list, acked: list, new_findings: list) -> list[int]:
    """Indexes into the NEW findings for everything that was accepted before.

    A finding that no longer exists drops off, which is correct: the acceptance
    was about that finding, and it is gone.
    """
    old_findings = old_findings or []
    accepted = {_key(f) for i, f in enumerate(old_findings) if i in (acked or [])}
    if not accepted:
        return []
    out, used = [], set()
    for i, f in enumerate(new_findings or []):
        k = _key(f)
        if k in accepted and k not in used:
            used.add(k)
            out.append(i)
    return out


def _new_failures(old_findings: list, acked: list, new_findings: list) -> list[str]:
    """Failures on the new answer that were not on the old one, ignoring any
    the person had already accepted."""
    old = {_key(f) for f in (old_findings or [])}
    fresh = []
    for f in new_findings or []:
        if f.get("severity") != "fail":
            continue
        if _key(f) not in old:
            fresh.append(f.get("title") or f.get("code") or "")
    return fresh


def _orders_current(db: Session) -> bool:
    """Were the loaded orders produced by the import code running now?

    While they were not, the product check has nothing current to compare
    against and abstains. It was answering from the old data instead, and
    saying the same wrong thing about the same report over and over.
    """
    from .db import OrderSync
    from .version import product_map_version
    row = db.scalars(select(OrderSync).where(OrderSync.state != "running")
                     .order_by(OrderSync.id.desc()).limit(1)).first()
    if row is None or not row.ok:
        return True                    # nothing loaded: a different problem
    return (row.map_version or "") == product_map_version()


def recheck(db: Session, rep: Report, *, manual: bool = False) -> dict:
    """Re-read this report's PDF with today's rules. Returns what changed."""
    from .checks.rules import run_all
    from .ingest import client_flight
    from .roster import (budgets_for, expected_any, expected_products,
                     expected_why, quiet_products)

    path = Path(rep.stored_path or "")
    if not path.exists():
        # Stamp it anyway. Without the stamp the sweeper picks it up forever,
        # and there is nothing on disk that a later pass could do better with.
        rep.rules_version = rules_version()
        db.commit()
        return {"ok": False, "reason": "the stored PDF is gone"}

    exp = expected_products(db, rep.client, rep.account_ids, period=rep.period)
    why = expected_why(db, rep.client, rep.account_ids, period=rep.period)
    any_of = expected_any(db, rep.client, rep.account_ids, period=rep.period)
    quiet = quiet_products(db, rep.client, rep.account_ids, period=rep.period)
    budgets = budgets_for(db, rep.client, rep.account_ids, period=rep.period)
    orders_ok = _orders_current(db)
    # The corner of page one, and which other markets print the same mark.
    # Computed here rather than inside the checks because it takes a database
    # question, and a check is handed facts rather than going looking.
    from .checks.logo import header_logo_hash, is_generic
    # THE FINGERPRINT IS TAKEN ONCE, WHEN THE FILE ARRIVES.
    #
    # It shells out to pdftoppm - a fifth of a second of CPU - and re-taking it
    # on every re-check put that on top of every report in an 838-deep queue,
    # on a box that is also meant to be answering a five-second health check.
    # Render started mailing about failed health checks, and this was why.
    #
    # The file cannot have changed under a re-check: a replacement goes through
    # its own path and re-fingerprints there. So an existing hash is reused,
    # and only a report that has never had one pays for it.
    logo = rep.logo_hash or header_logo_hash(path)
    logo_bad = is_generic(db, logo)
    logo_seen = bool(db.scalar(select(func.count()).select_from(KnownLogo)))
    flight = client_flight(db, rep.client, rep.account_ids)
    result = run_all(path, filename=rep.filename, expected_products=exp,
                     flight=flight, period=rep.period, market=rep.market or "",
                     expected_why=why, expected_any=any_of,
                     quiet_products=quiet,
                     logo_hash=logo, logo_generic=logo_bad,
                     logo_known=logo_seen, budgets=budgets,
                     orders_current=orders_ok)

    was_sev = rep.severity
    old_findings, old_acked = list(rep.findings or []), list(rep.acked or [])

    rep.findings = result["findings"]
    rep.checks = result.get("checks") or []
    rep.severity = result["severity"]
    rep.products = ", ".join(result.get("products") or [])
    rep.acked = remap_acks(old_findings, old_acked, rep.findings)
    rep.rules_version = rules_version()
    rep.logo_hash = logo

    fresh = _new_failures(old_findings, old_acked, rep.findings)
    reset = False
    if fresh and rep.review_state in ("reviewed", "waived"):
        # They signed off on a different answer. Saying so is the whole point;
        # leaving the sign-off would ship a report nobody has actually read.
        rep.review_state = "new"
        rep.reviewed_at = None
        # The name stays - somebody has to be told whose sign-off went - but it
        # is marked as pulled, so the row stops showing an initial beside a
        # report that is back to unreviewed.
        rep.signoff_cleared_at = dt.datetime.utcnow()
        reset = True
    db.commit()
    return {"ok": True, "was": was_sev, "now": rep.severity,
            "new_failures": fresh, "signoff_reset": reset,
            "acks_kept": len(rep.acked), "acks_before": len(old_acked)}


# ------------------------------------------------------------------ the sweep
_running = threading.Event()


def recent_periods(n: int) -> list[str]:
    """The cycles the automatic sweep covers.

    Old months are re-checked on demand, not on every deploy. A finding on a
    cycle that shipped in March is not in anybody's way, and re-reading four
    years of PDFs every time a rule changes is work nobody asked for.
    """
    from .cycle import recent_periods as _recent
    out = _recent(max(n, 1))
    if settings.default_period and settings.default_period not in out:
        out.append(settings.default_period)
    return out


def _stale_query(db: Session, periods: list[str] | None, group: str | None,
                 period: str | None, stale_only: bool = True,
                 skip_signed: bool = False, signed_only: bool = False):
    q = select(Report)
    if stale_only:
        q = q.where(Report.rules_version != rules_version())
    if skip_signed:
        # A REPORT SOMEBODY HAS SIGNED OFF IS NOT WHAT THE BUTTON IS FOR.
        #
        # Pressing Re-check on a partner with one report still pending said
        # "6 of 8" and worked through six that were already done. The button
        # means "bring this partner up to date", and a signed-off report is up
        # to date by definition - somebody read it and said so.
        #
        # The background sweep still covers them, which is how a rule change
        # reaches a signed-off report and pulls the sign-off if it finds a new
        # failure. This only narrows the button.
        q = q.where(Report.review_state.notin_(("reviewed", "waived")))
    if signed_only:
        # The deliberate pass before delivery: only the finished work, and only
        # where the code that judged it has since changed.
        q = q.where(Report.review_state.in_(("reviewed", "waived")))
    if period:
        q = q.where(Report.period == period)
    elif periods:
        q = q.where(Report.period.in_(periods))
    if group:
        from .board import market_names_for_group
        markets = market_names_for_group(db, group)
        q = q.where(Report.market.in_(markets or [group]))
    return q


def stale_count(db: Session, *, scoped: bool = False, group: str | None = None,
                period: str | None = None, stale_only: bool = True,
                skip_signed: bool = False, signed_only: bool = False) -> int:
    from sqlalchemy import func
    periods = recent_periods(settings.recheck_periods) if scoped else None
    q = _stale_query(db, periods, group, period, stale_only, skip_signed,
                     signed_only)
    return db.scalar(select(func.count()).select_from(q.subquery())) or 0


def _stale_batch(db: Session, limit: int, *, scoped: bool = True,
                 group: str | None = None, period: str | None = None,
                 stale_only: bool = True, after: int = 0,
                 skip_signed: bool = False,
                 signed_only: bool = False) -> list[Report]:
    """Newest cycles first. The month somebody is working on is the one where a
    stale answer is actually in the way."""
    periods = recent_periods(settings.recheck_periods) if scoped else None
    q = _stale_query(db, periods, group, period, stale_only, skip_signed,
                     signed_only)
    if after:
        q = q.where(Report.id > after)
    # Stale-only runs shrink their own queue, so newest-first is right. A run
    # over everything does not, so it walks the ids upward instead.
    order = (Report.id.asc(),) if not stale_only else (Report.period.desc(),
                                                       Report.id.desc())
    return list(db.scalars(q.order_by(*order).limit(limit)).all())


def sweep_once(db: Session, limit: int = BATCH, *, scoped: bool = True,
               group: str | None = None, period: str | None = None) -> int:
    """One batch of the automatic sweep.

    IT LEAVES SIGNED-OFF REPORTS ALONE. That is a change of mind and worth
    saying why. The sweep exists so a fixed rule reaches the reports it already
    got wrong, and a signed-off report is as capable of carrying a wrong answer
    as any other. But with a rule changing several times a day, sweeping them
    re-reads work that is already done, over and over, and every pass that
    finds a new failure pulls somebody's sign-off - so the queue never empties
    and the board keeps un-reviewing itself.

    So the sweep now covers what is still in the way, and the signed-off ones
    are counted on the board with a button to do them deliberately. Before
    delivery is the moment that matters, and that is a decision rather than
    something that should happen while somebody is mid-cycle.
    """
    done = 0
    for rep in _stale_batch(db, limit, scoped=scoped, group=group, period=period,
                            skip_signed=True):
        try:
            out = recheck(db, rep)
            done += 1
            if out.get("ok") and (out["was"] != out["now"] or out["new_failures"]):
                log.info("rechecked %s %s: %s -> %s%s", rep.id, rep.client,
                         out["was"], out["now"],
                         " (sign-off reset)" if out["signoff_reset"] else "")
        except Exception as exc:                       # never let one PDF stop the sweep
            log.warning("recheck failed for report %s: %s", rep.id, exc)
            rep.rules_version = rules_version()        # do not retry it forever
            db.commit()
            done += 1
    return done


def _remap_orders_if_stale() -> None:
    """Re-read the order export when the code that interprets it has changed.

    Reports get this treatment already: a rule changes, the reports it judged
    are re-read. The order list needs the same and did not have it - the export
    is parsed into products once and only the products are kept, so a mapping
    fix left every loaded order carrying the old answer, and the ETag test
    meant the file was never read again to correct it.

    Best effort. No S3, no credentials, someone else already syncing - none of
    those are this thread's problem, and none of them should stop the report
    sweep that runs after it.
    """
    from .db import OrderSync
    from .version import product_map_version

    db = SessionLocal()
    try:
        prev = db.scalars(select(OrderSync).where(OrderSync.state != "running")
                          .order_by(OrderSync.id.desc()).limit(1)).first()
        if prev is None or not prev.ok:
            return                        # nothing loaded, so nothing is stale
        if (prev.map_version or "") == product_map_version():
            return
        from .orders_s3 import begin_sync, sync as sync_orders
        claim = begin_sync(db, trigger="rules")
        if claim is None:
            return                        # the other worker has it
        log.info("re-reading the order export: the product mapping changed")
        rec = sync_orders(db, force=True, claim_id=claim.id, trigger="rules")
        log.info("order re-read: %s", getattr(rec, "message", ""))
    except Exception as exc:              # noqa: BLE001
        log.warning("could not re-read the order export: %s", exc)
    finally:
        db.close()


def start_sweeper() -> None:
    """Run the sweep in the background until nothing is stale.

    A daemon thread rather than a scheduler: it has one job, it finishes, and
    it must not keep a worker alive at shutdown. Both gunicorn workers start
    one; they take from the same queue and the row each claims is stamped
    immediately, so the overlap costs a duplicate read at worst, never a wrong
    answer.
    """
    if _running.is_set():
        return
    _running.set()

    def run():
        import time
        time.sleep(5)                     # let the first requests through
        # OUTSIDE THE auto_recheck GATE, deliberately.
        #
        # This used to sit behind it, so on a deploy with the automatic report
        # sweep turned off the order export was never re-read either - and the
        # board went on answering from order data an older import produced,
        # with nothing to show for it but the same product finding coming back.
        # Re-reading the orders is not the same job as re-reading the PDFs.
        _remap_orders_if_stale()
        if not settings.auto_recheck:
            _running.clear()
            return
        while True:
            db = SessionLocal()
            started = time.monotonic()
            try:
                if not stale_count(db, scoped=True, skip_signed=True):
                    log.info("recheck sweep: nothing stale that is still open")
                    break
                n = sweep_once(db)
                if not n:
                    break
            except Exception as exc:      # a dead database is not this thread's problem
                log.warning("recheck sweep paused: %s", exc)
                break
            finally:
                db.close()
            time.sleep(min(time.monotonic() - started, MAX_REST_SECONDS))
        _running.clear()

    threading.Thread(target=run, name="recheck-sweeper", daemon=True).start()


# ------------------------------------------------------ re-check on demand
def job_row(db: Session, key: str):
    from .db import RecheckJob
    return db.scalar(select(RecheckJob).where(RecheckJob.key == key))


def running_jobs(db: Session) -> dict[str, dict]:
    """Every re-check currently going, readable from either worker.

    Held in process memory this was invisible to the other gunicorn worker, so
    pressing the button and landing on the wrong one showed no job at all.
    """
    from .db import RecheckJob
    out = {}
    for j in db.scalars(select(RecheckJob).where(RecheckJob.state == "running")).all():
        out[j.key] = {"group": j.partner_group, "period": j.period,
                      "done": j.done, "total": j.total, "changed": j.changed,
                      "stalled": j.stalled, "note": j.note}
    return out


def _touch(db: Session, key: str, **fields) -> None:
    from .db import RecheckJob
    row = db.scalar(select(RecheckJob).where(RecheckJob.key == key))
    if row is None:
        return
    for k, v in fields.items():
        setattr(row, k, v)
    row.updated_at = dt.datetime.utcnow()
    db.commit()


def start_job(db: Session, key: str, *, group: str | None = None,
              period: str | None = None, stale_only: bool = True,
              skip_signed: bool = False, signed_only: bool = False) -> dict:
    """Re-check a partner, or a whole cycle, now.

    The sweep gets to everything eventually; this is for when eventually is not
    soon enough - after a fix has gone out and somebody wants that partner's
    board right rather than right in twenty minutes.
    """
    from .db import RecheckJob

    row = db.scalar(select(RecheckJob).where(RecheckJob.key == key))
    if row is not None and row.state == "running" and not row.stalled:
        return {"done": row.done, "total": row.total}
    if row is None:
        row = RecheckJob(key=key)
        db.add(row)
    row.partner_group = group or ""
    row.period = period or ""
    row.state = "running"
    row.total = stale_count(db, group=group, period=period,
                            stale_only=stale_only, skip_signed=skip_signed,
                            signed_only=signed_only)
    row.done = row.changed = 0
    row.note = ""
    row.started_at = row.updated_at = dt.datetime.utcnow()
    db.commit()
    total = row.total

    def run():
        own = SessionLocal()
        after = 0
        done = changed = 0
        try:
            while True:
                batch = _stale_batch(own, BATCH, scoped=False, group=group,
                                     period=period, stale_only=stale_only,
                                     after=0 if stale_only else after,
                                     skip_signed=skip_signed,
                                     signed_only=signed_only)
                if not batch:
                    break
                if not stale_only:
                    after = max(r.id for r in batch)
                for rep in batch:
                    was = rep.severity
                    try:
                        out = recheck(own, rep)
                    except Exception as exc:                     # noqa: BLE001
                        log.warning("recheck failed for %s: %s", rep.id, exc)
                        rep.rules_version = rules_version()
                        own.commit()
                        out = {"ok": False}
                    done += 1
                    if out.get("ok") and out.get("now") != was:
                        changed += 1
                    # Written every report, not every batch: a job that stops
                    # halfway has to be able to say where it got to.
                    _touch(own, key, done=done, changed=changed)
            _touch(own, key, state="done", done=done, changed=changed)
        except Exception as exc:                                 # noqa: BLE001
            log.warning("recheck job %s stopped: %s", key, exc)
            try:
                _touch(own, key, state="failed", note=f"{type(exc).__name__}: {exc}"[:255])
            except Exception:                                    # noqa: BLE001
                pass
        finally:
            own.close()

    threading.Thread(target=run, name=f"recheck-{key}", daemon=True).start()
    return {"done": 0, "total": total}
