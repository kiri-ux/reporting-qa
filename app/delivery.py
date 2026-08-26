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
import time
import logging
import re
import zipfile
from pathlib import Path

from sqlalchemy.orm import Session

from .board import Expected, GroupRow, by_group
from .folder_match import best as pick_folder

# WHAT IS WORTH TRYING AGAIN. All of these are the connection giving out
# mid-upload rather than anything being wrong with the file - the same button
# pressed again works, which is the definition of something the tool should
# have done itself.
RETRY_UPLOAD = (BrokenPipeError, ConnectionError, TimeoutError, OSError)
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
    from .naming import canonical_name

    r = getattr(e, "report", None) or e
    # THE BUILT NAME, WHATEVER HAPPENED UPSTREAM.
    #
    # Renaming used to happen only on the feed and on a replacement, so a
    # report uploaded by hand kept whatever it arrived as - and two of them
    # reached a partner's Dropbox folder as "Digital Marketing Report.pdf" and
    # "Digital Marketing Report - Lifetime.pdf". Every path names its reports
    # now; this is the last mile saying so, so one missed path cannot put an
    # unfilable name in front of a partner again.
    built = canonical_name(r) if getattr(r, "period", None) is not None else ""
    if built and built.lower() != (getattr(r, "filename", "") or "").lower():
        raw = built
    else:
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


def _key_market(s: str) -> str:
    import re as _re
    return _re.sub(r"[^a-z0-9]", "", (s or "").lower())


def drive_pins(db) -> dict[str, str]:
    """Market key -> the Drive folder id somebody pinned for it."""
    from sqlalchemy import select as _select

    from .db import Partner
    out = {}
    for p in db.scalars(_select(Partner)).all():
        fid = (getattr(p, "drive_folder_id", "") or "").strip()
        if fid:
            out[_key_market(p.partner)] = fid
    return out


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


