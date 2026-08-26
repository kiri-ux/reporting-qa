"""Package a partner group's finished cycle and share it.

When every report a market owes is good to go, its PDFs are filed into the
tree the team already keeps - `<parent>/<Market>/<cycle>/` - and the CYCLE
FOLDER is shared by link. Google Drive for most markets, Dropbox for the 7
Mountains ones.

A folder rather than a zip: a partner opening it sees the client names, can
take the one report they need, or copy the whole folder into their own drive.
A zip makes them download everything first.

Both uploaders are optional. With no credentials configured a zip is built and
kept locally for download instead, so the cycle board works end to end before
anyone touches a cloud console.
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import zipfile
from pathlib import Path

from sqlalchemy.orm import Session

from .board import Expected, GroupRow, by_group
from .folder_match import best as pick_folder
from .config import settings
from .db import Delivery

log = logging.getLogger("reportqa.delivery")


def _safe(name: str, limit: int = 120) -> str:
    return (re.sub(r"[^A-Za-z0-9._ &,()-]", "_", name).strip() or "untitled")[:limit]


def report_filename(e) -> str:
    """What a report is called when it leaves here.

    ITS OWN NAME. This used to build one from the client - "Elmira Downtown
    Development.pdf" - so what the partner opened was not what the report page
    said it was called, and neither carried the month or the order id that
    everybody downstream files by. The report already has a good name; the only
    work left is stripping a browser's "(1)" and making sure it is a .pdf.
    """
    from .checks.parser import DUPLICATE_SUFFIX

    r = getattr(e, "report", None) or e
    raw = (getattr(r, "filename", "") or "").strip()
    stem, dot, ext = raw.rpartition(".")
    if not dot:
        stem, ext = raw, "pdf"
    stem = DUPLICATE_SUFFIX.sub("", stem).strip()
    if not stem:
        # No name stored at all - build the one the rest of the system uses.
        month = ""
        period = getattr(r, "period", "") or ""
        if period:
            try:
                month = dt.date.fromisoformat(period + "-01").strftime("%B %Y") + "_"
            except ValueError:
                month = ""
        ids = (getattr(r, "account_ids", "") or "").replace(",", " ").split()
        stem = f"{month}{getattr(e, 'client', '') or 'report'}"
        if ids:
            stem += " " + " ".join(ids)
    if getattr(e, "kind", "") == "lifetime" and "lifetime" not in stem.lower():
        stem += " - Lifetime"
    return f"{_safe(stem)}.{(ext or 'pdf').lower()}"


def build_zip(group: GroupRow, period: str, out_dir: Path) -> tuple[Path, int]:
    """One zip per group, foldered by market.

    Partners open these to pull one client's PDF, so the inside is organized
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
            arc = f"{_safe(e.market)}/{report_filename(e)}"
            z.write(src, arc)
            n += 1
    return path, n


# ---------------------------------------------------------------- Google Drive
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"


