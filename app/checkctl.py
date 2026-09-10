"""Which checks are switched on, and whether the sweep is allowed to run.

WHY THIS EXISTS.

Every check ran on every report, always, and the only way to stop one was to
change the code. That is expensive twice over: the person who reads the
findings all day cannot do it, and doing it re-judges the whole board, because
the rules fingerprint is a hash of the checking code and a hash does not care
whether the edit mattered.

So both ends move here:

  * A check can be switched off by a person. Off means the rule does not run
    AND the findings it already wrote stop counting - no re-check needed, which
    is the whole point of a switch.
  * The fingerprint ignores a check that is off. Editing a rule nobody has
    switched on costs nothing, and the "every deploy re-reads seven hundred
    PDFs" problem stops being every deploy.
  * The sweep can be held. Held, it does not start on its own; the board still
    counts what is behind and there is a button to run it deliberately.

CACHED FOR FIFTEEN SECONDS, and read through the process-wide session rather
than a passed-in one. `open_findings` is a property on the model - it is
reached from template loops thousands of times a page and has no session to
hand. Fifteen seconds is how long the other gunicorn worker can go on believing
a switch that was just flipped, which is the same lag the board already has on
everything else a person changes.

FAILING MEANS EVERYTHING IS ON. A missing table, a database that is down, a
session that will not open - none of those are a reason to stop checking
reports, and a check that quietly stopped running because a query failed is the
worst outcome available.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger("reportqa.checks")

HOLD_KEY = "recheck_hold"
_TTL = 15.0
_cache: dict = {"at": 0.0, "off": frozenset(), "hold": False}


# --------------------------------------------------------------- reading it
def refresh() -> None:
    """Forget what was cached. Called by anything that writes a switch."""
    _cache["at"] = 0.0


def _load() -> None:
    if time.monotonic() - _cache["at"] < _TTL:
        return
    off: set[str] = set()
    hold = False
    try:
        from sqlalchemy import select

        from .db import AppSetting, CheckSetting, SessionLocal

        db = SessionLocal()
        try:
            for row in db.scalars(select(CheckSetting)):
                if not row.enabled:
                    off.add(row.name)
            got = db.scalar(select(AppSetting).where(AppSetting.key == HOLD_KEY))
            hold = bool(got and got.value == "1")
        finally:
            db.close()
    except Exception as exc:                                     # noqa: BLE001
        # Everything on. See the module docstring: a check that stopped running
        # because a query failed is worse than a slow page.
        log.debug("check settings unreadable, everything stays on: %s", exc)
        _cache.update(at=time.monotonic(), off=frozenset(), hold=False)
        return
    _cache.update(at=time.monotonic(), off=frozenset(off), hold=hold)


def switched_off() -> frozenset[str]:
    """The names of the checks somebody has turned off."""
    _load()
    return _cache["off"]


def is_on(name: str) -> bool:
    return name not in switched_off()


def held() -> bool:
    """Is the automatic re-check sweep on hold?"""
    _load()
    return bool(_cache["hold"])


# ------------------------------------------------- a finding to its check
def finding_is_off(f: dict) -> bool:
    """Was this finding written by a check that is now switched off?

    NEW FINDINGS SAY WHO WROTE THEM. Every finding is stamped with its check on
    the way out, so this is a dictionary lookup.

    OLD ONES DO NOT, and there are hundreds of thousands of them stored. For
    those the code is matched back to whichever check can emit it, read off the
    source once. Several checks share a code - "rule_error" comes from all of
    them - so a finding is only hidden when EVERY check that can write it is
    off. Hiding a finding a live check would still raise is the one mistake
    here that costs something.
    """
    off = switched_off()
    if not off:
        return False
    who = f.get("check")
    if who:
        return who in off
    owners = code_owners().get(f.get("code") or "")
    return bool(owners) and owners <= off


_OWNERS: dict[str, frozenset[str]] | None = None


def code_owners() -> dict[str, frozenset[str]]:
    """{finding code: the checks that can write it}, read off the source.

    Static, not runtime: the answer has to cover codes no report has raised
    this month. Every code is a literal first argument to `_f`, so the call
    sites inside each check function are all it takes - and if the shape ever
    stops being a literal, that code simply has no owner and its findings are
    never hidden, which is the safe direction.
    """
    global _OWNERS
    if _OWNERS is not None:
        return _OWNERS
    import ast
    from collections import defaultdict
    from pathlib import Path

    found: dict[str, set[str]] = defaultdict(set)
    here = Path(__file__).resolve().parent / "checks"
    try:
        names = sorted(p for p in here.glob("*.py"))
    except OSError:                                              # noqa: BLE001
        names = []
    for path in names:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not (isinstance(node, ast.FunctionDef)
                    and node.name.startswith("check_")):
                continue
            for call in ast.walk(node):
                if not (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id == "_f" and call.args):
                    continue
                first = call.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    found[first.value].add(node.name)
    _OWNERS = {k: frozenset(v) for k, v in found.items()}
    return _OWNERS


# --------------------------------------------------------------- writing it
def set_check(db, name: str, on: bool, who: str = "", note: str = "") -> None:
    """Turn one check on or off."""
    import datetime as dt

    from sqlalchemy import select

    from .db import CheckSetting

    from .version import forget_fingerprint, rules_version

    was = rules_version()
    row = db.scalar(select(CheckSetting).where(CheckSetting.name == name))
    if row is None:
        row = CheckSetting(name=name)
        db.add(row)
    row.enabled = bool(on)
    row.changed_by = who or ""
    row.note = note or ""
    row.changed_at = dt.datetime.utcnow()
    db.commit()
    refresh()
    forget_fingerprint()
    now = rules_version()
    if on or was == now:
        # COMING BACK ON COSTS A RE-CHECK, and it should. That rule did not run
        # on anything judged while it was off, so those reports have not been
        # asked the question at all - which is the one case where re-reading
        # seven hundred PDFs is the honest answer.
        return
    # GOING OFF COSTS NOTHING. The hash moved because that rule's source is no
    # longer part of it, but no stored answer changes: a finding from a check
    # that is off stops counting the moment it is off, read at display time.
    # So the reports are stamped with the new hash where they stand, and the
    # sweep has nothing to do.
    from sqlalchemy import update

    from .db import Report

    db.execute(update(Report).where(Report.rules_version == was)
               .values(rules_version=now))
    db.commit()


def set_hold(db, on: bool, who: str = "") -> None:
    """Hold the automatic sweep, or let it go again."""
    import datetime as dt

    from sqlalchemy import select

    from .db import AppSetting

    row = db.scalar(select(AppSetting).where(AppSetting.key == HOLD_KEY))
    if row is None:
        row = AppSetting(key=HOLD_KEY)
        db.add(row)
    row.value = "1" if on else ""
    row.changed_by = who or ""
    row.changed_at = dt.datetime.utcnow()
    db.commit()
    refresh()
