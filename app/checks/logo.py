"""Which logo is in the top-left corner of page one.

Every report should carry the partner station's logo there - SEVEN MOUNTAINS,
and so on - or, occasionally, the client's own. What it must not carry is the
reporting tool's default: a generic blue bar-chart icon that says nothing about
who sent the report, and goes out to the client looking like a template nobody
finished.

IT IS TOLD WHICH LOGO IS THE GENERIC ONE. It does not guess.

The first version counted markets: a mark on three or more of them could not be
any one partner's, so it must be the tool's. That is wrong, and Ken Waddell
proved it in a day - Seven Mountains runs 7 Mountains PA, PA Altoona and KY as
separate markets on this board and prints the same, entirely correct, logo on
all of them. Any threshold that catches the tool's default also catches every
group that covers more than a couple of markets.

So the corner is hashed and the hash is stored, and a person marks a hash as
the tool's default once, from a report that has it, looking at a picture of the
actual crop. After that every report carrying that mark fails, and nothing else
does. One click, no guessing, and no false positives to argue with.
"""
from __future__ import annotations

import hashlib
import subprocess

from .. import proc as _proc
import tempfile
from pathlib import Path

# The header logo sits in the top-left corner. Generous enough to survive a
# taller or wider mark, tight enough not to take in "Digital Marketing Report",
# which is centred, or the date block on the right.
BOX = (0.0, 0.0, 0.22, 0.075)

# Downsampled to 96x32 greyscale before hashing. The same logo rendered by two
# builds of poppler can differ by a pixel of anti-aliasing, and a hash of the
# full-size crop would call those two different logos.
SIZE = (96, 32)
DPI = 72


def header_logo_hash(path: str | Path) -> str:
    """A stable fingerprint of page one's top-left corner, or "".

    Returns "" rather than raising on anything that goes wrong - a missing
    poppler, an unreadable PDF, a report with no pages. A check that cannot
    see the logo should be quiet, not broken.
    """
    try:
        from PIL import Image
    except ImportError:
        return ""
    try:
        with tempfile.TemporaryDirectory() as d:
            _proc.run(
                ["pdftoppm", "-f", "1", "-l", "1", "-r", str(DPI), "-png",
                 str(path), f"{d}/p"],
                check=True, capture_output=True, timeout=30)
            pages = sorted(Path(d).glob("p*.png"))
            if not pages:
                return ""
            im = Image.open(pages[0]).convert("L")
            w, h = im.size
            crop = im.crop((int(BOX[0] * w), int(BOX[1] * h),
                            int(BOX[2] * w), int(BOX[3] * h))).resize(SIZE)
            return hashlib.sha1(crop.tobytes()).hexdigest()[:16]
    except Exception:                                    # noqa: BLE001
        return ""


def logo_markets(db, logo: str, exclude_id: int | None = None) -> list[str]:
    """Every market whose reports carry this logo. Shown, not judged on."""
    from sqlalchemy import select

    from ..db import Report
    if not logo:
        return []
    q = select(Report.market).where(Report.logo_hash == logo).distinct()
    if exclude_id:
        q = q.where(Report.id != exclude_id)
    return sorted({(m or "").strip() for m in db.scalars(q).all() if (m or "").strip()})


def logo_reports(db, logo: str, exclude_id: int | None = None, limit: int = 200):
    """Every report carrying this logo, newest cycle first.

    Marking a logo is a statement about all of them, so the page that takes the
    mark shows which ones it lands on by name, rather than leaving you to guess
    who else in the partner is about to change.
    """
    from sqlalchemy import select

    from ..db import Report
    if not logo:
        return []
    q = select(Report).where(Report.logo_hash == logo)
    if exclude_id:
        q = q.where(Report.id != exclude_id)
    return list(db.scalars(q.order_by(Report.period.desc(),
                                      Report.market.asc(),
                                      Report.client.asc()).limit(limit)).all())


def is_generic(db, logo: str) -> bool:
    """Has somebody marked this mark as the reporting tool's default?"""
    from sqlalchemy import select

    from ..db import KnownLogo
    if not logo:
        return False
    row = db.scalar(select(KnownLogo).where(KnownLogo.logo_hash == logo))
    return bool(row and row.kind == "generic")


def crop_png(path: str | Path) -> bytes:
    """The same corner the hash is taken from, as a PNG.

    Marking a logo is a decision about a picture, so the page shows the
    picture - the exact pixels the fingerprint was taken from, not an
    approximation of them.
    """
    import io
    try:
        from PIL import Image
    except ImportError:
        return b""
    try:
        with tempfile.TemporaryDirectory() as d:
            _proc.run(
                ["pdftoppm", "-f", "1", "-l", "1", "-r", "150", "-png",
                 str(path), f"{d}/p"],
                check=True, capture_output=True, timeout=30)
            pages = sorted(Path(d).glob("p*.png"))
            if not pages:
                return b""
            im = Image.open(pages[0])
            w, h = im.size
            crop = im.crop((int(BOX[0] * w), int(BOX[1] * h),
                            int(BOX[2] * w), int(BOX[3] * h)))
            buf = io.BytesIO()
            crop.save(buf, format="PNG")
            return buf.getvalue()
    except Exception:                                    # noqa: BLE001
        return b""
