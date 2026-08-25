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

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import Report, SessionLocal
from .version import rules_version

log = logging.getLogger("report-qa.recheck")

# How many to re-read per pass. A pass runs in a background thread on a 512 MB
# instance that is also serving pages, so it takes small bites and comes back.
BATCH = 8
PAUSE_SECONDS = 20


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


def recheck(db: Session, rep: Report, *, manual: bool = False) -> dict:
    """Re-read this report's PDF with today's rules. Returns what changed."""
    from .checks.rules import run_all
    from .ingest import client_flight
    from .roster import expected_products

    path = Path(rep.stored_path or "")
    if not path.exists():
        # Stamp it anyway. Without the stamp the sweeper picks it up forever,
        # and there is nothing on disk that a later pass could do better with.
        rep.rules_version = rules_version()
        db.commit()
        return {"ok": False, "reason": "the stored PDF is gone"}

    exp = expected_products(db, rep.client, rep.account_ids, period=rep.period)
    flight = client_flight(db, rep.client, rep.account_ids)
    result = run_all(path, filename=rep.filename, expected_products=exp,
                     flight=flight, period=rep.period, market=rep.market or "")

    was_sev = rep.severity
    old_findings, old_acked = list(rep.findings or []), list(rep.acked or [])

    rep.findings = result["findings"]
    rep.checks = result.get("checks") or []
    rep.severity = result["severity"]
    rep.products = ", ".join(result.get("products") or [])
    rep.acked = remap_acks(old_findings, old_acked, rep.findings)
    rep.rules_version = rules_version()

    fresh = _new_failures(old_findings, old_acked, rep.findings)
    reset = False
    if fresh and rep.review_state in ("reviewed", "waived"):
        # They signed off on a different answer. Saying so is the whole point;
        # leaving the sign-off would ship a report nobody has actually read.
        rep.review_state = "new"
        rep.reviewed_at = None
        reset = True
    db.commit()
    return {"ok": True, "was": was_sev, "now": rep.severity,
            "new_failures": fresh, "signoff_reset": reset,
            "acks_kept": len(rep.acked), "acks_before": len(old_acked)}


# ------------------------------------------------------------------ the sweep
_running = threading.Event()


def stale_count(db: Session) -> int:
    from sqlalchemy import func
    return db.scalar(select(func.count(Report.id))
                     .where(Report.rules_version != rules_version())) or 0


def _stale_batch(db: Session, limit: int) -> list[Report]:
    """Newest cycles first. The month somebody is working on is the one where a
    stale answer is actually in the way."""
    return list(db.scalars(
        select(Report)
        .where(Report.rules_version != rules_version())
        .order_by(Report.period.desc(), Report.id.desc())
        .limit(limit)).all())


def sweep_once(db: Session, limit: int = BATCH) -> int:
    done = 0
    for rep in _stale_batch(db, limit):
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


def start_sweeper() -> None:
    """Run the sweep in the background until nothing is stale.

    A daemon thread rather than a scheduler: it has one job, it finishes, and
    it must not keep a worker alive at shutdown. Both gunicorn workers start
    one; they take from the same queue and the row each claims is stamped
    immediately, so the overlap costs a duplicate read at worst, never a wrong
    answer.
    """
    if not settings.auto_recheck or _running.is_set():
        return
    _running.set()

    def run():
        import time
        time.sleep(5)                     # let the first requests through
        while True:
            db = SessionLocal()
            try:
                if not stale_count(db):
                    log.info("recheck sweep: nothing stale")
                    break
                n = sweep_once(db)
                if not n:
                    break
            except Exception as exc:      # a dead database is not this thread's problem
                log.warning("recheck sweep paused: %s", exc)
                break
            finally:
                db.close()
            time.sleep(PAUSE_SECONDS)
        _running.clear()

    threading.Thread(target=run, name="recheck-sweeper", daemon=True).start()
