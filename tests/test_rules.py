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
