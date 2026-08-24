"""
Brand assets, owned in code rather than only on disk.

The favicon is served from `static/`, but the <link> tag uses a data URI built
from the same bytes. That removes a whole class of failure: a missing static
directory, a route that never got hit, a browser caching a 404 from before the
file existed. The icon is about 1KB - cheaper to inline than to debug.

`static/favicon.svg` stays the editable source. This module reads it at import
and falls back to an embedded copy only if the file cannot be parsed, so there
is one place to change the artwork in the normal case.
"""
from __future__ import annotations
import base64
import os

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

# Vici 2025 palette. Named here so Python-side output (email digests, Slack
# attachments) can use the same values the stylesheet does.
ATLAS = "#002D58"
VELOCITY = "#0066B3"
PARCHMENT = "#FDFBF7"
INK = "#212121"
GOLD = "#F1B434"
CARDINAL = "#A6192E"
PLUM = "#78286E"
TEAL = "#4FD4E0"

_FALLBACK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#002D58"/>'
    '<path d="M18 15h18l10 10v24a2 2 0 0 1-2 2H18a2 2 0 0 1-2-2V17a2 2 0 0 1 2-2z" '
    'fill="#FDFBF7"/>'
    '<circle cx="43" cy="43" r="11" fill="#F1B434"/>'
    '<path d="M38.4 43.2l3.2 3.4 6-6.6" fill="none" stroke="#002D58" '
    'stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"/></svg>')


def _read(name: str) -> bytes | None:
    try:
        with open(os.path.join(STATIC_DIR, name), "rb") as f:
            return f.read()
    except Exception:
        return None


def _wellformed(blob: bytes | None) -> bytes | None:
    """SVG is XML, and a standalone SVG document is parsed strictly. One raw
    `&` in an attribute and the browser draws nothing and reports nothing - a
    clean 200 carrying a document no renderer accepts. A read that succeeds is
    not the test. Parsing is."""
    if not blob:
        return None
    try:
        import xml.etree.ElementTree as ET
        ET.fromstring(blob)
        return blob
    except Exception as exc:  # noqa: BLE001
        print(f"[brand] static/favicon.svg is not well-formed ({exc}) - "
              f"using the embedded copy", flush=True)
        return None


FAVICON_SVG: bytes = _wellformed(_read("favicon.svg")) or _FALLBACK_SVG.encode()

_DATA_URI = "data:image/svg+xml;base64," + base64.b64encode(FAVICON_SVG).decode()

# Inline data URI first, file second. A browser uses the first icon it can
# load, so this renders even when the static route is unreachable.
HEAD_TAGS = (f"<link rel='icon' href=\"{_DATA_URI}\" type='image/svg+xml'>"
             f"<link rel='alternate icon' href='/static/favicon.svg' type='image/svg+xml'>"
             f"<meta name='theme-color' content='{ATLAS}'>")
