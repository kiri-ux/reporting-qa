from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./reportqa.db"
    data_dir: Path = Path("./data")

    # shared secret appended to the inbound URL, e.g. /inbound/mailgun?k=...
    inbound_secret: str = "change-me"

    # MASTER OFF SWITCH for anything that leaves the building.
    #
    # Blank credentials already mean nothing sends, but that is a side effect
    # of a field being empty rather than a decision. Someone configuring SMTP
    # for an unrelated reason should not accidentally start mailing people, so
    # this has to be turned on deliberately.
    # Pin the board to one cycle while a month is being re-run. The dropdown
    # still switches freely; this only decides where /cycle lands with no
    # period in the URL. Set it to "" to go back to following the calendar.
    default_period: str = "2026-07"

    # Re-check reports in the background when the checking code changes. Off
    # only if a deploy needs to stop the sweep for some reason - a stale report
    # showing a fixed rule's old answer is the failure this prevents.
    auto_recheck: bool = True
    # How many recent cycles the automatic sweep covers. Older months are
    # re-checked on demand: a finding on a cycle that shipped in March is not
    # in anybody's way, and re-reading four years of PDFs on every deploy is
    # work nobody asked for.
    recheck_periods: int = 3

    notifications_enabled: bool = False

    slack_webhook_url: str = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    digest_from: str = ""
    digest_to: str = ""          # comma separated, the always-cc list
    # HARD LIMIT ON WHO CAN BE EMAILED. Anything outside these domains is
    # dropped before send, whatever put it in the list. The roster carries the
    # clients' own addresses for reference, and nothing this tool sends is
    # written for a client to read.
    internal_domains: str = "vicimediainc.com"

    # order list in S3. Leave the bucket blank to use manual upload only.
    orders_s3_bucket: str = ""
    orders_s3_key: str = ""              # one key, several comma separated, or a prefix ending in /
    orders_s3_region: str = "us-east-1"
    orders_s3_sheet: str = ""            # xlsx only; blank means the first sheet
    orders_refresh_minutes: int = 60     # re-check S3 at most this often

    # passed to boto3 explicitly, so a missing one is a legible error rather
    # than boto3's unhelpful "Unable to locate credentials"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""
    aws_session_token: str = ""

    # TapClicks sends one email per client, so eighteen reports arrive as
    # eighteen deliveries. Reports landing within this window for the same
    # market and month join one batch instead of each making their own.
    batch_window_minutes: int = 240
    # ...and the digest waits until nothing new has arrived for this long, so
    # one market gets one Slack post and one email rather than eighteen.
    batch_quiet_minutes: int = 12

    # ---- delivery ----------------------------------------------------------
    # Where a finished partner's zip goes when the roster does not say.
    delivery_target: str = "local"           # local | drive | dropbox

    # Google. Two ways in, and the OAuth one is first because most Workspace
    # orgs now enforce iam.disableServiceAccountKeyCreation, which makes a
    # service account key impossible to create at all.
    #
    #   OAuth  - a person authorises once and the refresh token is stored here.
    #            Files land in the SHARED DRIVE, which owns them, so nothing is
    #            lost if that person leaves.
    #   Key    - a service account JSON key, if the org policy allows one.
    google_client_id: str = ""
    google_client_secret: str = ""
    google_refresh_token: str = ""
    google_service_account_json: str = ""
    drive_parent_folder_id: str = ""         # folder inside the shared drive

    # Dropbox: a refresh token, not an access token. Access tokens expire after
    # four hours, so one pasted in by hand is dead before the next cycle.
    dropbox_app_key: str = ""
    dropbox_app_secret: str = ""
    dropbox_refresh_token: str = ""
    dropbox_folder: str = "/Vici Reports"

    # How many months of report PDFs to keep on the disk. Once delivery is
    # wired up the shared drive holds the archive, so what is here is a cache
    # for the viewer and the packager. 0 keeps everything.
    keep_pdf_months: int = 4

    # device breakout legitimately excludes these products
    device_excluded_products: str = "Mobile Conquesting,PPC,YouTube,LinkedIn,Performance Max"
    # creative types that never carry a preview image
    no_preview_creative_types: str = "Audio,HTML5"
    # device may run under the eligible total by this much before we flag it
    # 20%, not 10. A device breakout runs a little under the eligible
    # total on most reports - unknown-device filtering alone accounts
    # for ten or twelve - so at 10% this was warning on the normal case
    # and nobody was reading it.
    device_under_tolerance_pct: float = 20.0

    class Config:
        env_file = ".env"

    @property
    def excluded_products(self) -> set[str]:
        return {p.strip() for p in self.device_excluded_products.split(",") if p.strip()}

    @property
    def s3_configured(self) -> bool:
        return bool(self.orders_s3_bucket and self.orders_s3_key)

    @property
    def orders_s3_keys(self) -> list[str]:
        return [k.strip() for k in self.orders_s3_key.split(",") if k.strip()]

    @property
    def notify_status(self) -> dict:
        """What would actually go out right now, for the UI to state plainly."""
        return {
            "enabled": self.notifications_enabled,
            "email": bool(self.notifications_enabled and self.smtp_host
                          and self.digest_from),
            "slack": bool(self.notifications_enabled and self.slack_webhook_url),
            "to": self.digest_recipients,
            "domains": self.internal_domains,
        }

    @property
    def internal_domain_list(self) -> list[str]:
        return [d.strip().lower().lstrip("@")
                for d in self.internal_domains.split(",") if d.strip()]

    def is_internal(self, address: str) -> bool:
        """Is this one of ours?

        The reporting roster lists each partner's own contacts - the people the
        finished reports go to - so a client address is always one wrong join
        away from a recipient list. Nothing this tool sends is written for a
        client to read: it names failed checks, missing reports and internal
        owners. So the check is a domain allowlist rather than a promise that
        the current code paths behave.
        """
        addr = (address or "").strip().lower()
        if "@" not in addr:
            return False
        domain = addr.rsplit("@", 1)[1]
        allowed = self.internal_domain_list
        if not allowed:
            return True                 # explicitly cleared: no restriction
        return any(domain == d or domain.endswith("." + d) for d in allowed)

    @property
    def digest_recipients(self) -> list[str]:
        return [e.strip() for e in self.digest_to.split(",") if e.strip()]

    @property
    def google_auth_mode(self) -> str:
        if self.google_refresh_token.strip():
            return "oauth"
        if self.google_service_account_json.strip():
            return "key"
        return ""

    def google_credentials(self) -> dict:
        """The service account key, however it was pasted in.

        Render's env editor is a single-line box, so the JSON usually arrives
        with its newlines escaped or the whole thing base64'd. Both are handled
        rather than failing with a JSON parse error that says nothing useful.
        """
        import base64
        import json
        raw = self.google_service_account_json.strip()
        if not raw:
            raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON is not set.")
        if not raw.lstrip().startswith("{"):
            raw = base64.b64decode(raw).decode()
        info = json.loads(raw)
        # A private key pasted through a single-line field has literal \n in it.
        if "private_key" in info:
            info["private_key"] = info["private_key"].replace("\\n", "\n")
        return info

    @property
    def delivery_configured(self) -> dict[str, bool]:
        return {
            "drive": bool(self.google_auth_mode and self.drive_parent_folder_id.strip()),
            "dropbox": bool(self.dropbox_app_key.strip()
                            and self.dropbox_app_secret.strip()
                            and self.dropbox_refresh_token.strip()),
        }

    def env_report(self) -> list[dict]:
        """What the running process actually sees. Rendered on /orders so a
        stale deploy or a value pasted into the wrong box is visible."""
        def mask(v: str) -> str:
            v = (v or "").strip()
            if not v:
                return ""
            return v[:4] + "..." + v[-4:] if len(v) > 12 else "set"

        looks_like_key = self.orders_s3_key.strip().startswith(("AKIA", "ASIA"))
        return [
            {"name": "ORDERS_S3_BUCKET", "value": self.orders_s3_bucket.strip(),
             "ok": bool(self.orders_s3_bucket.strip()), "note": ""},
            {"name": "ORDERS_S3_KEY", "value": self.orders_s3_key.strip(),
             "ok": bool(self.orders_s3_key.strip()) and not looks_like_key,
             "note": "This is an AWS access key, not a path. It belongs in AWS_ACCESS_KEY_ID."
                     if looks_like_key else
                     ("Whole bucket. Every CSV in it is merged."
                      if self.orders_s3_key.strip().lstrip("/") == "" else
                      "Folder. Every CSV under it is merged."
                      if self.orders_s3_key.strip().endswith("/") else
                      "No trailing slash, so only this exact object is read.")},
            {"name": "ORDERS_S3_REGION", "value": self.orders_s3_region.strip(),
             "ok": bool(self.orders_s3_region.strip()), "note": ""},
            {"name": "AWS_ACCESS_KEY_ID", "value": mask(self.aws_access_key_id),
             "ok": bool(self.aws_access_key_id.strip()),
             "note": "" if self.aws_access_key_id.strip() else "Not visible to the app."},
            {"name": "AWS_SECRET_ACCESS_KEY", "value": mask(self.aws_secret_access_key),
             "ok": bool(self.aws_secret_access_key.strip()),
             "note": "" if self.aws_secret_access_key.strip() else "Not visible to the app."},
        ]


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
