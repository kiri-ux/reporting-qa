"""Is this order in the lists? One box, one answer.

WHY THIS EXISTS.

"I see this order, why is it not on the board" was taking a screenshot, a round
trip and somebody reading code. Everything needed to answer it was already
loaded - the order lines, the serving days, the sync log - and there was no way
to ask.

The answer is never just yes or no. An order can be absent because its
partner's file never landed, because it IS loaded but under a different
business unit than the one delivering it, or because it was read and dropped on
the way in. Those are three different afternoons, so this says which.
"""
from __future__ import annotations

import re

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .db import OrderLine, OrderSync, Report, ServedDays

ORDER_ID = re.compile(r"^\d{4,7}$")


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def find(db: Session, q: str, period: str) -> dict:
    """What the tool knows about an order id, a client, or a partner."""
    q = (q or "").strip()
    if not q:
        return {}
    out: dict = {"q": q, "is_id": bool(ORDER_ID.match(q)),
                 "lines": [], "served": [], "reports": [], "notes": []}
    k = _key(q)

    for l in db.scalars(select(OrderLine)).all():
        ids = f"{l.account_ids or ''} {l.line_ids or ''}".replace(",", " ").split()
        hit = (q in ids if out["is_id"]
               else (k and (k in _key(l.client) or k in _key(l.market))))
        if hit:
            out["lines"].append(l)

    # The serving side of the same question, which is the half that says
    # whether anything actually delivered.
    #
    # SEARCHED BY THE CLIENT'S NAME EVEN WHEN AN ID WAS TYPED. The serving file
    # has no order ids in it, so an id-only match finds nothing there - and
    # "nothing delivered" is the wrong answer to give about a live campaign.
    # The lines just found say what the client is called.
    names = {_key(l.client) for l in out["lines"] if l.client}
    if k and not out["is_id"]:
        names.add(k)
    for r in db.scalars(select(ServedDays).where(ServedDays.period == period)).all():
        if any(n and (n in r.client_key or r.client_key in n) for n in names) \
                or (k and not out["is_id"] and k in r.market_key):
            out["served"].append(r)

    # And any report already on the board under it.
    for rep in db.scalars(select(Report).order_by(desc(Report.id)).limit(4000)).all():
        ids = (rep.account_ids or "").replace(",", " ").split()
        if (q in ids) if out["is_id"] else (k and k in _key(rep.client or "")):
            out["reports"].append(rep)
            if len(out["reports"]) >= 12:
                break

    # WHAT IT MEANS, in the order somebody would work through it.
    if out["lines"]:
        markets = sorted({l.market for l in out["lines"] if l.market})
        out["notes"].append(
            f"Loaded. {len(out['lines'])} order line(s), under "
            + " and ".join(markets or ["no partner"]) + ".")
        if out["served"]:
            served_m = sorted({r.market for r in out["served"] if r.market})
            odd = [m for m in served_m if _key(m) not in {_key(x) for x in markets}]
            if odd:
                out["notes"].append(
                    "IT DELIVERS UNDER A DIFFERENT PARTNER: the serving file "
                    "puts it on " + " and ".join(odd) + ", the order tool on "
                    + " and ".join(markets) + ". They have to agree, or the "
                    "board reads it as a client with no order behind it.")
    else:
        out["notes"].append("Not in the order list loaded here.")
        if out["served"]:
            for r in out["served"][:3]:
                have = db.scalars(select(OrderLine).where(
                    OrderLine.market == r.market).limit(1)).first()
                out["notes"].append(
                    f"It delivered {r.days} day(s) in {period} under "
                    f"{r.market}, and that partner "
                    + ("has other orders loaded, so its file landed and this "
                       "client is not in it - check the client name in both "
                       "tools, and that the order was live when the export "
                       "was pulled."
                       if have else
                       "has NO orders loaded at all, so its export is the "
                       "thing to look at."))
        else:
            out["notes"].append(
                f"Nothing under that name delivered in {period} either, so "
                f"the serving file does not know about it any more than the "
                f"order list does.")

    sync = db.scalars(select(OrderSync)
                      .where(OrderSync.source.like("s3://%"), OrderSync.ok.is_(True))
                      .order_by(desc(OrderSync.id)).limit(1)).first()
    out["sync"] = sync
    out["lines"] = out["lines"][:60]
    out["served"] = out["served"][:20]
    return out
