"""Package a partner group's finished cycle and share it.

When every report a group owes is good to go, its PDFs are zipped and pushed
to wherever that partner takes delivery - a Google shared drive for most, a
Dropbox folder for 7 Mountains - and the resulting anyone-with-the-link URL is
recorded so it can be handed over.

Both uploaders are optional. With no credentials configured the zip is still
built and kept locally for download, so the cycle board works end to end
before anyone touches a cloud console.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import zipfile
from pathlib import Path

from sqlalchemy.orm import Session

from .board import Expected, GroupRow, by_group
from .config import settings
from .db import Delivery

log = logging.getLogger("reportqa.delivery")


def _safe(name: str, limit: int = 120) -> str:
    return (re.sub(r"[^A-Za-z0-9._ &,()-]", "_", name).strip() or "untitled")[:limit]


def build_zip(group: GroupRow, period: str, out_dir: Path) -> tuple[Path, int]:
    """One zip per group, foldered by market.

    Partners open these to pull one client's PDF, so the inside is organised
    the way they think - market, then the file - rather than as a flat dump of
    200 similarly-named PDFs.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    label = dt.date.fromisoformat(period + "-01").strftime("%Y-%m %B")
    path = out_dir / f"{_safe(group.group)} - {label}.zip"

    n = 0
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for e in group.expected:
            r = e.report
            if not r or not r.stored_path:
                continue
            src = Path(r.stored_path)
            if not src.exists():
                log.warning("missing file for %s / %s: %s", e.market, e.client, src)
                continue
            suffix = " - Lifetime" if e.kind == "lifetime" else ""
            arc = f"{_safe(e.market)}/{_safe(e.client)}{suffix}.pdf"
            z.write(src, arc)
            n += 1
    return path, n


# ---------------------------------------------------------------- Google Drive
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


def _drive_credentials():
    """OAuth first, service account key second.

    Most Workspace organisations now enforce the org policy
    `iam.disableServiceAccountKeyCreation`, which does not restrict what a key
    can do - it stops the key existing at all. There is no code change that
    gets around that, so the primary path is a refresh token from a person who
    authorised once. Files uploaded into a SHARED drive are owned by the drive
    rather than by that person, so nothing is orphaned when they leave.
    """
    mode = settings.google_auth_mode
    if mode == "oauth":
        from google.oauth2.credentials import Credentials
        missing = [n for n, v in (("GOOGLE_CLIENT_ID", settings.google_client_id),
                                  ("GOOGLE_CLIENT_SECRET", settings.google_client_secret))
                   if not v.strip()]
        if missing:
            raise RuntimeError(f"{' and '.join(missing)} not set. The refresh token "
                               f"cannot be exchanged without them.")
        return Credentials(
            token=None,
            refresh_token=settings.google_refresh_token.strip(),
            client_id=settings.google_client_id.strip(),
            client_secret=settings.google_client_secret.strip(),
            token_uri="https://oauth2.googleapis.com/token",
            scopes=[DRIVE_SCOPE])
    if mode == "key":
        from google.oauth2 import service_account
        return service_account.Credentials.from_service_account_info(
            settings.google_credentials(), scopes=[DRIVE_SCOPE])
    raise RuntimeError(
        "Google Drive is not configured. Set GOOGLE_CLIENT_ID, "
        "GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN (or, if your org allows "
        "service account keys, GOOGLE_SERVICE_ACCOUNT_JSON).")


