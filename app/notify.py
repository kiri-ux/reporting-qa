from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import httpx

from .config import settings

log = logging.getLogger("reportqa.notify")

SEV_EMOJI = {"fail": ":red_circle:", "warn": ":large_orange_circle:", "pass": ":large_green_circle:"}


def _lines(batch, comp) -> list[str]:
    out = [f"*{batch.market or 'Report batch'}* {batch.period} "
           f"— {len(batch.reports)} reports: "
           f"{batch.failed} failed, {batch.warned} warnings, {batch.clean} clean"]
    for r in sorted(batch.reports, key=lambda x: (x.severity != "fail", x.severity != "warn", x.client)):
        if r.severity == "pass":
            continue
        titles = "; ".join(f["title"] for f in r.findings if f["severity"] in ("fail", "warn"))
        who = " / ".join(x for x in (r.owner_buyer, r.owner_team) if x)
        out.append(f"{SEV_EMOJI.get(r.severity,'')} *{r.client}* {r.account_ids} — {titles}"
                   + (f"  _({who})_" if who else ""))
    if comp:
        if comp["missing"]:
            out.append(f":envelope_with_arrow: *{len(comp['missing'])} expected report(s) did not arrive*: "
                       + ", ".join(m["client"] for m in comp["missing"][:10]))
        if comp["lifetime_due"]:
            out.append(f":calendar: *{len(comp['lifetime_due'])} lifetime report(s) due* "
                       "(campaign ended this period): "
                       + ", ".join(f"{m['client']} (ended {m['ended']})" for m in comp["lifetime_due"][:10]))
    return out


def post_slack(batch, comp=None) -> bool:
    if not settings.notifications_enabled:
        return False
    if not settings.slack_webhook_url:
        return False
    text = "\n".join(_lines(batch, comp))
    try:
        r = httpx.post(settings.slack_webhook_url, json={"text": text}, timeout=20)
        return r.status_code < 300
    except Exception:
        return False


def _html(batch, comp) -> str:
    rows = []
    for r in sorted(batch.reports, key=lambda x: (x.severity != "fail", x.severity != "warn", x.client)):
        color = {"fail": "#A9382A", "warn": "#8A5F13", "pass": "#1F5F50"}[r.severity]
        items = "".join(
            f"<li><b>{f['title']}</b><br><span style='color:#555'>{f['detail']}</span></li>"
            for f in r.findings if f["severity"] in ("fail", "warn"))
        rows.append(
            f"<tr><td style='padding:8px 12px;border-bottom:1px solid #ddd'>"
            f"<b>{r.client}</b> <span style='color:#888;font-family:monospace'>{r.account_ids}</span>"
            f"<br><span style='color:#888;font-size:12px'>"
            f"{' / '.join(x for x in (r.owner_buyer, r.owner_team) if x)}</span></td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #ddd;color:{color}'>"
            f"<b>{r.severity.upper()}</b></td>"
            f"<td style='padding:8px 12px;border-bottom:1px solid #ddd'>"
            f"<ul style='margin:0;padding-left:18px'>{items or '<li>No issues</li>'}</ul></td></tr>")
    extra = ""
    if comp and comp["missing"]:
        extra += "<h3>Expected but not received</h3><ul>" + "".join(
            f"<li>{m['client']} <span style='color:#888'>{m['accounts']}</span>"
            f" — {m['buyer'] or ''} {m['team'] or ''}</li>" for m in comp["missing"]) + "</ul>"
    if comp and comp["lifetime_due"]:
        extra += "<h3>Lifetime reports due</h3><ul>" + "".join(
            f"<li>{m['client']} — campaign ended {m['ended']}</li>"
            for m in comp["lifetime_due"]) + "</ul>"
    return f"""<html><body style="font-family:Arial,Helvetica,sans-serif;color:#161D28">
<h2 style="margin-bottom:4px">{batch.market or 'Report batch'} — {batch.period}</h2>
<p style="color:#555;margin-top:0">{len(batch.reports)} reports checked ·
<b style="color:#A9382A">{batch.failed} failed</b> ·
<b style="color:#8A5F13">{batch.warned} warnings</b> ·
<b style="color:#1F5F50">{batch.clean} clean</b></p>
<table style="border-collapse:collapse;width:100%;font-size:14px">{''.join(rows)}</table>
{extra}</body></html>"""


def send_digest(batch, comp=None, extra_to: list[str] | None = None) -> bool:
    if not settings.notifications_enabled:
        return False
    if not (settings.smtp_host and settings.digest_from):
        return False
    wanted = list(dict.fromkeys(settings.digest_recipients + (extra_to or [])))
    to = [a for a in wanted if settings.is_internal(a)]
    blocked = [a for a in wanted if a not in to]
    if blocked:
        # Loud, because silently dropping a recipient is its own kind of bug -
        # someone will wonder why they never got the digest.
        log.warning("digest: refused %d external recipient(s): %s. "
                    "INTERNAL_DOMAINS is %s.",
                    len(blocked), ", ".join(blocked), settings.internal_domains)
    if not to:
        return False
    msg = EmailMessage()
    msg["Subject"] = (f"[Report QA] {batch.market or 'batch'} {batch.period} — "
                      f"{batch.failed} failed, {batch.warned} warnings")
    msg["From"] = settings.digest_from
    msg["To"] = ", ".join(to)
    msg.set_content("HTML report attached. See the dashboard for detail.")
    msg.add_alternative(_html(batch, comp), subtype="html")
    try:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as s:
            s.starttls()
            if settings.smtp_user:
                s.login(settings.smtp_user, settings.smtp_password)
            s.send_message(msg)
        return True
    except Exception:
        return False
