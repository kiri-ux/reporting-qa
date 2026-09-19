"""One partner's reports, on a link the buyer can open.

WHY A SEPARATE LINK AT ALL.

The buyer's flags are on the report and on the board, and both of those are
the reporting team's screens - filtered to the work that is open, laid out
around a sign-off the buyer does not make, and behind the site password.
Sending a buyer to /cycle meant sending them somewhere they had to be told
how to read, so what actually got sent was a screenshot or a paragraph typed
out by hand, per partner, every month.

WHAT THE TOKEN IS. The partner's name and a signature over it, so the link
carries who it is for and nothing has to be stored or cleaned up. A link is
good until the secret changes, which is how a link somebody put in a calendar
invite goes on working next month.

IT IS THE ONLY THING GUARDING THAT PARTNER'S LIST, so the secret matters.
BUYER_LINK_SECRET is what signs them; with it unset the inbound secret signs
them instead, and an inbound secret left at its default makes these links
guessable. Nothing else is reachable from one: the page is read-only, it
serves that partner's PDFs and nothing else's, and there is no path from it
back into the tool.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

from .config import settings

# Long enough that guessing is not a thing, short enough that the link fits in
# a message without wrapping.
SIG_LEN = 20


def _secret() -> bytes:
    raw = (settings.buyer_link_secret or settings.inbound_secret or "").strip()
    return ("report-qa-buyer:" + raw).encode()


def _sign(group: str) -> str:
    mac = hmac.new(_secret(), (group or "").encode(), hashlib.sha256)
    return base64.urlsafe_b64encode(mac.digest()).decode().rstrip("=")[:SIG_LEN]


def token_for(group: str) -> str:
    """The token for one partner. Stable as long as the secret is."""
    name = base64.urlsafe_b64encode((group or "").encode()).decode().rstrip("=")
    return f"{name}.{_sign(group)}"


def group_of(token: str) -> str:
    """Which partner this token is for, or "" if it was not signed here.

    compare_digest, so a token half right does not take measurably longer to
    be refused than one that is wrong from the first character.
    """
    name, _, sig = (token or "").partition(".")
    if not name or not sig:
        return ""
    try:
        group = base64.urlsafe_b64decode(name + "=" * (-len(name) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return ""
    return group if hmac.compare_digest(sig, _sign(group)) else ""


def url_for(base: str, group: str) -> str:
    """The whole link, ready to paste into a message."""
    return f"{(base or '').rstrip('/')}/buyer/{token_for(group)}"
