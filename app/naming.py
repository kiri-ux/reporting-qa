"""What a report is called, once this tool has decided what it is.

A file arrives called whatever the person who pulled it left it as - often
"Digital Marketing Report.pdf", sometimes "download (2).pdf" - and that name
then follows it onto the board, into the zip, and into the partner's folder.
Nothing downstream can file by a name like that.

So the name is BUILT, not kept: the cycle, the client, and the order ids that
report covers, in the shape the feed already uses.

    July 2026_All Seasons Powersports 53908.pdf
    Lifetime_All Seasons Powersports 53908.pdf

The original is only a fallback, for a report we know nothing about yet.
"""
from __future__ import annotations

import datetime as dt
import re

SAFE = re.compile(r"[^A-Za-z0-9._ &,()+-]")
DUPLICATE_SUFFIX = re.compile(r"(?:\s*\(\d+\)|\s+copy(?:\s+\d+)?)+$", re.I)


def _safe(s: str, limit: int = 150) -> str:
    return SAFE.sub("_", (s or "").strip())[:limit].strip()


def month_label(period: str) -> str:
    """"2026-07" -> "July 2026"."""
    try:
        return dt.date.fromisoformat((period or "") + "-01").strftime("%B %Y")
    except ValueError:
        return ""


def ids_of(raw: str) -> str:
    """The order ids on a report, de-duplicated, in the order they appear."""
    parts = re.split(r"[,\s;/]+", (raw or "").strip())
    return " ".join(dict.fromkeys(p for p in parts if p))


def canonical_name(rep) -> str:
    """The name this report should be filed under.

    Falls back to its own name - minus a browser's "(1)" - when there is not
    enough known to build one. That matters: "Service One Credit Union (1)"
    read as a different client from "Service One Credit Union", so a corrected
    file filed itself as a new report instead of replacing the one it corrects.
    """
    client = _safe(getattr(rep, "client", "") or "")
    ids = ids_of(getattr(rep, "account_ids", "") or "")
    prefix = ("Lifetime" if getattr(rep, "is_lifetime", False)
              else month_label(getattr(rep, "period", "") or ""))
    if client and prefix:
        stem = f"{prefix}_{client}" + (f" {ids}" if ids else "")
        return f"{stem}.pdf"

    raw = (getattr(rep, "filename", "") or "").strip()
    stem, dot, ext = raw.rpartition(".")
    if not dot:
        stem, ext = raw, "pdf"
    stem = DUPLICATE_SUFFIX.sub("", stem).strip()
    if not stem:
        stem = f"report-{getattr(rep, 'id', '') or 'unnamed'}"
    return f"{_safe(stem)}.{(ext or 'pdf').lower()}"


def ids_for_report(db, rep) -> str:
    """EVERY ORDER THIS REPORT COVERS, not just the one it was filed under.

    Congressman Mike Kelly's July report covers CTV on order 53130 and Online
    Audio on 50589 and 53130 - and was named "July 2026_Congressman Mike Kelly
    53130.pdf", because the ids were only ever filled in when the file arrived
    with none at all. A name that names one of three orders is worse than one
    that names none: it looks complete.

    Scoped to the lines this report is judged against - the ones that ran in
    the period, or on a lifetime the ones inside the campaign's flight. A
    client's other campaign is not in this report and does not belong in its
    name.
    """
    from .roster import _overlaps, _ran_during, client_lines

    have = ids_of((getattr(rep, "account_ids", "") or "").replace(",", " "))
    hit = client_lines(db, getattr(rep, "client", ""), have) or []
    if not hit:
        return have

    period = getattr(rep, "period", "") or ""
    if getattr(rep, "is_lifetime", False):
        from .ingest import client_flight
        window = client_flight(db, rep.client, have)
        if window and window[0]:
            hit = [l for l in hit if _overlaps(l, window[0], window[1])]
    elif period:
        hit = [l for l in hit if _ran_during(l, period)]

    out = list(have.split()) if have else []
    for l in hit:
        if getattr(l, "canceled", False):
            continue
        for i in (l.account_ids or "").replace(",", " ").split():
            if i not in out:
                out.append(i)
    # Stable and readable: the file was filed under one of these, and a name
    # whose ids move around between re-checks is a name nobody can search for.
    return " ".join(sorted(out))[:255]
