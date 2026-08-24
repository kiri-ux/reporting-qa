"""Regression tests pinned to real reports whose answers were verified by hand."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.checks import run_all                                   # noqa: E402
from app.checks.parser import extract_tables, headline, pdf_text  # noqa: E402
from app.checks.rules import LINE_ITEM, is_device_excluded        # noqa: E402

FIXTURES = Path(__file__).parent / "fixtures"
pytestmark = pytest.mark.skipif(not FIXTURES.exists(), reason="no fixture PDFs checked in")


def codes(path):
    return {f["code"] for f in run_all(path)["findings"]}


def test_clean_report_has_no_findings():
    r = run_all(FIXTURES / "benton_rodeo.pdf")
    assert r["severity"] == "pass"
    assert r["impressions"] == 53_280 and r["clicks"] == 89


def test_device_over_is_a_failure():
    """Centre Hills is all Social Mirror, nothing excluded, device reads 109,559
    on a 105,174 campaign. A breakout cannot exceed what was served."""
    r = run_all(FIXTURES / "centre_hills.pdf")
    assert r["severity"] == "fail"
    assert "device_over" in {f["code"] for f in r["findings"]}


def test_device_excludes_mobile_conquesting():
    """Watsontown's device table is exactly Video + CTV. It only reconciles once
    Mobile Conquesting is taken out of the denominator."""
    text = pdf_text(FIXTURES / "watsontown.pdf")
    tables = [t for t in extract_tables(text) if LINE_ITEM.search(t.title or "")]
    eligible = sum(v["Impressions"] for t in tables for n, v in t.body
                   if not is_device_excluded(n, {"Mobile Conquesting", "PPC", "YouTube",
                                                 "LinkedIn", "Performance Max"}))
    assert eligible == 70_349
    assert "device_under" not in codes(FIXTURES / "watsontown.pdf")


def test_ctv_click_base_is_informational_not_a_failure():
    assert "ctv_click_base" in codes(FIXTURES / "watsontown.pdf")
    assert "line_items_clicks" not in codes(FIXTURES / "watsontown.pdf")


def test_ctv_ctr_base_recognised():
    """Central Penn states 0.20%, which is clicks over non-CTV impressions."""
    c = codes(FIXTURES / "central_penn.pdf")
    assert "ctv_ctr_base" in c and "headline_ctr" not in c


def test_missing_thumbnail_counted():
    r = run_all(FIXTURES / "keystone_altoona.pdf")
    f = next(x for x in r["findings"] if x["code"] == "missing_thumbnail")
    assert "2 creative previews" in f["title"]


def test_geofence_blank_business_name_is_info_only():
    r = run_all(FIXTURES / "salem_rv.pdf")
    f = next(x for x in r["findings"] if x["code"] == "geofence_no_business_name")
    assert f["severity"] == "info"
    assert r["severity"] == "pass"


def test_headline_parses_impressions_only_reports():
    """DOOH reports print impressions with no clicks or CTR block."""
    imps, clicks, ctr = headline(pdf_text(FIXTURES / "independence_ford.pdf"))
    assert imps and clicks is None and ctr is None


# ---------------------------------------------------------------- order list
def test_xlsx_order_list_imports_with_dates():
    """The list lives in S3 as xlsx, so dates arrive as datetimes, not strings."""
    import datetime as dt
    import io

    from openpyxl import Workbook
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base, OrderLine
    from app.roster import import_orders

    wb = Workbook()
    ws = wb.active
    ws.append(["Market", "Client", "Account", "Campaign Start Date",
               "Campaign End Date", "Buyer", "P&A Team Member"])
    ws.append(["7 Mountains PA Selinsgrove", "Benton Rodeo", "53915",
               dt.date(2026, 5, 1), dt.date(2026, 7, 31), "Alyssa Aileo", "Taylor"])
    buf = io.BytesIO()
    wb.save(buf)

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    assert import_orders(db, buf.getvalue(), filename="order-list.xlsx") == 1
    line = db.query(OrderLine).one()
    assert line.ends_on == dt.date(2026, 7, 31)
    assert line.buyer == "Alyssa Aileo" and line.team_member == "Taylor"


def test_lifetime_due_when_campaign_ends_in_period():
    import datetime as dt

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base, OrderLine, Report
    from app.roster import completeness

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(OrderLine(market="M", client="Benton Rodeo", account_ids="53915",
                     starts_on=dt.date(2026, 5, 1), ends_on=dt.date(2026, 7, 31)))
    db.add(OrderLine(market="M", client="Never Arrives", account_ids="99999",
                     starts_on=dt.date(2026, 1, 1), ends_on=dt.date(2026, 12, 31)))
    db.add(Report(batch_id=1, filename="x.pdf", client="Benton Rodeo",
                  account_ids="53915", market="M", period="2026-07"))
    db.commit()

    comp = completeness(db, "M", "2026-07")
    assert [m["client"] for m in comp["missing"]] == ["Never Arrives"]
    assert [m["client"] for m in comp["lifetime_due"]] == ["Benton Rodeo"]


def test_sync_keeps_the_old_list_when_s3_is_unreachable():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.orders_s3 as s3mod
    from app.config import settings
    from app.db import Base, OrderLine

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    db.add(OrderLine(market="M", client="Kept", account_ids="1234"))
    db.commit()

    settings.orders_s3_bucket, settings.orders_s3_key = "b", "k"
    s3mod._client = lambda: (_ for _ in ()).throw(RuntimeError("AccessDenied"))
    rec = s3mod.sync(db, force=True)
    assert rec.ok is False and "AccessDenied" in rec.message
    assert db.query(OrderLine).count() == 1


# ---------------------------------------------------------------- products
def test_products_detected_from_sections_not_the_footnote():
    """Every report's footnote mentions CTV and TikTok. Neither should be
    reported as a product unless it is actually on the buy."""
    from app.checks.parser import extract_tables, pdf_text
    from app.checks.products import detect

    text = pdf_text(FIXTURES / "benton_rodeo.pdf")
    found = detect(text, extract_tables(text))
    assert "Mobile Conquesting" in found
    assert "TikTok" not in found and "CTV" not in found


def test_order_product_names_map_to_report_names():
    from app.checks.products import map_order_product
    assert map_order_product("Mobile Conquesting Display & Video Ads") == "Mobile Conquesting"
    assert map_order_product("Amazon Premium CTV + Video Ads") == "CTV"
    assert map_order_product("Pay-Per-Click Ads") == "PPC"


def test_product_check_is_quiet_without_an_order_list():
    r = run_all(FIXTURES / "benton_rodeo.pdf")
    assert not [f for f in r["findings"] if f["code"].startswith("product_")]


def test_product_check_flags_missing_and_rogue():
    r = run_all(FIXTURES / "benton_rodeo.pdf",
                expected_products={"Mobile Conquesting", "Meta"})
    got = {f["code"] for f in r["findings"]}
    assert "product_missing" in got                     # Meta ordered, not on the report
    r2 = run_all(FIXTURES / "benton_rodeo.pdf", expected_products={"Meta"})
    assert "product_rogue" in {f["code"] for f in r2["findings"]}


def test_account_ids_split_on_underscores():
    """Filenames join accounts inconsistently. Underscore is a word character,
    so "14885_48365" would otherwise read as a single id."""
    from app.checks.parser import meta_from_filename
    m = meta_from_filename("July 2026_W and L Subaru 14885_48365.pdf")
    assert m["client"] == "W and L Subaru"
    assert m["account_ids"] == "14885 48365"


# ---------------------------------------------------------------- IO export
IO_EXPORT = FIXTURES / "orders_io_export.csv"


@pytest.mark.skipif(not IO_EXPORT.exists(), reason="no IO export fixture")
def test_io_export_eligibility():
    """Live IOs and orders live inside the period only. No RFPs, no cancelled
    line items, nothing that ended before the period started."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base, OrderLine
    from app.orders_io import import_io_export

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    res = import_io_export(db, IO_EXPORT.read_bytes(), period="2026-07")

    assert res["skipped"].get("RFP")
    assert res["skipped"].get("ended before the period")
    assert res["skipped"].get("line item cancelled")

    rows = db.query(OrderLine).all()
    assert all("RFP" not in (r.campaign or "") for r in rows)
    # one row per client and product, so a client's report has one expected set
    assert len({(r.client, r.product) for r in rows}) == len(rows)

    subaru = {r.product for r in rows if r.client == "W&L Subaru"}
    assert subaru == {"Meta", "Mobile Conquesting", "Social Mirror", "Video"}


