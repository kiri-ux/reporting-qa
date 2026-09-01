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
from functools import lru_cache
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


class NotARoster(ValueError):
    """What came back is not the roster, so nothing was touched.

    THE FAILURE THAT MATTERS HERE IS THE SILENT ONE. A sheet read that comes
    back as a Google sign-in page, or as an empty range because a tab was
    renamed, parses as "a file with no partners in it" - and a straight import
    of that deletes 206 partners and every owner on the board with them. So a
    read has to look like a roster before it is allowed to replace one.
    """


def import_partners(db: Session, raw: bytes | str, *, replace: bool = True,
                    keep_targets: bool = False, min_rows: int = 0) -> int:
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

    if min_rows and len(rows) < min_rows:
        raise NotARoster(
            f"Read {len(rows)} partner row(s), and the roster loaded here has "
            f"{min_rows}. Nothing was changed - a read that comes back short "
            f"is a permissions page or a renamed tab far more often than it is "
            f"the roster actually losing two thirds of its partners.")

    # WHERE A PARTNER TAKES DELIVERY IS SET IN THE TOOL, NOT ALWAYS IN THE
    # SHEET. Overwriting it with a blank hands a Dropbox partner's client a
    # Google Drive link, and nothing on screen looks wrong. So a target the
    # sheet does not mention is kept.
    had: dict[str, str] = {}
    if keep_targets:
        had = {_key(p.partner): (p.delivery_target or "")
               for p in db.query(Partner).all()}

    if replace:
        db.query(Partner).delete()
    for k, r in rows.items():
        if keep_targets and not (r.get("delivery_target") or "").strip():
            r = r | {"delivery_target": had.get(k, "")}
        db.add(Partner(**r))
    db.commit()
    forget_partners()
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
        forget_partners()
        log.info("filled in the delivery target for %d partner(s)", n)
    return n


# THE ROSTER IS READ A LOT AND CHANGES ALMOST NEVER.
#
# `find` reads the whole table, and the order list calls it once per market -
# so building the orders page ran two hundred queries returning two hundred
# rows each, forty thousand rows fetched to answer a question about two
# hundred. Held for twenty seconds and dropped the moment anything writes to
# the table, so an uploaded roster shows up straight away.
_CACHE: dict = {"at": 0.0, "rows": None, "bind": None}
CACHE_SECONDS = 20.0


def forget_partners() -> None:
    _CACHE["rows"] = None


class Row:
    """A detached copy of a partner row.

    NOT the ORM object. Those belong to the session that loaded them: it is
    closed at the end of the request and expired by any commit, so a cached one
    raises DetachedInstanceError the moment somebody reads a field off it. This
    carries the same fields and outlives the session it came from.
    """
    __slots__ = ("id", "partner", "buyer", "buyer_email", "seo", "seo_email",
                 "manager", "reporting_team", "to_emails", "trainer",
                 "reporting_notes", "buyer_notes", "group", "delivery_target")

    def __init__(self, p: Partner):
        for f in self.__slots__:
            setattr(self, f, getattr(p, f, ""))

    @property
    def recipients(self) -> list[str]:
        """The same reading of the To: cell the ORM row does - one address per
        part, taken out of whatever name or brackets surround it."""
        out = []
        for part in re.split(r"[;,]", self.to_emails or ""):
            m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", part)
            if m:
                out.append(m.group(0))
        return out


def all_partners(db: Session) -> list[Row]:
    import time
    now = time.monotonic()
    # Keyed on the database as well as the clock. Without that, a test with its
    # own in-memory database reads the roster the last one cached.
    try:
        bind = id(db.get_bind())
    except Exception:                       # noqa: BLE001
        bind = None
    if (_CACHE["rows"] is not None and _CACHE["bind"] == bind
            and now - _CACHE["at"] < CACHE_SECONDS):
        return _CACHE["rows"]
    rows = [Row(p) for p in
            db.scalars(select(Partner).order_by(Partner.partner)).all()]
    _CACHE["rows"], _CACHE["at"], _CACHE["bind"] = rows, now, bind
    return rows


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


# ------------------------------------------------------------- how a name reads
#
# WORKED OUT ONCE PER ROSTER, NOT ONCE PER LABEL.
#
# Which first names are ambiguous is a fact about the roster - about 150 names
# - and it does not change between one label on the board and the next. It was
# being worked out again on every single call: 1,964 calls to shorten a name on
# one page, each one splitting and re-grouping the whole roster to answer the
# same question. That was a third of the time the board spent building itself.
#
# The key is the set of names, so a roster change gets a different answer for
# free and nothing has to remember to clear anything.
@lru_cache(maxsize=64)
def _clashing_first_names(others: frozenset) -> frozenset:
    surnames: dict[str, set] = {}
    for o in others:
        for bit in re.split(r"\s*(?:,|&|/| and )\s*", (o or "").strip()):
            bit = bit.strip()
            if not bit:
                continue
            head, _, rest = bit.partition(" ")
            if rest.strip():
                surnames.setdefault(head.lower(), set()).add(rest.strip().lower())
    return frozenset(k for k, v in surnames.items() if len(v) > 1)


# FIRST NAMES. That is how everybody here refers to each other, and a board
# already carrying a partner, a client, five product chips and a date range does
# not need "Lauren Hunter" where "Lauren" is what anybody would say out loud.
#
# THE ROLES KEEP THE TWO KATIES APART. There is a trainer Katie and a buyer
# Katie, and they are different people - which is why one of them is written in
# the sheet as "Katie Oxman". Nothing merges them, because a name is only ever
# read inside its role: the buyer tag, the trainer tag, and one tab each on the
# workload page. Two people with the same first name in the SAME role would be
# a real collision, and that is the one case this does not shorten.
def first_name(value: str, others: set | None = None) -> str:
    """"Lauren Hunter" -> "Lauren". "Todd, Megan" -> "Todd, Megan".

    `others` is every name that appears in the same role. When two of them
    share a first name the surname stays on both, because a label that cannot
    tell two people apart is worse than a long one.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    parts = [p.strip() for p in re.split(r"\s*(?:,|&|/| and )\s*", raw) if p.strip()]
    if not parts:
        return raw

    # A CLASH IS TWO SURNAMES, NOT TWO SPELLINGS.
    #
    # "Katie Oxman" beside "Katie Reed" is two people and neither can lose its
    # surname. "Lauren" beside "Lauren Hunter" is ONE person the sheet spells
    # two ways - and treating that as a clash leaves the workload page showing
    # Lauren and Lauren Hunter as two rows with half the work each, which is
    # the thing this is meant to fix.
    clash = _clashing_first_names(frozenset(others)) if others else frozenset()

    out = []
    for p in parts:
        head = p.split()[0]
        out.append(p if head.lower() in clash else head)
    return ", ".join(out)


def role_names(db, role: str) -> set:
    """Every name used in one role, so a clash inside it can be spotted."""
    field = {"buyer": "buyer", "reporter": "reporting_team",
             "trainer": "trainer", "seo": "seo"}.get(role, "")
    if not field:
        return set()
    return {getattr(p, field, "") or "" for p in all_partners(db)}
