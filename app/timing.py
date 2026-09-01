"""What the server thinks each request cost.

WRITTEN BECAUSE THE ONLY EVIDENCE WAS "IT TAKES A MINUTE".

The board was timed locally against the real export - 2,427 order lines, 900
reports - and came back in 1.4 seconds, on 26 queries. The same pages in
production felt like a minute. Every guess at the difference (the JSON the
board pulls, the query count, the re-check sweep) was a guess, and two of them
were shipped as fixes before anybody had measured the thing they were meant to
fix.

So this records what the server itself saw: how long each request took inside
the process, how many queries it ran, and which worker answered it. Then the
comparison that matters can actually be made -

  * server says 0.4s and the browser waited 60s -> the time is not being spent
    in this code. It is a cold start, a queue in front of the app, or the box
    being paged back in.
  * server says 55s -> it is this code, and the query count says which kind.

AND HOW LONG THIS WORKER HAS BEEN UP, which is the single most useful number on
the page. The import of a 148 MB export from an upload peaks at a gigabyte on
a box with 512 MB. A worker killed for that comes back cold, and a cold start
costs the next person the better part of a minute - forever, if it keeps
happening. Uptime measured in seconds when somebody is complaining about a
slow page is the whole answer, and nothing else on the dashboard shows it.

The ring is per worker and in memory: there are two workers, so a page here
shows the requests THIS one answered. The boot history is in the database
instead, because the event worth seeing is the restart, and a restart is
exactly when in-memory evidence is lost.
"""
from __future__ import annotations

import os
import threading
import time
from collections import deque
from contextvars import ContextVar

PID = os.getpid()
BOOTED_AT = time.time()

# Roughly an hour of one person clicking around. Small enough to be free.
RECENT: deque = deque(maxlen=200)
_LOCK = threading.Lock()

# Whether the query listener is already attached. It hangs off the SQLAlchemy
# Engine CLASS so a rebuilt engine is still counted, and that makes registering
# it twice a real possibility - the tests reload main.py - which would double
# every count on the page. This module is never reloaded, so the flag lives
# here rather than there.
LISTENING = False

# A MUTABLE HOLDER, NOT A NUMBER.
#
# The route runs in a threadpool thread, and a context variable set in there is
# set in a COPY of the context - the middleware that started the clock never
# sees it. So the counter is a list the middleware puts in place and the query
# listener mutates: same object, every context, whichever thread.
_COUNTER: ContextVar[list | None] = ContextVar("qa_query_counter", default=None)


def start_counting() -> dict:
    box = {"queries": 0, "db": 0.0, "phases": {}}
    _COUNTER.set(box)
    return box


def note_query(seconds: float) -> None:
    box = _COUNTER.get()
    if box is not None:
        box["queries"] += 1
        box["db"] += seconds


def mark(name: str, seconds: float) -> None:
    """Time spent in one named part of the request.

    WHICH HALF, is the question this answers. A page that takes four seconds
    with two tenths of it in the database is either building its rows slowly or
    rendering slowly, and those have nothing to do with each other. Guessing
    between them from a total is how the last two builds got aimed at the wrong
    thing.
    """
    box = _COUNTER.get()
    if box is not None:
        box["phases"][name] = round(box["phases"].get(name, 0.0) + seconds, 3)


def record(path: str, method: str, status: int, seconds: float,
           queries: int, db_seconds: float,
           phases: dict | None = None) -> None:
    with _LOCK:
        RECENT.append({"at": time.time(), "path": path[:120], "method": method,
                       "status": status, "seconds": round(seconds, 3),
                       "queries": queries,
                       "db_seconds": round(db_seconds, 3),
                       "phases": dict(phases or {})})


def recent(limit: int = 60) -> list[dict]:
    with _LOCK:
        rows = list(RECENT)
    rows.reverse()
    return rows[:limit]


def uptime_seconds() -> float:
    return time.time() - BOOTED_AT


def rss_mb() -> float | None:
    """This process's resident memory, in MB. None where /proc is not there."""
    try:
        with open("/proc/self/status", "r") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        pass
    return None


def memory_limit_mb() -> float | None:
    """What the container is allowed, off cgroup v2 then v1.

    Worth having beside the RSS: 380 MB of 512 is a box about to be killed and
    380 MB on its own is a number nobody can read.
    """
    for path in ("/sys/fs/cgroup/memory.max",
                 "/sys/fs/cgroup/memory/memory.limit_in_bytes"):
        try:
            raw = open(path).read().strip()
        except OSError:
            continue
        if raw in ("", "max"):
            continue
        try:
            val = int(raw)
        except ValueError:
            continue
        # An unset v1 limit is a very large sentinel, not a real ceiling.
        if val <= 0 or val > 64 * 1024 ** 3:
            continue
        return round(val / 1048576, 1)
    return None


def load_average() -> tuple[float, float, float] | None:
    try:
        return os.getloadavg()
    except (OSError, AttributeError):
        return None


def summary() -> dict:
    rows = recent(200)
    served = [r for r in rows if r["path"] not in ("/healthz",)]
    slowest = max((r["seconds"] for r in served), default=0.0)
    median = 0.0
    if served:
        vals = sorted(r["seconds"] for r in served)
        median = vals[len(vals) // 2]
    return {"pid": PID, "uptime_seconds": round(uptime_seconds(), 1),
            "rss_mb": rss_mb(), "memory_limit_mb": memory_limit_mb(),
            "requests_seen": len(served), "median_seconds": median,
            "slowest_seconds": slowest, "load": load_average()}


def verdict(boots_last_hour: int = 0) -> list[str]:
    """Plain sentences about what the numbers above mean.

    A table of milliseconds is not an answer to "why is it slow". These are the
    three readings that have an obvious next step behind them.
    """
    out: list[str] = []
    s = summary()
    up = s["uptime_seconds"]
    if up < 300:
        out.append(
            f"This worker started {int(up)} seconds ago. If pages were slow "
            "just before that, they were slow because the service was "
            "restarting - a cold start costs whoever asks first. Check the "
            "restart list below.")
    if boots_last_hour >= 4:
        out.append(
            f"The service has restarted {boots_last_hour} times in the last "
            "hour. That is not normal and it is the reason for the wait: "
            "every restart makes the next page load pay for the boot. It is "
            "almost always memory - something read a file too big for the box.")
    rss, cap = s["rss_mb"], s["memory_limit_mb"]
    if rss and cap and rss > cap * 0.85:
        out.append(
            f"This worker is holding {rss:.0f} MB of a {cap:.0f} MB ceiling. "
            "It is close enough that the next large job gets it killed.")
    load = s["load"]
    if load and load[0] >= 4:
        out.append(
            f"The box is carrying a load of {load[0]:.1f}, which means work is "
            "queuing rather than running. Worth knowing: inside a container "
            "this figure is usually the WHOLE MACHINE, including whatever else "
            "the host is running - so it can be high because of neighbors "
            "rather than because of this tool. The per-request seconds below "
            "are the ones that are definitely ours.")
    if s["requests_seen"] >= 5 and s["median_seconds"] < 2 and \
            s["slowest_seconds"] < 5:
        out.append(
            "Every request this worker has answered was quick. If the browser "
            "is waiting a lot longer than these numbers, the time is being "
            "spent before the request reaches this code, not inside it.")
    return out
