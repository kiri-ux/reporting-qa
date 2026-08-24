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
    orders_s3_key: str = ""              # e.g. "reporting/order-list.xlsx"
    orders_s3_region: str = "us-east-1"
    orders_s3_sheet: str = ""            # xlsx only; blank means the first sheet
    orders_refresh_minutes: int = 60     # re-check S3 at most this often

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
    def digest_recipients(self) -> list[str]:
        return [e.strip() for e in self.digest_to.split(",") if e.strip()]


settings = Settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