def upload_drive_folder(group, period: str, cycle_label: str,
                        progress=None, tag: str = "",
                        pins: dict | None = None) -> tuple[str, str, int]:
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
    skipped = 0
    market_folders: dict[str, str] = {}
    cycle_folders: dict[str, str] = {}
    # ONE LISTING EACH, NOT ONE PER FILE.
    #
    # The parent folder was re-listed for every market and the destination
    # folder was searched once per PDF - two round trips to Google before a
    # single byte moved, on a partner with thirty reports. Both answers are
    # the same all the way through one delivery.
    parent_folders: dict[str, str] | None = None
    dest_files: dict[str, dict[str, str]] = {}
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
            # A PIN BEATS A MATCH. When somebody has fixed a folder by hand -
            # pulled a folder out of a folder, renamed one, merged two - the
            # name is no longer a safe way to find it, and a best guess is not
            # good enough against work somebody did deliberately.
            pinned = (pins or {}).get(_key_market(e.market), "")
            if pinned:
                market_folders[e.market] = pinned
                log.info("drive: %s -> pinned folder %s", e.market, pinned)
            else:
                if parent_folders is None:
                    parent_folders = _list_folders(svc, parent)
                existing = parent_folders
                hit, why = pick_folder(e.market, existing)
                log.info("drive: %s -> %s", e.market, why)
                market_folders[e.market] = (existing[hit] if hit
                                            else _drive_folder(svc, e.market, parent))

            # ONE FOLDER PER CYCLE, AND THE LINK NEVER MOVES.
            #
            # This used to send a re-delivery into a "v2 updates" subfolder and
            # share that instead, so the partner already had a link to a folder
            # that was now out of date and the new link held only the reports
            # that changed. A link that stops being the current month is worse
            # than a file being replaced under it - the whole point of handing
            # over a folder is that it stays right.
            #
            # So corrections are filed over the top, in the same folder, under
            # the same link. Drive keeps the previous version of a replaced
            # file in its own revision history if anybody needs it back.
            # A TAG MAKES A SECOND FOLDER BESIDE THE MONTH, not instead of it.
            # The partner's own link keeps pointing at the untagged folder and
            # keeps being right.
            label = f"{cycle_label} - {tag}" if tag else cycle_label
            cycle_folders[e.market] = _drive_folder(
                svc, label, market_folders[e.market])

        # ONLY WHAT HAS CHANGED GOES UP.
        #
        # Re-uploading every report every time takes several minutes on a big
        # partner to change nothing, and nine megabytes a file is the whole of
        # that time. A report still filed under the name it has, from the file
        # it was filed from, is already in the folder. The folder above is
        # still resolved for it, so the link comes back either way.
        if not tag and not needs_send(e):
            skipped += 1
            continue
        name = report_filename(e)
        dest = cycle_folders[e.market]

        # Replace rather than duplicate, so re-running a delivery after a fix
        # does not leave the partner looking at two versions of one report.
        # The folder's contents are read once, not once per file.
        if dest not in dest_files:
            dest_files[dest] = {
                f["name"]: f["id"] for f in svc.files().list(
                    q=f"'{dest}' in parents and trashed = false",
                    fields="files(id,name)", pageSize=1000,
                    supportsAllDrives=True, includeItemsFromAllDrives=True,
                    corpora="allDrives").execute().get("files", [])}
        # AND A RENAME LEAVES NO SECOND COPY BEHIND.
        #
        # A report's name carries every order id touching it, so a re-read that
        # picks up another one renames the file. Uploading the new name into a
        # folder that still holds the old one hands the partner the same report
        # twice. Only the name THIS report was last filed as is touched, so
        # nothing else in the folder is at risk.
        was = "" if tag else (getattr(r, "delivered_as", "") or "").strip()
        if was and was != name and was in dest_files[dest]:
            try:
                svc.files().update(fileId=dest_files[dest].pop(was),
                                   body={"trashed": True},
                                   supportsAllDrives=True).execute()
                log.info("drive: replaced %s with %s", was, name)
            except Exception:                            # noqa: BLE001
                log.warning("drive: could not remove the old %s", was)
        old_id = dest_files[dest].get(name)

        # A BROKEN PIPE IS THE NETWORK, NOT THE FILE.
        #
        # "BrokenPipeError: [Errno 32] Broken pipe" halfway through a partner
        # killed the whole run, and the same button pressed a third time
        # worked - because nothing was wrong with the report, the connection
        # just went away mid-upload. Google's client retries inside execute()
        # when asked; the media object cannot be reused after a failure, so
        # the retry builds a fresh one.
        last = None
        for attempt in range(4):
            try:
                media = MediaFileUpload(r.stored_path, mimetype="application/pdf",
                                        resumable=True)
                if old_id:
                    svc.files().update(fileId=old_id, media_body=media,
                                       supportsAllDrives=True).execute(num_retries=4)
                else:
                    new = svc.files().create(
                        body={"name": name, "parents": [dest]},
                        media_body=media, fields="id",
                        supportsAllDrives=True).execute(num_retries=4)
                    dest_files[dest][name] = new["id"]
                break
            except RETRY_UPLOAD as exc:                  # noqa: PERF203
                last = exc
                log.warning("drive: %s failed (%s), attempt %d",
                            name, type(exc).__name__, attempt + 1)
                time.sleep(1.5 * (attempt + 1))
        else:
            raise RuntimeError(
                f"{name} would not upload after 4 attempts: "
                f"{type(last).__name__}: {last}")
        if not tag:
            # Only the partner's own folder is tracked. A tagged copy is a side
            # folder somebody asked for; recording it here would make the next
            # real delivery hunt for a stale name in the wrong place.
            r.delivered_as = name[:255]
        n += 1
        if progress:
            progress(n, f"filing {e.client} in Drive")

    if not cycle_folders:
        raise RuntimeError("Nothing to upload - no report has a stored PDF.")

    # One market per delivery, so there is exactly one folder to share.
    links = []
    for market, fid in cycle_folders.items():
        svc.permissions().create(fileId=fid, supportsAllDrives=True,
                                 body={"role": "reader", "type": "anyone"}).execute()
        links.append(f"https://drive.google.com/drive/folders/{fid}")
    where = " / ".join(list(cycle_folders)[:2])
    if n == 0 and skipped:
        # NOT A FAILURE, and not nothing either: the folder is right and the
        # link is the same one. This is the normal answer to pressing sync when
        # nothing has moved since the last one.
        return (links[0], f"Already up to date - all {skipped} report"
                          f"{'s' if skipped != 1 else ''} are filed as they "
                          f"stand. Nothing needed sending.", skipped)
    return (links[0], f"{n} report{'s' if n != 1 else ''} filed under "
                      f"{where} / {cycle_label}"
                      + (f", {skipped} already up to date" if skipped else "")
                      + ", folder shared by link.", n)


