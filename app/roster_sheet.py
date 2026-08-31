"""The reporting breakout sheet, read live.

WHY THIS EXISTS.

The roster - who buys, who reports, who trains, where a partner takes delivery
- lived in the tool as a copy: a CSV bundled in the repo for a fresh database,
and an upload on the Partners page for everything after. So a reporter changing
hands in the sheet reached the board when somebody remembered to export the
sheet and upload it, and not before. The board kept showing whoever it was told
about last, with nothing on screen that looked wrong.

Nothing new had to be built to fix that. The tool already authenticates to
Google with a refresh token and the full Drive scope, because that is how it
packages reports into Drive - and a Google Sheet is a Drive file. So the sheet
is exported as CSV through the credentials that are already there: no service
account, no extra scope, no new dependency, no login.

TWO THINGS IT WILL NOT DO.

It never writes back. The sheet is the source of truth for who owns what, and a
tool that edits the sheet it reads is a tool nobody trusts.

And it will not replace the roster with something that is not one. A read that
comes back as a sign-in page, or empty because a tab was renamed, parses
perfectly well as "a roster with no partners in it" - and importing that
deletes 206 partners and every owner on the board with them. So the import
refuses anything much shorter than what is already loaded, and says so.
"""
from __future__ import annotations

import hashlib
import logging
import re

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from .config import settings
from .db import OrderSync

log = logging.getLogger("report-qa")

SOURCE = "roster sheet"

# A pasted browser URL, a bare id, or an /export link - all the same thing.
SHEET_ID = re.compile(r"/spreadsheets/d/([a-zA-Z0-9-_]+)")
GID = re.compile(r"[#&?]gid=(\d+)")


def sheet_id() -> str:
    raw = (settings.roster_sheet or "").strip()
    if not raw:
        return ""
    m = SHEET_ID.search(raw)
    return m.group(1) if m else raw.split("?", 1)[0].strip("/")


def sheet_gid() -> str:
    raw = (settings.roster_sheet or "").strip()
    m = GID.search(raw)
    return m.group(1) if m else ""


def configured() -> bool:
    return bool(sheet_id())


def fetch_csv() -> bytes:
    """The sheet as CSV, through the Drive credentials already configured.

    THE FIRST TAB, which is what Drive's export gives you. That is the roster
    on this workbook. If it ever moves to a named tab that is not first, the
    gid on the URL is used instead - the same export, addressed by tab, which
    an authorised request can ask for directly.
    """
    from .delivery import _drive_credentials

    creds = _drive_credentials()
    gid = sheet_gid()
    if gid and gid != "0":
        # A specific tab. Drive's files.export takes no gid, so this asks the
        # spreadsheet's own export endpoint with the same token.
        import google.auth.transport.requests as _gr
        session = _gr.AuthorizedSession(creds)
        url = (f"https://docs.google.com/spreadsheets/d/{sheet_id()}"
               f"/export?format=csv&gid={gid}")
        resp = session.get(url, timeout=60)
        resp.raise_for_status()
        return resp.content

    from googleapiclient.discovery import build
    svc = build("drive", "v3", credentials=creds, cache_discovery=False)
    return svc.files().export_media(
        fileId=sheet_id(), mimeType="text/csv").execute()


def sync_roster(db: Session, *, force: bool = False) -> OrderSync | None:
    """Read the breakout sheet and load it. Returns the record, or None.

    NEVER RAISES INTO WHATEVER CALLED IT. This runs on the same triggers as the
    order sync, and a sheet nobody shared with the right account is not a reason
    for the orders to fail to load.
    """
    from .partners import NotARoster, backfill_targets, import_partners
    from .db import Partner

    if not configured():
        return None
    prev = db.scalars(select(OrderSync).where(OrderSync.source.like(SOURCE + "%"))
                      .order_by(desc(OrderSync.id)).limit(1)).first()
    try:
        raw = fetch_csv()
        digest = hashlib.sha256(raw).hexdigest()[:32]
        if not force and prev and prev.ok and prev.etag == digest:
            return prev                    # the sheet has not been edited

        # HOW MANY IT HAS NOW, as the floor for what a read is allowed to
        # leave behind. Two thirds, so a genuine batch of leavers still loads
        # and a sign-in page never does.
        have = db.query(Partner).count()
        n = import_partners(db, raw, keep_targets=True,
                            min_rows=int(have * 0.66) if have else 0)
        backfill_targets(db)
        msg = (f"Read {n} partners from the breakout sheet. Delivery targets "
               f"the sheet does not name were kept as they were.")
        rec = OrderSync(source=f"{SOURCE}: {sheet_id()[:24]}"[:512],
                        etag=digest, rows=n, ok=True, message=msg,
                        trigger="sheet")
        db.add(rec); db.commit()
        log.info("roster sheet: %s", msg)
        return rec
    except Exception as exc:                                 # noqa: BLE001
        db.rollback()
        hint = ""
        if isinstance(exc, NotARoster):
            hint = ""
        elif "403" in str(exc) or "404" in str(exc):
            hint = (" Share the sheet with the Google account the Drive "
                    "connection authorised as, with at least Viewer.")
        rec = OrderSync(source=SOURCE, rows=0, ok=False, trigger="sheet",
                        message=f"Breakout sheet: {type(exc).__name__}: {exc}"
                                f"{hint} The roster already loaded is untouched.")
        db.add(rec); db.commit()
        log.exception("roster sheet read failed")
        return rec


def last_read(db: Session) -> OrderSync | None:
    return db.scalars(select(OrderSync).where(OrderSync.source.like(SOURCE + "%"))
                      .order_by(desc(OrderSync.id)).limit(1)).first()
