"""
Build identity.

Shown in the footer of every page and returned by /healthz, so you can confirm
which code is actually live before you trust what the screen says. Deploying
and then reading a stale container is easy to do and hard to spot - the
dashboard looks perfectly plausible either way.

BUMP `BUILD` on every deploy whose effect you need to confirm visually.
"""
from __future__ import annotations
import os

# ---- bump this on every deploy you need to confirm -------------------------
BUILD = "2026.09.04-191"
BUILD_NOTES = ("Your fixes are in, and Mobile Conquesting is out of the "
               "device-eligible total.")

# ---------------------------------------------------------------------------


def commit() -> str:
    """Render injects RENDER_GIT_COMMIT automatically."""
    c = os.getenv("RENDER_GIT_COMMIT") or os.getenv("GIT_COMMIT") or ""
    return c[:7]


def service() -> str:
    return os.getenv("RENDER_SERVICE_NAME", "local")


def label() -> str:
    """Short human-readable build string, e.g. 'build 2026.08.24-1 - a1b2c3d'."""
    parts = [f"build {BUILD}"]
    if commit():
        parts.append(commit())
    return " · ".join(parts)


def info() -> dict:
    return {"build": BUILD, "notes": BUILD_NOTES, "commit": commit(),
            "service": service()}


# --------------------------------------------------------------- rules version
# Findings are written once, when a report arrives, and then stored. A deploy
# that fixes a rule does not reach back and fix the reports that rule already
# got wrong - they keep showing yesterday's answer.
#
# So every report records the fingerprint of the checking code that judged it,
# and anything stamped with an older one gets re-checked in the background.
# The fingerprint is a hash of the source rather than a number somebody has to
# remember to bump, because the one time it is forgotten is the deploy that
# most needed it.
def rules_fingerprint() -> str:
    import hashlib
    from pathlib import Path

    here = Path(__file__).resolve().parent / "checks"
    h = hashlib.sha256()
    for name in sorted(p.name for p in here.glob("*.py")):
        h.update(name.encode())
        h.update((here / name).read_bytes())
    # AND THE CODE THAT DECIDES WHAT A RE-CHECK KEEPS - BUT ONLY THAT.
    #
    # The rules alone were too narrow by exactly the bug it was written for.
    # recheck.py is what writes a re-check's answers back onto the report, and
    # for a long time it wrote the findings and left the impressions and clicks
    # alone; fixing that changed no file under checks/, so nothing was queued
    # and no report ever ran the fix.
    #
    # Hashing the WHOLE file was then too wide by as much. Changing when the
    # sweeper starts - a sleep, nothing to do with how a report is judged -
    # queued 880 reports for a full re-read, which is an afternoon of pdftotext
    # for no change of answer at all.
    #
    # So: the two functions that actually decide what a stored report ends up
    # saying, and nothing else in the file. Parsed rather than imported,
    # because importing recheck from here is a circle.
    h.update(_recheck_answers().encode())
    # AND ROSTER.PY, WHICH DECIDES WHAT THE REPORT IS JUDGED AGAINST.
    #
    # Half the findings on a report are not about the PDF at all - they are
    # about the order behind it. "Ordered but not on the report" is
    # roster.expected_products; every pacing number is roster.ordered_for.
    # A fix to either changes the answer as squarely as a fix to a rule does,
    # and nothing under checks/ moves when it lands, so SKyPAC would have gone
    # on failing for three cancelled products with the fix sitting in the file.
    #
    # The whole file, not picked functions. Those two read a dozen helpers
    # between them and picking by name is how the recheck.py hash was too
    # narrow by exactly the bug it was written for, twice.
    h.update(_roster_source())
    return h.hexdigest()[:16]


def _roster_source() -> bytes:
    from pathlib import Path
    try:
        return (Path(__file__).resolve().parent / "roster.py").read_bytes()
    except OSError:
        return b""


def _recheck_answers() -> str:
    """The source of the parts of recheck.py that decide what gets stored.

    `recheck` writes the answers onto the report; `sibling_for` is what the
    pair check compares against. A change to either changes what a stored
    report says, and has to reach the reports. The rest of the file - pacing,
    claims, when the sweep starts - does not.
    """
    import ast
    from pathlib import Path

    src_path = Path(__file__).resolve().parent / "recheck.py"
    try:
        src = src_path.read_text(encoding="utf-8")
        tree = ast.parse(src)
    except (OSError, SyntaxError):
        return ""
    out = []
    for node in tree.body:
        if (isinstance(node, ast.FunctionDef)
                and node.name in ("recheck", "sibling_for")):
            out.append(ast.get_source_segment(src, node) or "")
    return "\n".join(out)


def product_map_version() -> str:
    """Fingerprint of the code that turns the order export into order lines.

    The order list is not stored raw: every line item is mapped on the way in
    and only the answer is kept. So a fix to the mapping does nothing for the
    orders already loaded - and because the S3 sync skips a file whose ETag has
    not changed, "nothing" can mean forever.

    That is not theoretical. "TikTok Display & Video Ads" was being read as
    Video, and after the fix shipped the board still said a live TikTok order
    was a Video order, because the export had not changed so it was never read
    again. This makes the mapping code part of what "unchanged" means.
    """
    import hashlib
    from pathlib import Path

    here = Path(__file__).resolve().parent
    # EVERY FILE WHOSE OUTPUT IS BAKED INTO AN ORDER LINE.
    #
    # This started as just the product mapping, which was too narrow by exactly
    # the bug it was written for. orders_io.py decides which rows survive the
    # import at all - it holds the rule that a live line item rescues an order
    # whose header says "IO Pending Launch" - and a fix there was reaching the
    # loaded orders no more than a mapping fix was. Order 55216 was fixed and
    # kept failing for that reason.
    parts = [here / "checks" / "products.py", here / "orders_io.py",
             here / "roster.py"]
    h = hashlib.sha256()
    for src in parts:
        try:
            h.update(src.name.encode())
            h.update(src.read_bytes())
        except OSError:
            return ""
    return h.hexdigest()[:16]


_FINGERPRINT: str | None = None


def rules_version() -> str:
    """Cached: the source does not change while the process is running."""
    global _FINGERPRINT
    if _FINGERPRINT is None:
        _FINGERPRINT = rules_fingerprint()
    return _FINGERPRINT
