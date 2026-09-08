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


def _extension_of(rep) -> str:
    """The extension this report's file actually has - the stored file first.

    The stored path is the fact; the name is only what it was last called, and
    on a report whose deck replaced a PDF the two disagree for one save.
    """
    for attr in ("stored_path", "filename"):
        v = (getattr(rep, attr, "") or "").lower()
        if v.endswith(".pptx"):
            return ".pptx"
        if v.endswith(".pdf"):
            return ".pdf"
    return ".pdf"


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
    # NOT ALWAYS .pdf. Some SEO comes back as a PowerPoint deck, and a deck
    # filed as a .pdf is a file the partner cannot open. The extension comes
    # from what is actually on disk.
    ext = _extension_of(rep)
    if client and prefix:
        stem = f"{prefix}_{client}" + (f" {ids}" if ids else "")
        return f"{stem}{ext}"

    raw = (getattr(rep, "filename", "") or "").strip()
    stem, dot, ext = raw.rpartition(".")
    if not dot:
        stem, ext = raw, "pdf"
    stem = DUPLICATE_SUFFIX.sub("", stem).strip()
    if not stem:
        stem = f"report-{getattr(rep, 'id', '') or 'unnamed'}"
    return f"{_safe(stem)}.{(ext or 'pdf').lower()}"


def rebuild_ids(db, rep) -> str:
    """The orders this report covers, worked out again from the client alone.

    ids_for_report below only ever ADDS to what a report already carries, which
    is right when the stored ids are right and no use at all when they are not.
    They were not: a year in a campaign name was read as an order id, so "LMSD
    - Z90 Secret Contest 2026" matched every other campaign named after the
    year, and its report came out carrying 51666 51923 53511 54820 54822 54824
    55200 - one of which is Z90's, and 55200 belongs to Excel Summer-Fall 2026
    at Red Pony Marketing. Fixing the id rule fixed nothing already stored,
    because nothing re-derived these. A re-check rewrote the findings and left
    the ids exactly as the import first read them, which is the same shape as
    the impressions bug in recheck.py.

    So this starts from the client's name and nothing else, and can therefore
    take an id AWAY. It returns "" when it cannot tell, and the caller keeps
    what it has: a report whose client is not on the order list is not an
    invitation to blank the name it is filed under.
    """
    from .roster import _overlaps, _ran_during, client_lines

    # By name only. Handing it the stored ids is how the wrong ones survive.
    hit = client_lines(db, getattr(rep, "client", "") or "", "") or []
    if not hit:
        return ""
    period = getattr(rep, "period", "") or ""
    if getattr(rep, "is_lifetime", False):
        from .ingest import client_flight
        window = client_flight(db, rep.client, "")
        if window and window[0]:
            hit = [l for l in hit if _overlaps(l, window[0], window[1])]
    elif period:
        hit = [l for l in hit if _ran_during(l, period)]

    out: list[str] = []
    for l in hit:
        if getattr(l, "canceled", False):
            continue
        for i in (l.account_ids or "").replace(",", " ").split():
            if i not in out:
                out.append(i)
    return " ".join(sorted(out))[:255]


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
