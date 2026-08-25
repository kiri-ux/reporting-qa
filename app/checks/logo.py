"""Which logo is in the top-left corner of page one.

Every report should carry the partner station's logo there - SEVEN MOUNTAINS,
and so on - or, occasionally, the client's own. What it must not carry is the
reporting tool's default: a generic blue bar-chart icon that says nothing about
who sent the report, and goes out to the client looking like a template nobody
finished.

THE TEST IS NOT "IS THIS THE RIGHT LOGO". Nobody has a list of a hundred and
forty-six partner logos, and keeping one current would be its own job. What
separates a generic logo from a real one is who it belongs to:

  * a partner's logo appears on that partner's reports
  * a client's logo appears on that client's report
  * a generic logo appears on everybody's

So the corner is hashed, the hash is stored, and a logo that turns up across
several unrelated markets is the generic one. It learns which image that is
from the reports themselves and needs nothing kept up to date.
"""
from __future__ import annotations

import hashlib
import subprocess
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
            subprocess.run(
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


# How many different markets have to share one logo before it is generic. Two
# is not enough: a partner group really can cover two markets and print one
# logo across both, and one report per market of a two-market group would
# otherwise be accused of using a template.
GENERIC_AT = 3


def logo_markets(db, logo: str, exclude_id: int | None = None) -> list[str]:
    """Every market whose reports carry this logo."""
    from sqlalchemy import select

    from ..db import Report
    if not logo:
        return []
    q = select(Report.market).where(Report.logo_hash == logo).distinct()
    if exclude_id:
        q = q.where(Report.id != exclude_id)
    return sorted({(m or "").strip() for m in db.scalars(q).all() if (m or "").strip()})
