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
BUILD = "2026.08.25-18"
BUILD_NOTES = ("A product whose flight ended before the report's month is no "
               "longer expected on it. An \"Only started\" button on the cycle "
               "board hides the partners nothing has arrived for. Sign-off "
               "keeps its three verdicts on one line and says what Waive does.")
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
