"""Running the poppler tools without taking the site down with them.

Everything heavy this service does is a subprocess - pdftotext, pdftoppm,
pdfinfo - and the background sweep runs hundreds of them in a row. On a small
instance that is the whole CPU, so a page that needs one of its own waits
behind the sweep, and the dashboard looks hung when it is merely last in the
queue.

So work started by a background thread marks itself, and anything it spawns
runs at a lower priority than a request. `nice` rather than preexec_fn: the
same effect, no forking hook to get wrong, and it is visible in ps.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import threading

_local = threading.local()

NICE = shutil.which("nice")
NICENESS = "10"


def in_background() -> bool:
    return bool(getattr(_local, "on", False))


class background:
    """Mark this thread's work as background. Also drops the thread's own
    scheduling priority, which is what the Python-heavy parts (parsing a
    million-row export) need - nice on a subprocess does nothing for those."""

    def __enter__(self):
        _local.on = True
        try:
            os.nice(5)          # per-thread on Linux, and one way only
        except OSError:
            pass
        return self

    def __exit__(self, *exc):
        _local.on = False
        return False


def run(cmd: list[str], **kw):
    """subprocess.run, at low priority when a background thread asked for it."""
    if in_background() and NICE and cmd:
        cmd = [NICE, "-n", NICENESS] + list(cmd)
    return subprocess.run(cmd, **kw)
