"""The one thing this tool did not have: a clock.

WHY IT NEEDED ONE.

Everything that comes in from outside - the order export, the daily serve file,
the reporting breakout sheet - was read on the back of something else
happening. A batch of reports arriving, or somebody pressing the button. That
is fine during the pull, when reports land by the hundred and the sync runs
constantly. It is not fine on the 12th of the month, when nothing is arriving
at all and a reporter changing hands in the sheet reaches the board the next
time somebody happens to press a button.

So: a heartbeat. Every half hour it asks the same question the button asks, and
the answer is almost always "nothing has changed" - the order export is checked
by ETag, the serve file by ETag, the sheet by a checksum of its own contents.
An unchanged everything costs three small requests and no writes.

ONE WORKER, NOT BOTH. The sync claims itself in the database, so the second
worker's heartbeat finds it taken and goes back to sleep rather than running a
second import over the top of the first.
"""
from __future__ import annotations

import logging
import threading
import time

from .config import settings

log = logging.getLogger("report-qa")

_started = threading.Event()


def start() -> None:
    """Start the heartbeat, once per process."""
    if _started.is_set() or not settings.sync_every_minutes:
        return
    _started.set()

    def run():
        from .db import SessionLocal
        from .orders_s3 import sync as sync_orders

        # Long enough that a deploy's first requests are served before this
        # asks S3 for anything.
        time.sleep(90)
        while True:
            db = SessionLocal()
            try:
                sync_orders(db, trigger="clock")
            except Exception as exc:                         # noqa: BLE001
                # A heartbeat that dies on one bad answer is worse than no
                # heartbeat: it stops silently and everything goes stale.
                log.warning("scheduled sync skipped: %s", exc)
            finally:
                db.close()
            sync_links()
            time.sleep(max(settings.sync_every_minutes, 5) * 60)

    threading.Thread(target=run, name="report-qa-clock", daemon=True).start()


def sync_links() -> None:
    """Bring every packaged folder up to its signed-off reports.

    A report corrected after its partner was packaged sat in the tool while
    the client's link held the old file, until somebody pressed sync. This is
    that press, on the heartbeat. Packaged partners only and signed-off
    reports only - exactly what Sync all sends - so nothing reaches a folder
    that would not have reached it by hand.

    TWO WORKERS, ONE RUN. start_sync_all refuses while its job row says
    running; the jitter keeps both heartbeats from reading that row in the
    same instant after a deploy.
    """
    if not settings.auto_sync_links:
        return
    import random

    from .cycle import working_period
    from .db import SessionLocal
    from .delivery import start_sync_all

    time.sleep(random.uniform(0, 45))
    db = SessionLocal()
    try:
        start_sync_all(db, working_period())
    except Exception as exc:                                 # noqa: BLE001
        log.warning("scheduled link sync skipped: %s", exc)
        db.rollback()
    finally:
        db.close()
    log.info("scheduled sync every %s minutes", settings.sync_every_minutes)