def _drive_credentials():
    """OAuth first, service account key second.

    Most Workspace organizations now enforce the org policy
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


def _list_folders(svc, parent: str) -> dict[str, str]:
    """Every folder under `parent`, name -> id."""
    q = (f"'{parent}' in parents and mimeType = "
         f"'application/vnd.google-apps.folder' and trashed = false")
    out, token = {}, None
    while True:
        kw = dict(q=q, fields="nextPageToken, files(id, name)", pageSize=1000,
                  supportsAllDrives=True, includeItemsFromAllDrives=True,
                  corpora="allDrives")
        if token:
            kw["pageToken"] = token
        page = svc.files().list(**kw).execute()
        for f in page.get("files", []):
            out[f["name"]] = f["id"]
        token = page.get("nextPageToken")
        if not token:
            break
    return out


def _has_files(svc, folder_id: str) -> bool:
    q = (f"'{folder_id}' in parents and trashed = false and "
         f"mimeType != 'application/vnd.google-apps.folder'")
    return bool(svc.files().list(q=q, fields="files(id)", pageSize=1,
                                 supportsAllDrives=True,
                                 includeItemsFromAllDrives=True,
                                 corpora="allDrives").execute().get("files"))


def _drive_folder(svc, name: str, parent: str) -> str:
    """Find a folder by name under `parent`, or make one.

    Matched case-insensitively against what is already there, because these
    folders are maintained by hand and "7 Mountains PA Selinsgrove" against
    "7 Mountains Pa Selinsgrove" would otherwise quietly create a second one
    beside the real thing.

    supportsAllDrives / includeItemsFromAllDrives are required on every call
    that touches a shared drive. Without them the API behaves as if the folder
    does not exist, which reads as a permissions problem.
    """
    q = (f"'{parent}' in parents and mimeType = "
         f"'application/vnd.google-apps.folder' and trashed = false")
    token, found = None, None
    while True:
        kw = dict(q=q, fields="nextPageToken, files(id, name)", pageSize=1000,
                  supportsAllDrives=True, includeItemsFromAllDrives=True,
                  corpora="allDrives")
        if token:
            kw["pageToken"] = token
        page = svc.files().list(**kw).execute()
        for f in page.get("files", []):
            if f["name"].strip().lower() == name.strip().lower():
                found = f["id"]
                break
        token = page.get("nextPageToken")
        if found or not token:
            break
    if found:
        return found
    return svc.files().create(
        body={"name": name, "parents": [parent],
              "mimeType": "application/vnd.google-apps.folder"},
        fields="id", supportsAllDrives=True).execute()["id"]


def upload_drive_folder(group, period: str, cycle_label: str) -> tuple[str, str, int]:
    """Put this market's reports where the team already keeps them.

    The shared drive is already organized as
    `01_Reporting Markets / <Market> / ...`, maintained by hand for years, so
    the tool files into that rather than inventing a parallel tree next to it.
    A cycle folder goes inside the market folder and the PDFs go inside that.

    The CYCLE FOLDER is what gets shared, not a zip. A partner opening a folder
    can see the client names, take the one they want, or copy the whole folder
    into their own drive - none of which a zip lets them do without downloading
    it first.
    """
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    svc = build("drive", "v3", credentials=_drive_credentials(),
                cache_discovery=False)
    parent = settings.drive_parent_folder_id.strip()
    if not parent:
        raise RuntimeError("DRIVE_PARENT_FOLDER_ID is not set. It should be the "
                           "id of the folder that holds one folder per market.")

    n = 0
    market_folders: dict[str, str] = {}
    cycle_folders: dict[str, str] = {}
    for e in group.expected:
        r = e.report
        if not r or not r.stored_path or not Path(r.stored_path).exists():
            log.warning("no stored file for %s / %s", e.market, e.client)
            continue
        if e.market not in market_folders:
            # MATCH THE FOLDER THAT IS ALREADY THERE.
            #
            # The drive's folders were named by hand over ten years and do not
            # match the roster exactly - "Results Media Solutions Chico" lives
            # in "Results Radio Chico". Matching refuses rather than guesses,
            # so an unmatched partner gets a new folder under its own name
            # instead of its reports landing in a sibling's.
            existing = _list_folders(svc, parent)
            hit, why = pick_folder(e.market, existing)
            log.info("drive: %s -> %s", e.market, why)
            market_folders[e.market] = (existing[hit] if hit
                                        else _drive_folder(svc, e.market, parent))

            # A cycle folder that already holds files means this month has
            # already gone out. Re-sending corrected reports into it would
            # overwrite what the partner has already seen, so revisions go in
            # their own subfolder and the original stays intact.
            cyc_id = _drive_folder(svc, cycle_label, market_folders[e.market])
            if _has_files(svc, cyc_id):
                inner = _list_folders(svc, cyc_id)
                n = 2
                while True:
                    name = f"v{n} updates"
                    fid = inner.get(name)
                    if fid is None:
                        cyc_id = _drive_folder(svc, name, cyc_id)
                        break
                    if not _has_files(svc, fid):
                        cyc_id = fid
                        break
                    n += 1
                log.info("drive: %s %s already delivered, using v%d", e.market,
                         cycle_label, n)
            cycle_folders[e.market] = cyc_id
        name = report_filename(e)
        dest = cycle_folders[e.market]

        # Replace rather than duplicate, so re-running a delivery after a fix
        # does not leave the partner looking at two versions of one report.
        old = svc.files().list(
            q=f"'{dest}' in parents and name = '{name.replace(chr(39), chr(92)+chr(39))}' "
              f"and trashed = false",
            fields="files(id)", supportsAllDrives=True,
            includeItemsFromAllDrives=True, corpora="allDrives"
        ).execute().get("files", [])
        media = MediaFileUpload(r.stored_path, mimetype="application/pdf",
                                resumable=True)
        if old:
            svc.files().update(fileId=old[0]["id"], media_body=media,
                               supportsAllDrives=True).execute()
        else:
            svc.files().create(body={"name": name, "parents": [dest]},
                               media_body=media, fields="id",
                               supportsAllDrives=True).execute()
        n += 1

    if not cycle_folders:
        raise RuntimeError("Nothing to upload - no report has a stored PDF.")

    # One market per delivery, so there is exactly one folder to share.
    links = []
    for market, fid in cycle_folders.items():
        svc.permissions().create(fileId=fid, supportsAllDrives=True,
                                 body={"role": "reader", "type": "anyone"}).execute()
        links.append(f"https://drive.google.com/drive/folders/{fid}")
    where = " / ".join(list(cycle_folders)[:2])
    return (links[0], f"{n} report{'s' if n != 1 else ''} filed under "
                      f"{where} / {cycle_label}, folder shared by link.", n)


# ---------------------------------------------------------------- Dropbox
def upload_dropbox_folder(group, period: str, cycle_label: str) -> tuple[str, str, int]:
    """A zip per market, flat in one Dropbox folder.

    Unlike Drive, 7 Mountains keep everything in a single folder named by the
    zip itself - "7 Mountains KY August reports.zip" beside "7 Mountains Media
    PA January 2026 Reports.zip". So this builds one zip and shares the FILE,
    rather than making a folder tree nobody there uses.
    """
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

    tmp = settings.data_dir / "deliveries" / period
    path, n = build_zip(group, period, tmp)
    if n == 0:
        raise RuntimeError("Nothing to upload - no report has a stored PDF.")

    base = settings.dropbox_folder.strip().rstrip("/")
    if base and not base.startswith("/"):
        base = "/" + base
    month = dt.date.fromisoformat(period + "-01").strftime("%B %Y")
    dest = f"{base}/{_safe(group.group)} {month} Reports.zip"

    data = path.read_bytes()
    # 150 MB is Dropbox's single-request ceiling. A market's reports are a
    # fraction of that, so anything near it is a bug worth surfacing rather
    # than silently chunking around.
    if len(data) > 140 * 1024 * 1024:
        raise RuntimeError(f"{dest} is {len(data) / 1048576:.0f} MB, which is too "
                           f"large for one upload and far larger than a month of "
                           f"reports should ever be.")
    dbx.files_upload(data, dest, mode=WriteMode("overwrite"))

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
    return link, (f"{n} report{'s' if n != 1 else ''} zipped to {dest}, "
                  f"public link created."), n


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

    # ARCHIVE AND CLIENT LINK ARE TWO DIFFERENT THINGS.
    #
    # Every market's reports are filed in the shared drive, which is the
    # internal record. What the client is handed is separate: usually that same
    # Drive folder, but the 7 Mountains markets get a Dropbox link instead. So
    # a Dropbox market uploads twice - once to archive, once to share - rather
    # than the Drive copy being skipped.
    share_to = group.target or settings.delivery_target
    label = dt.date.fromisoformat(period + "-01").strftime("%Y-%m %B")
    archive_url, share_url, message, n = "", "", "", 0

    def fail(msg: str) -> Delivery:
        db.rollback()
        rec = Delivery(period=period, group=group_name, target=share_to,
                       ok=False, message=msg, archive_url=archive_url)
        db.add(rec); db.commit()
        return rec

    if settings.delivery_configured["drive"]:
        try:
            archive_url, drive_msg, n = upload_drive_folder(group, period, label)
        except ModuleNotFoundError as exc:
            return fail(f"The Google library is not installed on this deploy "
                        f"({exc.name}). Redeploy so requirements.txt is picked up.")
        except Exception as exc:  # noqa: BLE001
            import traceback; traceback.print_exc()
            return fail(f"Filing to Drive failed: {type(exc).__name__}: {exc}")
        share_url, message = archive_url, drive_msg

    if share_to == "dropbox":
        try:
            share_url, dbx_msg, n2 = upload_dropbox_folder(group, period, label)
            n = n or n2
        except ModuleNotFoundError as exc:
            return fail(f"Reports are filed in Drive, but the Dropbox library is "
                        f"not installed ({exc.name}). Redeploy so requirements.txt "
                        f"is picked up.")
        except Exception as exc:  # noqa: BLE001
            import traceback; traceback.print_exc()
            return fail(f"Reports are filed in Drive"
                        f"{' at ' + archive_url if archive_url else ''}, but the "
                        f"Dropbox upload failed: {type(exc).__name__}: {exc}")
        message = (f"{dbx_msg} Also filed in Drive." if archive_url else dbx_msg)

    if share_url:
        rec = Delivery(period=period, group=group_name, target=share_to, reports=n,
                       share_url=share_url, archive_url=archive_url, ok=True,
                       message=message)
        db.add(rec); db.commit()
        return rec

    # Nothing configured: build the zip and keep it here to download.
    out_dir = settings.data_dir / "deliveries" / period
    try:
        path, n = build_zip(group, period, out_dir)
    except Exception as exc:  # noqa: BLE001
        return fail(f"Could not build the zip: {type(exc).__name__}: {exc}")
    if n == 0:
        return fail("Every report is marked ready but none has a stored PDF.")
    rec = Delivery(period=period, group=group_name, target="local", reports=n,
                   bytes=path.stat().st_size, local_path=str(path), ok=True,
                   message="Zip built and kept here for download. Set up Drive or "
                           "Dropbox to deliver it automatically.")
    db.add(rec); db.commit()
    return rec


def latest_deliveries(db: Session, period: str) -> dict[str, Delivery]:
    from sqlalchemy import select as _select
    out: dict[str, Delivery] = {}
    for d in db.scalars(_select(Delivery).where(Delivery.period == period)
                        .order_by(Delivery.id)).all():
        out[d.group] = d              # last one per group wins
    return out
