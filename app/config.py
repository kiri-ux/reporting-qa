from pathlib import Path
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite:///./reportqa.db"
    data_dir: Path = Path("./data")

    # shared secret appended to the inbound URL, e.g. /inbound/mailgun?k=...
    inbound_secret: str = "change-me"

    slack_webhook_url: str = ""

    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    digest_from: str = ""
    digest_to: str = ""          # comma separated, the always-cc list

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

    # Google: a service account JSON key, pasted whole into one env var. The
    # service account's email must be a member of the shared drive.
    google_service_account_json: str = ""
    drive_parent_folder_id: str = ""         # folder inside the shared drive

    # Dropbox: a refresh token, not an access token. Access tokens expire after
    # four hours, so one pasted in by hand is dead before the next cycle.
    dropbox_app_key: str = ""
    dropbox_app_secret: str = ""
    dropbox_refresh_token: str = ""
    dropbox_folder: str = "/Vici Reports"

    # device breakout legitimately excludes these products
    device_excluded_products: str = "Mobile Conquesting,PPC,YouTube,LinkedIn,Performance Max"
    # creative types that never carry a preview image
    no_preview_creative_types: str = "Audio,HTML5"
    # device may run under the eligible total by this much before we flag it
    device_under_tolerance_pct: float = 10.0

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
    def digest_recipients(self) -> list[str]:
        return [e.strip() for e in self.digest_to.split(",") if e.strip()]

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
            "drive": bool(self.google_service_account_json.strip()
                          and self.drive_parent_folder_id.strip()),
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