def test_concurrent_init_db_does_not_crash(tmp_path):
    """Two gunicorn workers call init_db() at once. create_all is check-then-
    create, so the loser used to die with 'table already exists'."""
    import threading

    import app.db as dbmod
    from app.config import settings

    settings.database_url = f"sqlite:///{tmp_path / 'race.db'}"
    import importlib
    importlib.reload(dbmod)

    errors = []

    def boot():
        try:
            dbmod.init_db()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=boot) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


def test_overlapping_exports_are_merged_not_duplicated():
    """Two exports with overlapping date ranges must not double-count."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base, OrderLine
    from app.orders_io import import_io_export

    raw = IO_EXPORT.read_bytes()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)

    one = sessionmaker(bind=engine)()
    a = import_io_export(one, raw, period="2026-07")

    two = sessionmaker(bind=engine)()
    b = import_io_export(two, [raw, raw], period="2026-07")

    assert b["kept"] == a["kept"]
    assert b["duplicate_rows"] > a["duplicate_rows"]
    assert two.query(OrderLine).count() == a["kept"]


def test_export_guidance_reports_the_range_to_pull():
    """The export filters on line-item start date, so the range has to reach
    back to the oldest campaign still running, not 30 days."""
    import datetime as dt

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.db import Base
    from app.orders_io import import_io_export

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine)()
    g = import_io_export(db, IO_EXPORT.read_bytes(), period="2026-07")["guidance"]

    assert g["pull_to"] == dt.date.today().isoformat()
    assert g["pull_from"] < "2019-01-01"          # reaches back to the 2018 orders
    assert g["may_be_truncated"] is False


def test_s3_fingerprint_fits_its_column():
    """A folder of many exports must not overflow OrderSync.etag.

    The first version joined `key:etag` per object. Five files came to 299
    characters against a VARCHAR(255), so Postgres rejected the row from
    inside an exception handler and the request 500'd with the real cause
    nowhere on screen. SQLite ignores VARCHAR length, so the whole suite
    passed while production was broken - which is why this test asserts the
    length directly rather than trusting an insert to fail.
    """
    import hashlib
    from app.db import OrderSync

    limit = OrderSync.__table__.c.etag.type.length
    for n_files in (1, 5, 50, 500):
        parts = [f"orders/a_very_long_export_filename_{i:04d}.csv:"
                 f"{'d41d8cd98f00b204e9800998ecf8427e'}" for i in range(n_files)]
        digest = hashlib.sha256("|".join(parts).encode()).hexdigest()
        fingerprint = f"{len(parts)}f-{digest[:40]}"
        assert len(fingerprint) <= limit, f"{n_files} files overflows etag({limit})"

    # different contents must still produce different fingerprints
    a = hashlib.sha256(b"orders/x.csv:aaa").hexdigest()[:40]
    b = hashlib.sha256(b"orders/x.csv:bbb").hexdigest()[:40]
    assert a != b