# ---------------------------------------------------------------- Dropbox
def upload_dropbox_folder(group, period: str, cycle_label: str,
                          progress=None, tag: str = "") -> tuple[str, str, int]:
    """The month's PDFs in a shared Dropbox folder, view only.

    NOT A ZIP. It used to build one and share the file, so the partner
    downloaded an archive, extracted it, and found a folder inside a folder
    before reaching a PDF. The link now opens on the reports themselves.

    A shared link gives whoever has it view access; it does not make them a
    member of the folder, so nobody following the link can edit or delete
    anything in it.
    """
    import dropbox
    from dropbox.files import WriteMode
    from dropbox.sharing import RequestedVisibility, SharedLinkSettings

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

    base = settings.dropbox_folder.strip().rstrip("/")
    if base and not base.startswith("/"):
        base = "/" + base
    month = dt.date.fromisoformat(period + "-01").strftime("%B %Y")
    folder = f"{base}/{_safe(group.group)} {month} Reports"
    # A TAG MAKES A SECOND FOLDER BESIDE THE MONTH, not instead of it. The
    # partner's own link keeps pointing at the untagged folder.
    if tag:
        folder += f" - {_safe(tag)}"

    n = 0
    skipped = 0
    for e in group.expected:
        r = e.report
        if not r or not r.stored_path:
            continue
        # ONLY WHAT HAS CHANGED. See the same rule in the Drive path: a report
        # still filed under the name it has, from the file it was filed from,
        # is already in this folder.
        if not tag and not needs_send(e):
            skipped += 1
            continue
        src = Path(r.stored_path)
        if not src.exists():
            log.warning("missing file for %s / %s: %s", e.market, e.client, src)
            continue
        data = src.read_bytes()
        # 150 MB is Dropbox's single-request ceiling. A report is a fraction of
        # that, so anything near it is a bug worth surfacing rather than
        # silently chunking around.
        if len(data) > 140 * 1024 * 1024:
            raise RuntimeError(f"{src.name} is {len(data) / 1048576:.0f} MB, far "
                               f"larger than a report should ever be.")
        name = report_filename(e)
        # A RENAME LEAVES NO SECOND COPY BEHIND. The folder path and the link
        # never change, so a report filed last week under an older name is
        # still sitting there beside the new one unless it is taken out. Only
        # the name THIS report was last filed as is removed.
        was = "" if tag else (getattr(r, "delivered_as", "") or "").strip()
        if was and was != name:
            try:
                dbx.files_delete_v2(f"{folder}/{was}")
                log.info("dropbox: replaced %s with %s", was, name)
            except Exception:                            # noqa: BLE001
                pass          # it was never there, which is the normal case
        dbx.files_upload(data, f"{folder}/{name}",
                         mode=WriteMode("overwrite"))
        if not tag:
            r.delivered_as = name[:255]
            r.delivered_stamp = file_stamp(r.stored_path)
        n += 1
        if progress:
            progress(n, f"sending {e.client} to Dropbox")

    if n == 0 and not skipped:
        raise RuntimeError("Nothing to upload - no report has a stored PDF.")

    link = _dropbox_link(dbx, folder, SharedLinkSettings, RequestedVisibility)
    if n == 0:
        return link, (f"Already up to date - all {skipped} report"
                      f"{'s' if skipped != 1 else ''} are filed as they stand. "
                      f"Nothing needed sending."), skipped
    return link, (f"{n} report{'s' if n != 1 else ''} in {folder}"
                  + (f", {skipped} already up to date" if skipped else "")
                  + ", shared view-only."), n


def _dropbox_link(dbx, path: str, SharedLinkSettings, RequestedVisibility) -> str:
    """A public, view-only link to a folder - the existing one if there is one.

    Dropbox refuses to mint a second link for the same path, and that refusal
    is not a failure: the link it already has is the one to hand over.
    """
    from dropbox.sharing import RequestedLinkAccessLevel

    settings_kw = {"requested_visibility": RequestedVisibility.public}
    try:
        # Spelled out rather than left to the default. A link is view-only
        # either way, and saying so is what keeps it that way if the default
        # ever changes.
        settings_kw["access"] = RequestedLinkAccessLevel.viewer
    except Exception:                                    # noqa: BLE001
        pass
    try:
        return dbx.sharing_create_shared_link_with_settings(
            path, SharedLinkSettings(**settings_kw)).url
    except Exception:                                    # noqa: BLE001
        try:
            return dbx.sharing_create_shared_link_with_settings(
                path, SharedLinkSettings(
                    requested_visibility=RequestedVisibility.public)).url
        except Exception:                                # noqa: BLE001
            links = dbx.sharing_list_shared_links(path=path, direct_only=True).links
            if not links:
                raise
            return links[0].url


