"""How fast reports are arriving, and what that means for the ones still out.

Reports come in one email at a time over several days, so "763 not received" on
its own does not answer the question anybody actually has, which is whether
that is a morning's work or the rest of the week.

The honest difficulty is that arrivals are bursty. A reporter pulls a market
and forty land in ten minutes, then nothing for three hours. Extrapolating from
the last ten minutes says everything will be in by lunch; extrapolating from
the last three hours says next Tuesday. So this reports several windows rather
than one number, says which one the estimate came from, and stays quiet when
there is not enough to go on.
"""
from __future__ import annotations

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .db import Batch, Report

# Windows to measure, longest last. The estimate uses the shortest window that
# has enough arrivals in it to mean anything.
WINDOWS = ((1, "the last hour"), (3, "the last 3 hours"),
           (12, "the last 12 hours"), (24, "the last day"),
           (72, "the last 3 days"))
ENOUGH = 8          # arrivals in a window before its rate is worth quoting


def arrivals(db: Session, period: str) -> list[dt.datetime]:
    """When each report for this cycle landed, oldest first."""
    rows = db.execute(
        select(Batch.received_at, func.count(Report.id))
        .join(Report, Report.batch_id == Batch.id)
        .where(Report.period == period)
        .group_by(Batch.id, Batch.received_at)).all()
    out: list[dt.datetime] = []
    for when, n in rows:
        if when:
            out.extend([when] * int(n or 0))
    out.sort()
    return out


def pace(db: Session, period: str, outstanding: int,
         now: dt.datetime | None = None) -> dict:
    """Arrival rate and, where it is honest to give one, an estimate."""
    now = now or dt.datetime.utcnow()
    seen = arrivals(db, period)
    out: dict = {"received": len(seen), "outstanding": outstanding,
                 "windows": [], "rate": None, "basis": "", "eta": None,
                 "hours": None, "first": seen[0] if seen else None,
                 "last": seen[-1] if seen else None}
    if not seen or outstanding <= 0:
        return out

    for hours, label in WINDOWS:
        since = now - dt.timedelta(hours=hours)
        n = sum(1 for t in seen if t >= since)
        # A window that reaches back before the first arrival would divide by
        # more hours than the cycle has actually been running.
        span = min(hours, max((now - seen[0]).total_seconds() / 3600, 0.05))
        out["windows"].append({"label": label, "hours": hours, "count": n,
                               "per_hour": n / span if span else 0.0})
        if out["rate"] is None and n >= ENOUGH:
            out["rate"] = n / span
            out["basis"] = label

    if out["rate"] is None:
        # Nothing recent enough to quote. Fall back to the whole cycle, which
        # is a weaker claim and is labelled as one.
        span = max((now - seen[0]).total_seconds() / 3600, 0.05)
        if len(seen) >= ENOUGH:
            out["rate"] = len(seen) / span
            out["basis"] = "the whole cycle so far"

    if out["rate"] and out["rate"] > 0:
        out["hours"] = outstanding / out["rate"]
        out["eta"] = now + dt.timedelta(hours=out["hours"])
    return out


def humanise(hours: float | None) -> str:
    """"about 4 hours", "about 2 days". Never "3.7 hours"; nobody believes the
    decimal and it makes a rough projection look like a measurement."""
    if hours is None:
        return ""
    if hours < 1:
        return f"about {max(int(hours * 60), 1)} minutes"
    if hours < 36:
        n = round(hours)
        return f"about {n} hour{'s' if n != 1 else ''}"
    n = round(hours / 24)
    return f"about {n} day{'s' if n != 1 else ''}"


def working_days(hours: float | None) -> str:
    """The same span in working hours, because nothing arrives overnight.

    A projection of "about 2 days" made from a rate measured at eleven in the
    morning is really four working days, and the difference is the whole point
    of asking.
    """
    if hours is None:
        return ""
    days = hours / 8.0
    if days < 1:
        return ""
    n = round(days)
    return f"{n} working day{'s' if n != 1 else ''} at that rate"
