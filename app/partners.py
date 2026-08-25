"""The reporting roster: who pulls each partner's reports, who trains them,
and who the finished reports go to.

Seeded from `app/seed/partners.csv`, which is the reporting roster sheet
exported. It ships in the repo so a fresh deploy has the roster without any
setup, and it can be replaced from the Partners page when the sheet changes.

The other half of this module is owner resolution. The IO export carries a
campaign manager per line item, but not every line has one. When it is blank
the partner's buyer takes over - except on SEO line items, where the partner's
SEO person does. SEO is pulled outside TapClicks, so those never appear in a
monthly batch; the fallback matters for the order list and the completeness
check, not for the report checks.
"""
from __future__ import annotations

import csv
import io
import logging
import re
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import Partner

log = logging.getLogger("reportqa.partners")

SEED = Path(__file__).parent / "seed" / "partners.csv"

FIELDS = ("partner", "buyer", "buyer_email", "seo", "seo_email", "manager",
          "reporting_team", "to_emails", "trainer", "reporting_notes", "buyer_notes",
          "group", "delivery_target")

# Header spellings seen in exports of the roster sheet. The canonical field
# names are added below too - the bundled seed file is written with those, and
# leaving them out silently dropped every column whose sheet name had a space
# in it while the single-word ones came through fine.
HEADER_ALIASES = {
    "partner": "partner", "market": "partner",
    "buyer": "buyer",
    "email": "buyer_email", "buyer email": "buyer_email",
    "seo": "seo", "seo email": "seo_email", "email.1": "seo_email",
    "manager": "manager",
    "reporting team": "reporting_team", "reporter": "reporting_team",
    "to:": "to_emails", "to": "to_emails",
    "trainer": "trainer",
    "reporting notes": "reporting_notes",
    "buyer notes": "buyer_notes",
    # Delivery. Markets sharing a group ship as one link.
    "group": "group", "partner group": "group", "parent": "group",
    "delivery": "delivery_target", "delivery target": "delivery_target",
}
HEADER_ALIASES.update({f: f for f in FIELDS})
HEADER_ALIASES.update({f.replace("_", " "): f for f in FIELDS})


def _key(name: str) -> str:
    """Partner names are typed by hand in two systems, so match on letters and
    digits only: '7 Mountains PA - State College' and '7 Mountains PA State
    College' are the same partner."""
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def import_partners(db: Session, raw: bytes | str, *, replace: bool = True) -> int:
    text = raw.decode("utf-8-sig", "replace") if isinstance(raw, bytes) else raw
    reader = csv.reader(io.StringIO(text))
    header = next(reader, None)
    if not header:
        return 0

    # Duplicate "Email" headers: the first belongs to Buyer, the second to SEO.
    idx, seen_email = {}, 0
    for i, h in enumerate(header):
        h = (h or "").strip().lower()
        if h == "email":
            seen_email += 1
            idx["buyer_email" if seen_email == 1 else "seo_email"] = i
            continue
        field = HEADER_ALIASES.get(h)
        if field and field not in idx:
            idx[field] = i
    if "partner" not in idx:
        raise ValueError("No Partner column in that file.")

    # STOP AT THE FIRST BLANK PARTNER, do not skip past it.
    #
    # The roster workbook has a second table below this one with the same
    # twelve columns - the report-issue log, where column B is a client and
    # column C is a bug description. Reading straight through merged the two,
    # and because the issue log repeats partner names it overwrote real roster
    # rows: "7 Mountains KY" came out with Buyer "Expree Credit Union" and SEO
    # "Impressions/clicks not matching". A blank partner cell is the boundary.
    rows: dict[str, dict] = {}
    for row in reader:
        get = lambda f: (row[idx[f]].strip() if f in idx and idx[f] < len(row) else "")  # noqa: E731
        name = get("partner")
        if not name:
            if rows:
                break
            continue                       # leading blanks before the header
        if _key(name) == "partner":        # a repeated header row
            continue
        # Second guard: a roster row always names a Vici person by email. An
        # issue-log row put a URL or a sentence here.
        be = get("buyer_email")
        if be and "@" not in be:
            break
        row = {f: get(f) for f in FIELDS} | {"partner": name}
        row["group"] = row["group"] or name      # blank = ships on its own
        rows[_key(name)] = row

    if replace:
        db.query(Partner).delete()
    for r in rows.values():
        db.add(Partner(**r))
    db.commit()
    return len(rows)