# ---------------------------------------------------------------- orchestration
def deliver(db: Session, period: str, group_name: str, *,
            force: bool = False, progress=None, tag: str = "",
            ready_only: bool = False) -> Delivery:
    """Package one group and push it wherever that partner takes delivery."""
    groups = {g.group: g for g in by_group(db, period)}
    group = groups.get(group_name)
    # SEND THE FINISHED ONES AND LEAVE THE REST.
    #
    # A partner is not all-or-nothing: two thirds of it can be signed off while
    # somebody is still working through the last dozen, and waiting for the
    # last one to hand over the first thirty is a week of nobody having
    # anything. This packages what is clear and leaves the folder to grow.
    if group is not None and ready_only:
        from dataclasses import replace as _replace
        keep = [e for e in group.expected if e.ready]
        if not keep:
            rec = Delivery(period=period, group=group_name, ok=False,
                           message="Nothing is signed off and clear yet.")
            db.add(rec); db.commit(); return rec
        group = _replace(group, expected=keep)
    if group is None:
        rec = Delivery(period=period, group=group_name, ok=False,
                       message="No reports expected for that partner this cycle.")
        db.add(rec); db.commit(); return rec

    if not group.ready and not force and not ready_only:
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
                       ok=False, message=msg, archive_url=archive_url, tag=tag)
        db.add(rec); db.commit()
        return rec

    if settings.delivery_configured["drive"]:
        try:
            archive_url, drive_msg, n = upload_drive_folder(
                group, period, label, progress=progress, tag=tag,
                pins=drive_pins(db))
        except ModuleNotFoundError as exc:
            return fail(f"The Google library is not installed on this deploy "
                        f"({exc.name}). Redeploy so requirements.txt is picked up.")
        except Exception as exc:  # noqa: BLE001
            import traceback; traceback.print_exc()
            return fail(f"Filing to Drive failed: {type(exc).__name__}: {exc}")
        share_url, message = archive_url, drive_msg

    if share_to == "dropbox":
        try:
            share_url, dbx_msg, n2 = upload_dropbox_folder(
                group, period, label, progress=progress, tag=tag)
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
                       message=message, tag=tag)
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


def file_stamp(path: str) -> str:
    """What identifies this exact PDF: its size and its modified time.

    Not a hash. These run to nine megabytes each and a partner has thirty of
    them, so hashing the lot on every page load to answer "has this changed"
    would cost more than the upload it saves.
    """
    try:
        st = Path(path).stat()
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return ""


def needs_send(e) -> bool:
    """Is this report different from what is sitting in the partner's folder?

    Two ways it can be: filed under a name it no longer has, or filed as a file
    that has since been replaced. Anything else has already gone up and does
    not need to go again.
    """
    r = getattr(e, "report", None)
    if not r or not getattr(r, "stored_path", ""):
        return False
    if (getattr(r, "delivered_as", "") or "") != report_filename(e):
        return True
    # AN UNKNOWN STAMP IS NOT A CHANGED FILE.
    #
    # Every report that went out before this was recorded has no stamp, and
    # reading that as "changed" told a partner with thirty-six perfectly good
    # reports in its folder that thirty-three of them needed sending again.
    # Crying wolf about it is worse than missing one: the name still has to
    # match, and the stamp gets written the next time it does go up.
    stamp = getattr(r, "delivered_stamp", "") or ""
    return bool(stamp) and stamp != file_stamp(r.stored_path)


def out_of_sync(group) -> list[str]:
    """Reports in this partner that are not what is sitting in its folder.

    A LINK THAT STOPPED BEING THE CURRENT MONTH IS THE THING TO AVOID. Files
    get corrected, re-checked, renamed and replaced all cycle, and the folder
    only changes when somebody presses sync - so a partner can be showing a
    perfectly good link to last Tuesday's reports.

    Every report records what it was last filed as, which makes this exact
    rather than a guess: a name that does not match, or was never filed at all,
    is a report the partner does not have.
    """
    return [e.client or report_filename(e)
            for e in group.expected if needs_send(e)]


def latest_deliveries(db: Session, period: str) -> dict[str, Delivery]:
    """The partner's OWN link, per group - the one that never moves.

    Tagged deliveries are deliberately not in here. A tagged copy is a second
    folder somebody made for a reason, and letting it become "the link" for the
    partner is exactly the thing the stable link is for.
    """
    from sqlalchemy import select as _select
    out: dict[str, Delivery] = {}
    for d in db.scalars(_select(Delivery).where(Delivery.period == period)
                        .order_by(Delivery.id)).all():
        if (getattr(d, "tag", "") or ""):
            continue
        out[d.group] = d              # last one per group wins
    return out


