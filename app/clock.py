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
            time.sleep(max(settings.sync_every_minutes, 5) * 60)

    threading.Thread(target=run, name="report-qa-clock", daemon=True).start()
    log.info("scheduled sync every %s minutes", settings.sync_every_minutes)