def upload_drive(path: Path, folder_name: str) -> tuple[str, str]:
    """Upload into a Google shared drive and return (url, message).

    A folder per cycle inside the configured parent, so a partner coming back
    next month finds this month beside last month rather than a pile of zips.
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    svc = build("drive", "v3", credentials=_drive_credentials(),
                cache_discovery=False)

    parent = settings.drive_parent_folder_id.strip()
    if not parent:
        raise RuntimeError("DRIVE_PARENT_FOLDER_ID is not set.")

    # supportsAllDrives / includeItemsFromAllDrives are required on every call
    # that touches a shared drive. Without them the API quietly behaves as if
    # the folder does not exist, which reads as a permissions problem.
    q = (f"name = '{folder_name}' and '{parent}' in parents "
         f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    found = svc.files().list(q=q, fields="files(id)", supportsAllDrives=True,
                             includeItemsFromAllDrives=True,
                             corpora="allDrives").execute().get("files", [])
    if found:
        folder_id = found[0]["id"]
    else:
        folder_id = svc.files().create(
            body={"name": folder_name, "parents": [parent],
                  "mimeType": "application/vnd.google-apps.folder"},
            fields="id", supportsAllDrives=True).execute()["id"]

    media = MediaFileUpload(str(path), mimetype="application/zip", resumable=True)
    f = svc.files().create(body={"name": path.name, "parents": [folder_id]},
                           media_body=media, fields="id, webViewLink",
                           supportsAllDrives=True).execute()

    svc.permissions().create(fileId=f["id"], supportsAllDrives=True,
                             body={"role": "reader", "type": "anyone"}).execute()
    return f.get("webViewLink", f"https://drive.google.com/file/d/{f['id']}/view"), \
        "Uploaded to Google Drive, link sharing on."


# ---------------------------------------------------------------- Dropbox
def upload_dropbox(path: Path, folder_name: str) -> tuple[str, str]:
    """Upload to Dropbox and return (shared link, message)."""
    import dropbox
    from dropbox.files import WriteMode
    from dropbox.sharing import SharedLinkSettings, RequestedVisibility

    app_key = settings.dropbox_app_key.strip()
    app_secret = settings.dropbox_app_secret.strip()
    refresh = settings.dropbox_refresh_token.strip()
    if not (app_key and app_secret and refresh):
        raise RuntimeError("Dropbox needs DROPBOX_APP_KEY, DROPBOX_APP_SECRET "
                           "and DROPBOX_REFRESH_TOKEN.")
    # A refresh token, not a raw access token: Dropbox access tokens expire
    # after four hours, so anything issued by hand is dead by the next cycle.
    dbx = dropbox.Dropbox(app_key=app_key, app_secret=app_secret,
                          oauth2_refresh_token=refresh)

    base = (settings.dropbox_folder.strip().rstrip("/") or "")
    dest = f"{base}/{folder_name}/{path.name}"
    if not dest.startswith("/"):
        dest = "/" + dest

    # 150 MB is Dropbox's single-request ceiling; above it the upload has to be
    # chunked into a session.
    data = path.read_bytes()
    if len(data) <= 140 * 1024 * 1024:
        dbx.files_upload(data, dest, mode=WriteMode("overwrite"))
    else:
        chunk = 100 * 1024 * 1024
        sess = dbx.files_upload_session_start(data[:chunk])
        cursor = dropbox.files.UploadSessionCursor(session_id=sess.session_id,
                                                   offset=chunk)
        while cursor.offset < len(data) - chunk:
            dbx.files_upload_session_append_v2(data[cursor.offset:cursor.offset + chunk],
                                               cursor)
            cursor.offset += chunk
        dbx.files_upload_session_finish(
            data[cursor.offset:], cursor,
            dropbox.files.CommitInfo(path=dest, mode=WriteMode("overwrite")))

    try:
        link = dbx.sharing_create_shared_link_with_settings(
            dest, SharedLinkSettings(
                requested_visibility=RequestedVisibility.public)).url
    except Exception:
        # Already shared: Dropbox refuses to mint a second link, so read the
        # existing one rather than treating this as a failure.
        links = dbx.sharing_list_shared_links(path=dest, direct_only=True).links
        if not links:
            raise
        link = links[0].url
    return link, "Uploaded to Dropbox, public link created."


# ---------------------------------------------------------------- orchestration
def deliver(db: Session, period: str, group_name: str, *,
            force: bool = False) -> Delivery:
    """Package one group and push it wherever that partner takes delivery."""
    groups = {g.group: g for g in by_group(db, period)}
    group = groups.get(group_name)
    if group is None:
        rec = Delivery(period=period, group=group_name, ok=False,
                       message="No reports expected for that partner this cycle.")
        db.add(rec); db.commit(); return rec

    if not group.ready and not force:
        c = group.counts
        blocking = ", ".join(f"{v} {k.replace('_', ' ')}"
                             for k, v in c.items() if v and k != "ready")
        rec = Delivery(period=period, group=group_name, ok=False,
                       message=f"Not ready: {blocking}.")
        db.add(rec); db.commit(); return rec

    target = group.target or settings.delivery_target
    out_dir = settings.data_dir / "deliveries" / period
    try:
        path, n = build_zip(group, period, out_dir)
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        rec = Delivery(period=period, group=group_name, target=target, ok=False,
                       message=f"Could not build the zip: {type(exc).__name__}: {exc}")
        db.add(rec); db.commit(); return rec

    if n == 0:
        rec = Delivery(period=period, group=group_name, target=target, ok=False,
                       message="Every report is marked ready but none has a stored PDF.")
        db.add(rec); db.commit(); return rec

    label = dt.date.fromisoformat(period + "-01").strftime("%Y-%m %B")
    url, message = "", "Zip built and kept here for download."
    if target in ("drive", "dropbox"):
        try:
            url, message = (upload_drive if target == "drive"
                            else upload_dropbox)(path, label)
        except ModuleNotFoundError as exc:
            db.rollback()
            rec = Delivery(period=period, group=group_name, target=target, reports=n,
                           bytes=path.stat().st_size, local_path=str(path), ok=False,
                           message=f"Zip is ready to download, but the {target} "
                                   f"library is not installed on this deploy "
                                   f"({exc.name}). Redeploy so requirements.txt "
                                   f"is picked up.")
            db.add(rec); db.commit(); return rec
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            db.rollback()
            rec = Delivery(period=period, group=group_name, target=target,
                           reports=n, bytes=path.stat().st_size,
                           local_path=str(path), ok=False,
                           message=f"Zip built, but the upload failed: "
                                   f"{type(exc).__name__}: {exc}")
            db.add(rec); db.commit(); return rec

    rec = Delivery(period=period, group=group_name, target=target, reports=n,
                   bytes=path.stat().st_size, local_path=str(path),
                   share_url=url, ok=True, message=message)
    db.add(rec); db.commit()
    return rec


def latest_deliveries(db: Session, period: str) -> dict[str, Delivery]:
    from sqlalchemy import select as _select
    out: dict[str, Delivery] = {}
    for d in db.scalars(_select(Delivery).where(Delivery.period == period)
                        .order_by(Delivery.id)).all():
        out[d.group] = d              # last one per group wins
    return out