def tagged_deliveries(db: Session, period: str) -> dict[str, list]:
    """{group: [the extra links somebody made, newest last]}."""
    from sqlalchemy import select as _select
    out: dict[str, list] = {}
    seen: dict[tuple, Delivery] = {}
    for d in db.scalars(_select(Delivery).where(Delivery.period == period)
                        .order_by(Delivery.id)).all():
        if not (getattr(d, "tag", "") or "") or not d.ok:
            continue
        seen[(d.group, d.tag)] = d          # last run of that tag wins
    for (grp, _tag), d in seen.items():
        out.setdefault(grp, []).append(d)
    return out


# ------------------------------------------------------------ in the background
#
# PACKAGING IS NOT A THING A BROWSER SHOULD WAIT FOR.
#
# It uploads every PDF in the partner - forty-five pages and nine megabytes
# each - one after another, and that was happening inside the request. Several
# minutes of a spinner with no way to tell a slow upload from a dead one, and
# a proxy timeout at the end of it if the partner was big enough.
#
# Same shape as an on-demand re-check: a row either worker can read, a thread
# that touches it as it goes, and a card that says "12 of 30".
def delivery_key(period: str, group: str) -> str:
    return f"deliver:{period}:{group}"


def start_delivery(db: Session, period: str, group_name: str, *,
                   force: bool = False, tag: str = "",
                   ready_only: bool = False) -> dict:
    """Kick a packaging run off and come straight back."""
    import threading

    from sqlalchemy import select as _select

    from .db import DeliveryJob, SessionLocal

    key = delivery_key(period, group_name) + (f":{tag}" if tag else "")
    row = db.scalar(_select(DeliveryJob).where(DeliveryJob.key == key))
    if row is not None and row.state == "running" and not row.stalled:
        return {"done": row.done, "total": row.total}
    if row is None:
        row = DeliveryJob(key=key)
        db.add(row)
    groups = {g.group: g for g in by_group(db, period)}
    g = groups.get(group_name)
    row.partner_group = group_name
    row.period = period
    row.state = "running"
    row.done = 0
    # WHAT IT IS ACTUALLY GOING TO SEND, not what the partner holds. A sync
    # that had one file to move sat at "0 of 10 packaging" and looked stuck,
    # because the count was every report in the partner rather than the ones
    # going up.
    if not g:
        row.total = 0
    elif tag:
        row.total = len([e for e in g.expected if e.report and e.report.stored_path])
    else:
        rows_ = [e for e in g.expected if not ready_only or e.ready]
        row.total = len([e for e in rows_ if needs_send(e)])
    row.note = "starting"
    row.started_at = row.updated_at = dt.datetime.utcnow()
    db.commit()
    total = row.total

    def run():
        from .proc import background
        own = SessionLocal()

        def touch(done: int, note: str, state: str = "running"):
            r = own.scalar(_select(DeliveryJob).where(DeliveryJob.key == key))
            if r is None:
                return
            r.done, r.note, r.state = done, note[:255], state
            r.updated_at = dt.datetime.utcnow()
            own.commit()

        try:
            with background():          # a page load still comes first
                rec = deliver(own, period, group_name, force=force, tag=tag,
                              ready_only=ready_only,
                              progress=lambda n, note: touch(n, note))
                touch(rec.reports or total,
                      (rec.message or "")[:255],
                      "done" if rec.ok else "failed")
        except Exception as exc:                              # noqa: BLE001
            log.exception("packaging %s failed", group_name)
            try:
                touch(0, f"{type(exc).__name__}: {exc}", "failed")
            except Exception:                                 # noqa: BLE001
                pass
        finally:
            own.close()

    threading.Thread(target=run, name=f"deliver-{group_name}"[:30],
                     daemon=True).start()
    return {"done": 0, "total": total}


def delivery_jobs(db: Session) -> dict[str, dict]:
    """Packaging runs in flight, keyed by partner group.

    A job whose thread went away with a deploy is closed out here, so a card
    cannot sit on "12 of 30" forever looking like the tool is stuck.
    """
    from sqlalchemy import select as _select

    from .db import DeliveryJob
    out, dead = {}, False
    for j in db.scalars(_select(DeliveryJob)
                        .where(DeliveryJob.state == "running")).all():
        if j.stalled:
            j.state = "failed"
            j.note = (f"Stopped after {j.done} of {j.total or '?'} - the process "
                      f"running it went away, usually a deploy. Press Package "
                      f"and share again.")[:255]
            dead = True
            continue
        out[j.partner_group] = {"done": j.done, "total": j.total,
                                "note": j.note, "period": j.period}
    if dead:
        try:
            db.commit()
        except Exception:                                     # noqa: BLE001
            db.rollback()
    return out
