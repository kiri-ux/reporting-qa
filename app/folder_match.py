"""Match a partner name to the folder that already exists for it.

The shared drive has ~130 folders named by hand over ten years, and they do
not match the roster exactly: "Results Media Solutions Chico" against a folder
called "Results Radio Chico", "Stephens Media Group Merced, CA (previously
Mapleton)" against "Stephens Merced".

The dangerous failure is not missing a match - that just creates a new folder.
It is matching the WRONG one, which files a client's reports in another
client's folder where nobody looks for them. So the rules below are built to
refuse rather than guess:

  * the LAST token has to line up. It is almost always the thing that
    distinguishes siblings - Knoxville from Wichita, Boise from Reno - and
    without that rule "Summit Media Knoxville" happily matches "Summit Media
    Wichita" on two shared tokens.
  * at least one more token has to be shared, so a bare city name cannot pull
    two unrelated partners together.
  * if two folders match equally well, none of them is used. An ambiguous
    match is worse than no match.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

# Words that carry no identity. "Summit Media" and "Summit Group" are not
# distinguished by "media" or "group", so they must not count as the shared
# token that authorises a match.
NOISE = {
    "media", "group", "marketing", "advertising", "digital", "broadcasting",
    "communications", "agency", "solutions", "company", "inc", "llc", "co",
    "the", "and", "of", "reports", "report", "radio", "tv", "partners",
    "consulting", "creative", "services", "network", "networks", "corp",
}


# Two-letter state codes. A trailing "OH" is not what tells Lima from Toledo,
# and treating it as the distinguishing token matched them to each other.
STATES = {
    "al", "ak", "az", "ar", "ca", "co", "ct", "de", "fl", "ga", "hi", "id",
    "il", "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms",
    "mo", "mt", "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok",
    "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv",
    "wi", "wy", "dc",
}


def _tail_matches(pt: list[str], ft: list[str]) -> bool:
    """Do these two names end on the same distinguishing token?

    A state code is only noise when BOTH sides carry the SAME one. "Woof Boom
    Toledo, OH" and "Woof Boom Lima, OH" share the OH, so the real difference
    is one token further back. But "7 Mountains KY" and "7 Mountains PA" are
    told apart by nothing else - there the state IS the distinction, and
    skipping it matched two different partners to each other.
    """
    a, b = list(pt), list(ft)
    while a and b:
        la, lb = a[-1], b[-1]
        a_state, b_state = la in STATES, lb in STATES
        if a_state and b_state:
            if not _close(la, lb):
                return False          # KY against PA: that is the whole difference
            a.pop(); b.pop()          # same state, look further back
            continue
        if a_state:                   # one side spells the state out, the other does not
            a.pop()
            continue
        if b_state:
            b.pop()
            continue
        return _close(la, lb)
    return False


def tokens(name: str) -> list[str]:
    """Lowercase words, punctuation gone, parenthetical asides dropped.

    "(previously Mapleton)" is history, not identity, and leaving it in makes
    a folder look less like its partner than it really is.
    """
    name = re.sub(r"\([^)]*\)", " ", name or "")
    return [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]


def _significant(toks: list[str]) -> list[str]:
    out = [t for t in toks if t not in NOISE]
    return out or toks          # a name made entirely of noise still has to match itself


def _close(a: str, b: str) -> bool:
    """Same token, allowing for a typo or a missing letter.

    "Moxii" and "Moxi" are the same partner; "Chico" and "Chino" are not, so
    the threshold has to sit above a single-letter swap on a short word.
    """
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4:
        return False
    if abs(len(a) - len(b)) <= 1 and (a.startswith(b) or b.startswith(a)):
        return True
    return SequenceMatcher(None, a, b).ratio() >= 0.9


def score(partner: str, folder: str) -> int:
    """0 means no match. Higher is a better one."""
    pt, ft = tokens(partner), tokens(folder)
    if not pt or not ft:
        return 0
    if pt == ft:
        return 100

    # The distinguishing token is nearly always last, give or take a state code.
    if not _tail_matches(pt, ft):
        return 0

    ps, fs = set(_significant(pt)), set(_significant(ft))
    shared = {a for a in ps for b in fs if _close(a, b)}
    if len(shared) < 2 and not (len(ps) == 1 and len(fs) == 1):
        # A single shared token is not enough unless both names ARE that token.
        return 0
    # Prefer the folder that shares most and pads least.
    return 10 * len(shared) - abs(len(ps) - len(fs))


def best(partner: str, folders: dict[str, str]) -> tuple[str | None, str]:
    """(folder name, why). folders maps name -> id, but only names are read.

    Returns (None, reason) when nothing is safe to use, and the caller creates
    a folder named exactly after the partner instead.
    """
    if not folders:
        return None, "no folders to match against"
    key = {n.strip().lower(): n for n in folders}
    exact = key.get((partner or "").strip().lower())
    if exact:
        return exact, "exact name"

    ranked = sorted(((score(partner, n), n) for n in folders), reverse=True)
    top, name = ranked[0]
    if top <= 0:
        return None, "nothing close enough"
    if len(ranked) > 1 and ranked[1][0] == top:
        # Two folders fit equally. Picking either one risks filing a client's
        # reports where nobody will look for them.
        return None, f"ambiguous between {name!r} and {ranked[1][1]!r}"
    return name, f"matched {name!r}"