def seed_if_empty(db: Session) -> int:
    """Load the bundled roster on a fresh database.

    Only when the table is empty, so a roster the user uploaded later is never
    overwritten by a deploy.
    """
    if db.query(Partner).count():
        return 0
    if not SEED.exists():
        log.warning("no bundled partner roster at %s", SEED)
        return 0
    n = import_partners(db, SEED.read_bytes())
    log.info("seeded %d partners from the bundled roster", n)
    return n


def backfill_targets(db: Session) -> int:
    """Fill in where a partner takes delivery, when the loaded roster is silent.

    A roster exported from the sheet without the Delivery column loads every
    other field and leaves this one blank, and a blank target means Drive. So
    7 Mountains NY Elmira/Mansfield - a Dropbox partner - was packaged and the
    client was handed a Google Drive link, with nothing on screen that looked
    wrong. The bundled seed knows the answer for all 206 partners; this only
    fills blanks, so an uploaded roster that DOES say still wins.
    """
    if not SEED.exists():
        return 0
    blanks = [p for p in db.scalars(select(Partner)).all()
              if not (p.delivery_target or "").strip()]
    if not blanks:
        return 0
    want: dict[str, str] = {}
    reader = csv.reader(io.StringIO(SEED.read_text(encoding="utf-8-sig")))
    header = next(reader, None) or []
    idx = {(h or "").strip().lower(): i for i, h in enumerate(header)}
    pi, ti = idx.get("partner"), idx.get("delivery_target")
    if pi is None or ti is None:
        return 0
    for row in reader:
        if len(row) > max(pi, ti) and row[pi].strip() and row[ti].strip():
            want[_key(row[pi])] = row[ti].strip()
    n = 0
    for p in blanks:
        t = want.get(_key(p.partner))
        if t:
            p.delivery_target = t
            n += 1
    if n:
        db.commit()
        log.info("filled in the delivery target for %d partner(s)", n)
    return n


_CACHE: dict[str, Partner] | None = None


def all_partners(db: Session) -> list[Partner]:
    return db.scalars(select(Partner).order_by(Partner.partner)).all()


def find(db: Session, name: str) -> Partner | None:
    """The partner row for a market name, tolerant of spelling differences."""
    k = _key(name)
    if not k:
        return None
    rows = all_partners(db)
    for p in rows:
        if _key(p.partner) == k:
            return p
    # One name is often a prefix of the other ("7 Mountains PA" vs
    # "7 Mountains PA State College"). Prefer the longest match so the more
    # specific partner wins over its own parent.
    best = None
    for p in rows:
        pk = _key(p.partner)
        if pk and (pk in k or k in pk):
            if best is None or len(_key(best.partner)) < len(pk):
                best = p
    return best


def is_seo(product: str) -> bool:
    return "seo" in (product or "").lower() or "search engine" in (product or "").lower()


def resolve_owner(partner: Partner | None, product: str,
                  campaign_manager: str = "", manager_email: str = "") -> tuple[str, str]:
    """(name, email) for whoever owns this line item.

    The campaign manager off the IO export wins when it is there. Otherwise the
    partner's buyer covers it, and an SEO line item goes to the partner's SEO
    person instead.
    """
    if campaign_manager.strip():
        return campaign_manager.strip(), manager_email.strip()
    if partner is None:
        return "", ""
    if is_seo(product):
        return partner.seo or "", partner.seo_email or ""
    return partner.buyer or "", partner.buyer_email or ""
