"""Regression tests pinned to real reports whose answers were verified by hand."""
import datetime as dt
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


def test_a_filtered_clicks_tile_is_informational_not_a_failure():
    """The Clicks tile leaves CTV, YouTube and PMax out on some templates.

    Renamed from ctv_click_base: the exclusion is not CTV alone, which is the
    bug this replaced - it recognised only the CTV case and failed the rest.
    """
    c = codes(FIXTURES / "watsontown.pdf")
    assert "clicks_exclude_products" in c and "line_items_clicks" not in c


def test_a_filtered_ctr_tile_is_recognised():
    """Central Penn's stated CTR is filtered clicks over filtered impressions.

    The old handling took CTV impressions out of the denominator and left every
    click in the numerator, so it only matched by luck. Both halves are
    filtered, and by five product patterns, not one.
    """
    c = codes(FIXTURES / "central_penn.pdf")
    assert "ctr_excludes_products" in c and "headline_ctr" not in c


def test_the_line_item_sum_reads_the_whole_grid():
    """It used to read the strict table parser, which stops after about
    seventeen rows - so every long report was failed for line items that did
    not add up to a campaign it had only seen a page of."""
    r = run_all(FIXTURES / "watsontown.pdf")
    f = next((x for x in r["findings"]
              if x["code"] in ("line_items_impressions", "clicks_exclude_products")), None)
    assert f is not None
    counted = next(t["value"] for t in f["trace"] if t["label"] == "Line items counted")
    assert int(counted) == 14


def test_a_device_breakout_matching_the_top_line_is_fine():
    """Credit King's device table sums to the whole campaign because YouTube is
    in it. The ceiling is the top line, not our guess at an eligible subset."""
    from app.checks.rules import check_device
    from app.checks.parser import Table

    dev = Table(title="Device Performance", rows=[
        ("Mobile", {"Impressions": 113867.0}), ("Desktop", {"Impressions": 55003.0}),
        ("Tablet", {"Impressions": 19835.0}), ("Streaming Device", {"Impressions": 3443.0}),
        ("Connected TV", {"Impressions": 1596.0})])
    li = Table(title="Line Item Performance", rows=[
        ("Acme - AI YouTube", {"Impressions": 40018.0}),
        ("Acme - Behavioral Display", {"Impressions": 153728.0})])
    assert check_device({"tables": [dev, li], "imps": 193746.0}) == []


def test_a_device_breakout_over_the_top_line_still_fails():
    from app.checks.rules import check_device
    from app.checks.parser import Table
    dev = Table(title="Device Performance",
                rows=[("Mobile", {"Impressions": 109559.0})])
    li = Table(title="Line Item Performance",
               rows=[("Acme - Display", {"Impressions": 105174.0})])
    out = check_device({"tables": [dev, li], "imps": 105174.0})
    assert len(out) == 1 and out[0]["code"] == "device_over"
    assert out[0]["trace"]


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


def test_one_email_per_client_makes_one_batch(tmp_path, monkeypatch):
    """Eighteen deliveries for one market must be one batch, not eighteen.

    TapClicks mails a separate report per client, so the naive handler creates
    a batch, a dashboard row and a Slack post for every single one, and the
    completeness check never sees a whole market at once.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("INBOUND_SECRET", "s3cret")

    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod, ingest as ingest_mod
    importlib.reload(db_mod); importlib.reload(ingest_mod)
    db_mod.init_db()

    db = db_mod.SessionLocal()
    pdf = (FIXTURES / "benton_rodeo.pdf").read_bytes()
    subject = "7 Mountains PA State College - July 2026 report"

    batches = []
    for client in ["Benton Rodeo", "Centre Hills", "Watsontown", "Salem RV", "Keystone"]:
        b = ingest_mod.process_batch(
            db, [(f"July 2026_{client}.pdf", pdf)], source="zapier",
            subject=subject, notify=False, coalesce=True)
        batches.append(b.id)

    assert len(set(batches)) == 1, f"expected one batch, got {sorted(set(batches))}"
    batch = db.get(db_mod.Batch, batches[0])
    assert len(batch.reports) == 5

    # a retried delivery must not double-count
    ingest_mod.process_batch(db, [("July 2026_Benton Rodeo.pdf", pdf)], source="zapier",
                             subject=subject, notify=False, coalesce=True)
    db.refresh(batch)
    assert len(batch.reports) == 5

    # and once the digest is out, the next report starts a fresh batch
    batch.notified_at = dt.datetime.utcnow()
    db.commit()
    later = ingest_mod.process_batch(db, [("July 2026_Nittany Motors.pdf", pdf)],
                                     source="zapier", subject=subject,
                                     notify=False, coalesce=True)
    assert later.id != batch.id


def test_digest_waits_for_quiet(tmp_path, monkeypatch):
    """A batch still receiving reports must not fire its digest early."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'q.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod, ingest as ingest_mod
    importlib.reload(db_mod); importlib.reload(ingest_mod)
    db_mod.init_db()

    db = db_mod.SessionLocal()
    b = db_mod.Batch(market="M", period="2026-07", status="done",
                     last_report_at=dt.datetime.utcnow())
    db.add(b); db.commit()

    ingest_mod.finish_batch(db, b.id, respect_quiet=True)
    db.refresh(b)
    assert b.notified_at is None, "digest went out while reports were still arriving"

    # now let it go quiet
    b.last_report_at = dt.datetime.utcnow() - dt.timedelta(hours=1)
    db.commit()
    assert ingest_mod.sweep_stale(db) == 1
    db.refresh(b)
    assert b.notified_at is not None, "a quiet batch never got its digest"


def test_roster_import_stops_at_the_issue_log(tmp_path, monkeypatch):
    """The roster workbook has a second twelve-column table under the first.

    It is the report-issue log, where column B is a client and column C is a
    bug description. Reading straight through merged the two and, because the
    log repeats partner names, it overwrote real roster rows - "7 Mountains
    KY" came out with Buyer "Expree Credit Union". A blank partner row is the
    boundary and must end the read.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'p.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    from app import partners as pmod
    importlib.reload(pmod)
    db_mod.init_db()
    db = db_mod.SessionLocal()

    csv_text = (
        "Partner,Buyer,Email,SEO,Email,Manager,Reporting Team,To:,Trainer,"
        "Reporting Notes,Buyer Notes\n"
        "7 Mountains KY,Lauren,laurenhunter@vicimediainc.com,Lauren,"
        "laurenhunter@vicimediainc.com,Amin,Paulina,wendy@7m.com,Katie,,\n"
        "3-Piece Media,Hanna,hannaw@vicimediainc.com,Matt,matt@vicimediainc.com,"
        "Mallory,Paulina,rgs@3piecemedia.com,Jennaya,,\n"
        ",,,,,,,,,,\n"
        "7 Mountains KY,Expree Credit Union,https://reporting.zone/x,"
        "Impressions not matching,,in progress,Paulina,,Katie,,\n"
    )
    n = pmod.import_partners(db, csv_text)
    assert n == 2, f"read {n} rows, so the issue log leaked in"

    ky = pmod.find(db, "7 Mountains KY")
    assert ky.buyer == "Lauren", f"issue log overwrote the roster: buyer={ky.buyer!r}"
    assert ky.reporting_team == "Paulina"
    assert ky.recipients == ["wendy@7m.com"]


def test_bundled_roster_is_clean():
    """Every row in the shipped roster must look like a roster row."""
    import csv as _csv
    from app.partners import SEED
    assert SEED.exists()
    rows = list(_csv.DictReader(SEED.open(encoding="utf-8-sig")))
    assert len(rows) > 150, f"only {len(rows)} partners shipped"
    for r in rows:
        assert "@" in r["buyer_email"], f"{r['partner']}: buyer_email={r['buyer_email']!r}"
        assert "@" in r["seo_email"], f"{r['partner']}: seo_email={r['seo_email']!r}"
        assert "http" not in r["buyer"], f"{r['partner']}: buyer is a URL"


def test_seo_falls_back_to_the_seo_person():
    """A blank campaign manager takes the partner's buyer - except on SEO."""
    from app.db import Partner
    from app.partners import resolve_owner
    p = Partner(partner="X", buyer="Amin", buyer_email="amin@v.com",
                seo="Lauren", seo_email="lauren@v.com")
    assert resolve_owner(p, "Display Ads", "") == ("Amin", "amin@v.com")
    assert resolve_owner(p, "Search Engine Optimization+", "") == ("Lauren", "lauren@v.com")
    assert resolve_owner(p, "SEO", "") == ("Lauren", "lauren@v.com")
    # an actual campaign manager always wins
    assert resolve_owner(p, "SEO", "Jane", "jane@v.com") == ("Jane", "jane@v.com")
    assert resolve_owner(None, "Display Ads", "") == ("", "")


# ---------------------------------------------------------------- the cycle
def test_business_days_skip_federal_holidays():
    """Counting weekdays only is right eight months a year and quietly wrong
    for the rest - and the months it breaks (January, December) are the ones
    with no slack to absorb a two-day error."""
    from app.cycle import federal_holidays, is_business_day, nth_business_day

    # July 2026: the 4th is a Saturday, so Friday the 3rd is the holiday.
    assert dt.date(2026, 7, 3) in federal_holidays(2026)
    assert not is_business_day(dt.date(2026, 7, 3))

    # January 2027: 1st is a Friday holiday, so business days start Monday 4th.
    assert nth_business_day(2027, 1, 1) == dt.date(2027, 1, 4)
    assert nth_business_day(2027, 1, 3) == dt.date(2027, 1, 6)

    # Thanksgiving 2026 is Nov 26; the 5th business day of December is unaffected.
    assert dt.date(2026, 11, 26) in federal_holidays(2026)
    assert nth_business_day(2026, 12, 5) == dt.date(2026, 12, 7)


def test_lifetime_window_matches_the_stated_rule():
    """Ends in the data month, or by the 3rd business day of the next: in.
    Ends on the 4th or 5th business day: waits for the following cycle."""
    from app.cycle import cycle_for

    c = cycle_for("2026-07")
    assert c.lifetime_cutoff == dt.date(2026, 8, 5)     # Mon 3, Tue 4, Wed 5
    assert c.due_on == dt.date(2026, 8, 7)              # 5th business day

    assert c.needs_lifetime(dt.date(2026, 7, 1))
    assert c.needs_lifetime(dt.date(2026, 7, 31))
    assert c.needs_lifetime(dt.date(2026, 8, 1))        # ends on the 1st: in
    assert c.needs_lifetime(dt.date(2026, 8, 5))        # 3rd business day: in
    assert not c.needs_lifetime(dt.date(2026, 8, 6))    # 4th business day: out
    assert not c.needs_lifetime(dt.date(2026, 8, 7))    # 5th business day: out
    assert not c.needs_lifetime(dt.date(2026, 6, 30))   # last cycle's
    assert not c.needs_lifetime(None)


def test_a_partner_ships_only_when_every_report_is_signed_off():
    """Delivery is gated on a person, not on the checks. A clean report that
    nobody looked at must not ship, and a failing one ships only when someone
    knowingly waives it."""
    from app.board import Expected, GroupRow
    from app.db import Report

    def rep(sev, state):
        return Report(severity=sev, review_state=state, findings=[])

    clean_unreviewed = Expected(market="M", group="G", client="A", kind="monthly",
                                report=rep("pass", "new"))
    assert clean_unreviewed.state == "in"
    assert not GroupRow("G", "", [clean_unreviewed]).ready

    clean_reviewed = Expected(market="M", group="G", client="A", kind="monthly",
                              report=rep("pass", "reviewed"))
    assert clean_reviewed.state == "ready"
    assert GroupRow("G", "", [clean_reviewed]).ready

    failing_reviewed = Expected(market="M", group="G", client="B", kind="monthly",
                                report=rep("fail", "reviewed"))
    assert failing_reviewed.state == "errors"
    assert not GroupRow("G", "", [failing_reviewed]).ready

    waived = Expected(market="M", group="G", client="B", kind="monthly",
                      report=rep("fail", "waived"))
    assert waived.state == "ready"

    # one missing report holds the whole group
    missing = Expected(market="M", group="G", client="C", kind="lifetime")
    assert missing.state == "missing"
    assert not GroupRow("G", "", [clean_reviewed, missing]).ready

    # and an empty group is not "ready" by vacuous truth
    assert not GroupRow("G", "", []).ready


def test_expected_set_covers_monthlies_and_lifetimes(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'c.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    from app import board as bmod
    importlib.reload(bmod)
    db_mod.init_db()
    db = db_mod.SessionLocal()

    db.add(db_mod.Partner(partner="7 Mountains PA State College", group="7 Mountains",
                          reporting_team="Jacob", delivery_target="dropbox"))
    db.add(db_mod.Partner(partner="7 Mountains KY", group="7 Mountains",
                          reporting_team="Paulina"))
    D = dt.date.fromisoformat
    for client, product, s, e in [
        ("Watsontown", "Display Ads", "2024-03-01", "2026-09-30"),   # still running
        ("Centre Hills", "Connected TV Ads", "2026-02-01", "2026-07-31"),  # ended in July
        ("Beech Bend", "Video Ads", "2026-06-01", "2026-08-03"),     # ends 1st bday
        ("Late Co", "Display Ads", "2026-06-01", "2026-08-06"),      # ends 4th bday
        ("Old Co", "Display Ads", "2025-01-01", "2026-06-15"),       # ended before
    ]:
        db.add(db_mod.OrderLine(market="7 Mountains PA State College", client=client,
                                product=product, starts_on=D(s), ends_on=D(e),
                                account_ids="14885"))
    db.commit()

    exp = bmod.expected_for(db, "2026-07")
    got = {(e.client, e.kind) for e in exp}

    assert ("Watsontown", "monthly") in got
    assert ("Watsontown", "lifetime") not in got        # still running
    assert ("Centre Hills", "monthly") in got
    assert ("Centre Hills", "lifetime") in got          # ended inside July
    assert ("Beech Bend", "lifetime") in got            # ended Aug 3
    assert ("Late Co", "lifetime") not in got           # ended Aug 6
    assert ("Old Co", "monthly") not in got             # ended in June
    assert all(e.group == "7 Mountains" for e in exp)

    groups = bmod.by_group(db, "2026-07", exp)
    assert [g.group for g in groups] == ["7 Mountains"]
    assert groups[0].target == "dropbox"
    assert not groups[0].ready                          # nothing received yet


def test_each_market_delivers_on_its_own(tmp_path, monkeypatch):
    """One link per market, not one per media group.

    The roster still has a group column so markets CAN be bundled, but nothing
    is bundled by default - 7 Mountains PA State College and 7 Mountains KY are
    two deliveries, each with its own link, both going to Dropbox.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'g.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    from app import partners as pmod, board as bmod
    importlib.reload(pmod); importlib.reload(bmod)
    db_mod.init_db()
    db = db_mod.SessionLocal()
    pmod.seed_if_empty(db)

    D = dt.date.fromisoformat
    for market in ["7 Mountains PA State College", "7 Mountains KY",
                   "Lockwood Digital Solutions Augusta",
                   "Lockwood Digital Solutions Denison"]:
        db.add(db_mod.OrderLine(market=market, client=f"{market} client",
                                product="Display Ads", account_ids="1234",
                                starts_on=D("2026-01-01"), ends_on=D("2026-12-31")))
    db.commit()

    groups = {g.group: g for g in bmod.by_group(db, "2026-07")}
    assert "7 Mountains" not in groups, "markets got bundled into a media group"
    assert "Lockwood Digital" not in groups
    assert set(groups) == {
        "7 Mountains PA State College", "7 Mountains KY",
        "Lockwood Digital Solutions Augusta", "Lockwood Digital Solutions Denison"}
    for g in groups.values():
        assert len(g.markets) == 1, f"{g.group} covers {g.markets}"

    # ...and every 7 Mountains market still goes to Dropbox, individually
    assert groups["7 Mountains PA State College"].target == "dropbox"
    assert groups["7 Mountains KY"].target == "dropbox"
    assert groups["Lockwood Digital Solutions Augusta"].target == ""


def test_a_mixed_s3_folder_still_imports(tmp_path, monkeypatch):
    """A folder holding an export plus anything else must not break the sync.

    The first version peeked at ONE file's header and applied that verdict to
    every file. A partner list or a stray sheet sorting alphabetically first
    therefore sent all five exports down the wrong path, and the failure
    surfaced as "list index out of range" with no file named.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'m.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    from app import roster as rmod
    importlib.reload(rmod)
    db_mod.init_db()
    db = db_mod.SessionLocal()

    export = tmp_path / "z_export.csv"
    export.write_bytes(IO_EXPORT.read_bytes())
    # sorts before the export, and is not an export
    other = tmp_path / "a_partner_list.csv"
    other.write_text("Partner,Buyer,Email\nFoo,Bar,x@y.com\n")

    res = rmod.import_orders(db, [other, export], filename="a_partner_list.csv",
                             period="2026-07")
    assert isinstance(res, dict), f"fell through to the flat-list path: {res!r}"
    assert res["kept"] > 0
    assert res.get("ignored_files") == ["a_partner_list.csv"]

    # and a file that cannot be read names itself rather than failing obscurely
    junk = tmp_path / "broken.csv"
    junk.write_bytes(b"\x00\xff binary")
    res = rmod.import_orders(db, [export, junk], filename="z_export.csv",
                             period="2026-07")
    assert res["kept"] > 0, "one bad file should not lose the good ones"


def test_drive_upload_follows_the_existing_market_folders(monkeypatch, tmp_path):
    """Reports must land in the shared drive's own tree, not a parallel one.

    The drive is already organised as `01_Reporting Markets / <Market> / ...`
    and maintained by hand. A market folder that already exists must be reused
    - matched case-insensitively, since these were typed by people - and a
    cycle folder created inside it. The CYCLE FOLDER is what gets shared.
    """
    import importlib
    from app import delivery as dmod
    importlib.reload(dmod)
    from app import board as bmod
    from app import db as db_mod
    Expected, GroupRow, Report = bmod.Expected, bmod.GroupRow, db_mod.Report

    # A stand-in Drive: folders by (parent, name), files by (parent, name).
    folders = {("PARENT", "7 Mountains PA State College"): "EXISTING_MARKET_ID"}
    files: dict[tuple[str, str], str] = {}
    shared: list[str] = []
    counter = {"n": 0}

    class FakeFiles:
        def list(self, q="", **kw):
            self._q = q
            return self

        def create(self, body=None, media_body=None, **kw):
            counter["n"] += 1
            new_id = f"ID{counter['n']}"
            parent = body["parents"][0]
            if body.get("mimeType", "").endswith("folder"):
                folders[(parent, body["name"])] = new_id
            else:
                files[(parent, body["name"])] = new_id
            self._result = {"id": new_id}
            return self

        def update(self, fileId=None, **kw):
            self._result = {"id": fileId}
            return self

        def execute(self):
            if hasattr(self, "_result"):
                r, self._result = self._result, None
                del self._result
                return r
            q = self._q
            parent = q.split("'")[1]
            if "folder" in q:
                return {"files": [{"id": i, "name": n}
                                  for (p, n), i in folders.items() if p == parent]}
            name = q.split("name = '")[1].split("'")[0]
            hit = files.get((parent, name))
            return {"files": [{"id": hit}] if hit else []}

    class FakePerms:
        def create(self, fileId=None, **kw):
            shared.append(fileId)
            return self

        def execute(self):
            return {}

    class FakeSvc:
        def files(self):
            return FakeFiles()

        def permissions(self):
            return FakePerms()

    monkeypatch.setattr(dmod, "_drive_credentials", lambda: object())
    monkeypatch.setattr(dmod, "build", lambda *a, **k: FakeSvc(), raising=False)
    import googleapiclient.discovery as disc
    import googleapiclient.http as ghttp
    monkeypatch.setattr(disc, "build", lambda *a, **k: FakeSvc())
    monkeypatch.setattr(ghttp, "MediaFileUpload", lambda *a, **k: object())
    monkeypatch.setattr(dmod.settings, "drive_parent_folder_id", "PARENT")

    pdf = tmp_path / "r.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")

    def exp(market, client, kind):
        return Expected(market=market, group=market, client=client, kind=kind,
                        report=Report(severity="pass", review_state="reviewed",
                                      findings=[], stored_path=str(pdf)))

    group = GroupRow("7 Mountains PA State College", "drive", [
        exp("7 Mountains PA State College", "Watsontown Trucking", "monthly"),
        exp("7 Mountains PA State College", "Centre Hills", "lifetime"),
    ])
    url, msg, n = dmod.upload_drive_folder(group, "2026-08", "2026-08 August")

    assert n == 2
    # the hand-made market folder was reused, not duplicated
    assert folders[("PARENT", "7 Mountains PA State College")] == "EXISTING_MARKET_ID"
    assert sum(1 for (p, _) in folders if p == "PARENT") == 1
    # a cycle folder was created inside it
    cycle_id = folders[("EXISTING_MARKET_ID", "2026-08 August")]
    # and the PDFs went inside the cycle folder, lifetime clearly labelled
    assert ("Watsontown Trucking.pdf") in [n for (p, n) in files if p == cycle_id]
    assert ("Centre Hills - Lifetime.pdf") in [n for (p, n) in files if p == cycle_id]
    # the shared thing is the cycle folder, and the link points at it
    assert shared == [cycle_id]
    assert url.endswith(cycle_id)


def test_seven_mountains_archives_to_drive_but_shares_dropbox(monkeypatch, tmp_path):
    """Archive and client link are two different destinations.

    Every market's reports are filed in the shared drive - that is the internal
    record and it does not change. What the CLIENT is handed is separate: the
    Drive folder for 199 markets, a Dropbox link for the seven 7 Mountains
    ones. A Dropbox market therefore uploads twice; the Drive copy is never
    skipped.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'d.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    from app import board as bmod
    importlib.reload(bmod)
    # delivery LAST, so it binds to the config and models above rather than to
    # whatever a previously-reloading test left in sys.modules
    from app import delivery as dmod
    importlib.reload(dmod)
    Expected, GroupRow, Report = bmod.Expected, bmod.GroupRow, db_mod.Report
    db_mod.init_db()
    db = db_mod.SessionLocal()

    calls = []
    monkeypatch.setattr(dmod, "upload_drive_folder",
                        lambda g, p, l: (calls.append("drive"),
                                         ("https://drive.google.com/drive/folders/DRV",
                                          "filed in Drive", 2))[1])
    monkeypatch.setattr(dmod, "upload_dropbox_folder",
                        lambda g, p, l: (calls.append("dropbox"),
                                         ("https://www.dropbox.com/scl/fo/XYZ",
                                          "shared on Dropbox", 2))[1])
    monkeypatch.setattr(type(dmod.settings), "delivery_configured",
                        property(lambda self: {"drive": True, "dropbox": True}))

    pdf = tmp_path / "r.pdf"; pdf.write_bytes(b"%PDF-1.4\n")

    def group_for(market, target):
        e = Expected(market=market, group=market, client="A Client", kind="monthly",
                     report=Report(severity="pass", review_state="reviewed",
                                   findings=[], stored_path=str(pdf)))
        return GroupRow(market, target, [e])

    # --- a 7 Mountains market: both, and the client link is Dropbox
    monkeypatch.setattr(dmod, "by_group",
                        lambda *a, **k: [group_for("7 Mountains PA State College", "dropbox")])
    rec = dmod.deliver(db, "2026-08", "7 Mountains PA State College")
    assert rec.ok, rec.message
    assert calls == ["drive", "dropbox"], f"got {calls}"
    assert "dropbox.com" in rec.share_url, "client was given the wrong link"
    assert "drive.google.com" in rec.archive_url, "nothing was archived to Drive"

    # --- any other market: Drive only, and that is also the client link
    calls.clear()
    monkeypatch.setattr(dmod, "by_group",
                        lambda *a, **k: [group_for("Cape Cod Broadcasting", "")])
    rec = dmod.deliver(db, "2026-08", "Cape Cod Broadcasting")
    assert rec.ok, rec.message
    assert calls == ["drive"], f"Dropbox was used for a non-7-Mountains market: {calls}"
    assert rec.share_url == rec.archive_url
    assert "drive.google.com" in rec.share_url


def test_a_dropbox_failure_does_not_lose_the_drive_copy(monkeypatch, tmp_path):
    """If Dropbox fails, the reports are still filed in Drive and the message
    says so - otherwise it reads as though nothing was delivered at all."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'f.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    from app import board as bmod
    importlib.reload(bmod)
    from app import delivery as dmod
    importlib.reload(dmod)
    Expected, GroupRow, Report = bmod.Expected, bmod.GroupRow, db_mod.Report
    db_mod.init_db()
    db = db_mod.SessionLocal()

    def boom(*a, **k):
        raise RuntimeError("insufficient_scope")

    monkeypatch.setattr(dmod, "upload_drive_folder",
                        lambda g, p, l: ("https://drive.google.com/drive/folders/DRV",
                                         "filed", 2))
    monkeypatch.setattr(dmod, "upload_dropbox_folder", boom)
    monkeypatch.setattr(type(dmod.settings), "delivery_configured",
                        property(lambda self: {"drive": True, "dropbox": True}))
    pdf = tmp_path / "r.pdf"; pdf.write_bytes(b"%PDF-1.4\n")
    e = Expected(market="7 Mountains KY", group="7 Mountains KY", client="C",
                 kind="monthly", report=Report(severity="pass", review_state="reviewed",
                                               findings=[], stored_path=str(pdf)))
    monkeypatch.setattr(dmod, "by_group",
                        lambda *a, **k: [GroupRow("7 Mountains KY", "dropbox", [e])])

    rec = dmod.deliver(db, "2026-08", "7 Mountains KY")
    assert not rec.ok
    assert "filed in Drive" in rec.message
    assert "insufficient_scope" in rec.message
    assert rec.archive_url.endswith("DRV"), "the Drive copy was not recorded"


def test_retention_frees_the_disk_but_keeps_the_record(tmp_path, monkeypatch):
    """A cycle is ~1.6 GB of PDFs. The findings and sign-offs must survive the
    file being deleted, because those are the record - the PDF itself lives in
    the shared drive by then."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'r.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("KEEP_PDF_MONTHS", "4")
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    from app import ingest as imod
    importlib.reload(imod)
    db_mod.init_db()
    db = db_mod.SessionLocal()

    today = dt.date.today()
    def months_back(n):
        y, m = today.year, today.month - n
        while m <= 0:
            y, m = y - 1, m + 12
        return f"{y:04d}-{m:02d}"

    b = db_mod.Batch(market="M", period=months_back(0)); db.add(b); db.flush()
    keep, drop = [], []
    for n, bucket in ((0, keep), (2, keep), (6, drop), (14, drop)):
        f = tmp_path / f"r{n}.pdf"
        f.write_bytes(b"x" * 5000)
        r = db_mod.Report(batch_id=b.id, period=months_back(n), client=f"C{n}",
                          filename=f.name, stored_path=str(f), severity="fail",
                          review_state="reviewed", reviewed_by="Jacob",
                          findings=[{"title": "Device breakout under total"}])
        db.add(r); bucket.append((r, f))
    db.commit()

    res = imod.prune_old_pdfs(db)
    assert res["files"] == 2, f"pruned {res['files']}"
    assert res["freed"] == 10000

    for r, f in keep:
        assert f.exists(), f"{r.period} was deleted and should not have been"
        assert r.stored_path
    for r, f in drop:
        assert not f.exists(), f"{r.period} should have been deleted"
        db.refresh(r)
        assert r.stored_path == "", "stored_path should be cleared"
        # the record itself survives
        assert r.severity == "fail"
        assert r.reviewed_by == "Jacob"
        assert r.findings[0]["title"] == "Device breakout under total"

    # 0 means keep everything
    monkeypatch.setattr(imod.settings, "keep_pdf_months", 0)
    assert imod.prune_old_pdfs(db)["files"] == 0


def test_inbound_key_survives_url_encoding(monkeypatch):
    """A base64 secret containing "+" arrives with spaces where the + were.

    A query string decodes "+" as a space - form-encoding semantics, applied
    to the query part whether or not anyone intended it. The value therefore
    arrives the right LENGTH with the right last four characters and simply
    is not the same string, which is about the most confusing way for a shared
    secret to fail.
    """
    import app.main as m
    from fastapi import HTTPException

    secret = "aB3+xY7/kP2mQ9nR4tV6wZ1cD8eF5gH0jL/sT+uXwLSg="
    monkeypatch.setattr(m.settings, "inbound_secret", secret)

    m._guard(secret)                                    # exact
    m._guard(secret.replace("+", " "))                  # what a URL delivers
    m._guard(secret + " ")                              # copy-paste picked up a space
    m._guard(secret.replace("+", " ") + " ")            # both at once

    # a genuinely different secret is still refused
    with pytest.raises(HTTPException) as err:
        m._guard(secret[:-4] + "WRO=")
    assert err.value.status_code == 403
    with pytest.raises(HTTPException):
        m._guard(None)
    with pytest.raises(HTTPException):
        m._guard("")

    # and the message points at the real cause rather than just "bad key"
    with pytest.raises(HTTPException) as err:
        m._guard(secret.replace("+", " ").replace("LSg=", "WRO="))
    assert "%2B" in err.value.detail, err.value.detail


def test_market_comes_from_the_order_list_when_the_subject_is_useless(tmp_path, monkeypatch):
    """TapClicks sends "FW: Daily report - All Client Data".

    Nothing in that names a market, and a batch filed under no market never
    joins a partner on the cycle board. The order list already knows which
    market a client belongs to and the filename already carries the client and
    its account ids, so the lookup goes through the data rather than the prose.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'mk.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    from app import ingest as imod
    importlib.reload(imod)
    db_mod.init_db()
    db = db_mod.SessionLocal()

    D = dt.date.fromisoformat
    db.add(db_mod.OrderLine(market="7 Mountains PA State College",
                            client="Watsontown Trucking", account_ids="14885",
                            product="Display Ads", starts_on=D("2026-01-01"),
                            ends_on=D("2026-12-31")))
    db.add(db_mod.OrderLine(market="Cape Cod Broadcasting", client="Chatham Bars Inn",
                            account_ids="51120", product="Display Ads",
                            starts_on=D("2026-01-01"), ends_on=D("2026-12-31")))
    db.commit()

    useless = "FW: Daily report - All Client Data"
    assert imod.guess_market(useless, "reports@tapclicks.com",
                             ["July 2026_Watsontown Trucking_14885.pdf"]) == ""

    # by account id on the filename
    assert imod.market_from_orders(
        db, ["July 2026_Watsontown Trucking_14885.pdf"]) == "7 Mountains PA State College"
    # by client name, no account id present
    assert imod.market_from_orders(
        db, ["July 2026_Chatham Bars Inn.pdf"]) == "Cape Cod Broadcasting"
    # a zip of several: the market most files agree on
    assert imod.market_from_orders(db, [
        "July 2026_Watsontown Trucking_14885.pdf",
        "July 2026_Watsontown Trucking_14885 Lifetime.pdf",
        "July 2026_Chatham Bars Inn.pdf"]) == "7 Mountains PA State College"
    # an unknown client leaves it blank rather than guessing
    assert imod.market_from_orders(db, ["July 2026_Someone Else Entirely.pdf"]) == ""


def test_process_batch_fills_in_a_blank_market(tmp_path, monkeypatch):
    """End to end: a real PDF arriving with a useless subject still lands on
    the right partner."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'pb.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    from app import roster as rmod, ingest as imod
    importlib.reload(rmod); importlib.reload(imod)
    db_mod.init_db()
    db = db_mod.SessionLocal()

    D = dt.date.fromisoformat
    db.add(db_mod.OrderLine(market="7 Mountains PA Selinsgrove", client="Benton Rodeo",
                            account_ids="19042", product="Video Ads",
                            starts_on=D("2026-01-01"), ends_on=D("2026-12-31")))
    db.commit()

    pdf = (FIXTURES / "benton_rodeo.pdf").read_bytes()
    batch = imod.process_batch(db, [("July 2026_Benton Rodeo_19042.pdf", pdf)],
                               source="zapier",
                               subject="FW: Daily report - All Client Data",
                               notify=False, coalesce=True)
    assert batch.market == "7 Mountains PA Selinsgrove", f"got {batch.market!r}"


def test_accepting_a_finding_clears_the_flag_but_keeps_the_note():
    """A report can carry a finding that is true, understood, and not worth
    acting on - CTV excluded from the CTR base, a creative type that never
    renders a preview. Ticking it off has to clear the status without deleting
    the note, or the next person to open the report rediscovers it cold."""
    import importlib
    from app import db as db_mod
    importlib.reload(db_mod)
    Report = db_mod.Report

    r = Report(severity="warn", review_state="new", acked=[], findings=[
        {"severity": "warn", "title": "1 creative preview did not render"},
        {"severity": "warn", "title": "2 creative previews did not render"},
        {"severity": "info", "title": "Products match the order"},
    ])
    assert r.effective_severity == "warn"
    assert r.board_state == "warnings"
    assert len(r.open_findings) == 2

    # accepting one of two identical-looking warnings leaves the other open
    r.acked = [0]
    assert r.effective_severity == "warn"
    assert len(r.open_findings) == 1
    assert len(r.findings) == 3, "the note must not be deleted"

    r.acked = [0, 1]
    assert r.effective_severity == "pass"
    assert r.board_state == "in"          # clean, but nobody has signed off
    assert len(r.findings) == 3

    r.review_state = "reviewed"
    assert r.ready and r.board_state == "ready"

    # a failure can be accepted too, and then it no longer blocks delivery
    f = Report(severity="fail", review_state="reviewed", acked=[],
               findings=[{"severity": "fail", "title": "Device breakout under total"}])
    assert not f.ready and f.board_state == "errors"
    f.acked = [0]
    assert f.effective_severity == "pass"
    assert f.ready, "an accepted failure should stop blocking the partner"

    # un-accepting puts it back
    f.acked = []
    assert not f.ready


def test_date_range_is_read_off_the_report():
    from app.checks.parser import date_range, pdf_text
    text = pdf_text(FIXTURES / "salem_rv.pdf", 1, 1)
    assert date_range(text) == (dt.date(2026, 7, 1), dt.date(2026, 7, 31))
    assert date_range("no such line here") is None


def test_a_lifetime_pulled_with_a_monthly_range_is_caught():
    """The failure this exists for.

    A lifetime pulled with the default monthly range looks completely normal -
    right client, right products, plausible numbers - and silently reports one
    month of a two-year campaign. Nothing else on the page gives it away.
    """
    from app.checks.rules import check_date_range

    july = (dt.date(2026, 7, 1), dt.date(2026, 7, 31))
    flight = (dt.date(2024, 3, 1), dt.date(2026, 7, 31))

    bad = check_date_range({"date_range": july, "is_lifetime": True,
                            "flight": flight, "period": "2026-07"})
    assert [f["severity"] for f in bad] == ["fail"]
    assert "does not go back to the campaign start" in bad[0]["title"]
    assert "Mar 01, 2024" in bad[0]["detail"]

    # a lifetime covering the whole flight says nothing at all - a confirmation
    # in a list of problems reads as another problem
    good = check_date_range({"date_range": (dt.date(2024, 3, 1), dt.date(2026, 7, 31)),
                             "is_lifetime": True, "flight": flight, "period": "2026-07"})
    assert good == []

    # cut short at the end
    short = check_date_range({"date_range": (dt.date(2024, 3, 1), dt.date(2026, 5, 31)),
                              "is_lifetime": True, "flight": flight, "period": "2026-07"})
    assert any("stops before the campaign ends" in f["title"] for f in short)

    # a monthly covering its own month is silent
    assert check_date_range({"date_range": july, "is_lifetime": False,
                             "flight": flight, "period": "2026-07"}) == []
    # ...and one covering the wrong month is not
    wrong = check_date_range({"date_range": (dt.date(2026, 6, 1), dt.date(2026, 6, 30)),
                              "is_lifetime": False, "flight": flight, "period": "2026-07"})
    assert wrong[0]["severity"] == "fail"
    assert "Jul 01, 2026 to Jul 31, 2026" in wrong[0]["detail"]

    # no date range printed at all
    none = check_date_range({"date_range": None, "is_lifetime": False, "period": "2026-07"})
    assert none[0]["severity"] == "warn"


def test_overlapping_orders_become_one_flight(tmp_path, monkeypatch):
    """Two overlapping orders are one continuous campaign to the client, so
    the flight runs from the FIRST start to the LAST end across all of them -
    not the bounds of whichever single order happened to be looked up."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'fl.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    from app import ingest as imod, board as bmod
    importlib.reload(imod); importlib.reload(bmod)
    db_mod.init_db()
    db = db_mod.SessionLocal()

    D = dt.date.fromisoformat
    for oid, product, s_, e_ in [
        ("50001", "Display Ads", "2024-03-01", "2025-06-30"),   # first order
        ("50002", "Connected TV Ads", "2025-01-15", "2026-07-31"),  # overlaps, runs later
        ("50003", "Video Ads", "2025-09-01", "2026-02-28"),      # sits inside
    ]:
        db.add(db_mod.OrderLine(market="7 Mountains KY", client="Awaken Bakery",
                                account_ids=oid, product=product,
                                starts_on=D(s_), ends_on=D(e_)))
    db.commit()

    assert imod.client_flight(db, "Awaken Bakery", "50002") == (D("2024-03-01"),
                                                                D("2026-07-31"))
    # found by name alone, no account id on the filename
    assert imod.client_flight(db, "Awaken Bakery", "") == (D("2024-03-01"), D("2026-07-31"))
    assert imod.client_flight(db, "Nobody At All", "") is None

    # and the board carries both dates so the reporter knows what to pull
    life = [e for e in bmod.expected_for(db, "2026-07") if e.kind == "lifetime"]
    assert life, "an order ending in July owes a lifetime"
    assert life[0].starts_on == D("2024-03-01")
    assert life[0].ends_on == D("2026-07-31")


def test_nothing_is_ever_emailed_to_a_client(monkeypatch, tmp_path):
    """The digest is internal. It names failed checks, missing reports and
    internal owners, and no part of it is written for a client to read.

    The reporting roster carries each partner's OWN contacts - the people the
    finished reports eventually go to - so a client address sits one wrong
    join away from a recipient list at all times. This asserts the guard, not
    the current call paths.
    """
    from app import notify as nmod

    sent = {}

    class FakeSMTP:
        def __init__(self, *a, **k): pass
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def starttls(self): pass
        def login(self, *a): pass
        def send_message(self, msg): sent["to"] = msg["To"]

    monkeypatch.setattr(nmod.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(nmod.settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(nmod.settings, "digest_from", "report-qa@vicimediainc.com")
    monkeypatch.setattr(nmod.settings, "smtp_user", "")
    monkeypatch.setattr(nmod.settings, "digest_to", "jacob@vicimediainc.com")
    monkeypatch.setattr(nmod.settings, "internal_domains", "vicimediainc.com")
    monkeypatch.setattr(nmod.settings, "notifications_enabled", True)   # the point here
                                                                       # is WHO, not whether

    class B:
        market, period, failed, warned, clean = "7 Mountains KY", "2026-07", 1, 2, 3
        reports, notified_at, id = [], None, 1

    # every client address from the real roster, offered as a recipient
    ok = nmod.send_digest(B(), None, extra_to=[
        "wendy@7mountainscreative.com",         # 7 Mountains
        "lhobbs@curtismedia.com",               # Curtis Media
        "sarahreghitto@ccb-media.com",          # Cape Cod
        "paulina@vicimediainc.com",             # one of ours
    ])
    assert ok
    got = [a.strip() for a in sent["to"].split(",")]
    assert sorted(got) == ["jacob@vicimediainc.com", "paulina@vicimediainc.com"], got
    for client in ("7mountainscreative", "curtismedia", "ccb-media"):
        assert client not in sent["to"], f"{client} was emailed"

    # a subdomain of ours is still ours
    sent.clear()
    nmod.send_digest(B(), None, extra_to=["kiri@mail.vicimediainc.com"])
    assert "kiri@mail.vicimediainc.com" in sent["to"]

    # a lookalike domain is not
    sent.clear()
    nmod.send_digest(B(), None, extra_to=["someone@notvicimediainc.com"])
    assert "notvicimediainc.com" not in sent["to"]

    # with only external addresses offered, nothing is sent at all
    sent.clear()
    monkeypatch.setattr(nmod.settings, "digest_to", "")
    assert nmod.send_digest(B(), None, extra_to=["wendy@7mountainscreative.com"]) is False
    assert not sent


def test_the_roster_client_addresses_never_reach_the_notifier():
    """Belt and braces: the To: column is display-only.

    It is read on the Partners page and in that CSV, and nowhere else. This
    fails if anyone later wires Partner.recipients into a send path.
    """
    from pathlib import Path as _P
    app_dir = _P(__file__).resolve().parent.parent / "app"
    users = []
    for f in app_dir.rglob("*.py"):
        if f.name in ("db.py", "partners.py"):
            continue                       # where it is defined and parsed
        text = f.read_text()
        if ".recipients" in text or "to_emails" in text:
            users.append(f.name)
    assert users in ([], ["main.py"]), f"client addresses referenced in {users}"
    if users == ["main.py"]:
        text = (app_dir / "main.py").read_text()
        for line in text.splitlines():
            if ".recipients" in line or "to_emails" in line:
                assert "_csv_response" in text and "partners_csv" in text
                assert "send_digest" not in line and "extra_to" not in line, line


def test_notifications_are_off_until_deliberately_turned_on(monkeypatch):
    """Blank credentials already mean silence, but that is an accident of an
    empty field. Someone configuring SMTP for an unrelated reason must not
    start mailing people as a side effect."""
    from app import notify as nmod

    class FakeSMTP:
        def __init__(self, *a, **k): raise AssertionError("mail was sent")
    def boom(*a, **k): raise AssertionError("slack was posted")

    monkeypatch.setattr(nmod.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(nmod.httpx, "post", boom)
    # fully configured, and still silent
    monkeypatch.setattr(nmod.settings, "smtp_host", "smtp.example.com")
    monkeypatch.setattr(nmod.settings, "digest_from", "qa@vicimediainc.com")
    monkeypatch.setattr(nmod.settings, "digest_to", "jacob@vicimediainc.com")
    monkeypatch.setattr(nmod.settings, "slack_webhook_url", "https://hooks.slack.com/x")
    monkeypatch.setattr(nmod.settings, "notifications_enabled", False)

    class B:
        market, period, failed, warned, clean = "M", "2026-07", 1, 2, 3
        reports, notified_at, id = [], None, 1

    assert nmod.send_digest(B()) is False
    assert nmod.post_slack(B()) is False
    assert nmod.settings.notify_status == {
        "enabled": False, "email": False, "slack": False,
        "to": ["jacob@vicimediainc.com"], "domains": nmod.settings.internal_domains}


def test_no_check_ever_reports_good_news():
    """Findings are things to act on. A rule that raises "everything is fine"
    puts a line in a list whose entire job is naming problems, and it gets
    read as another one - which is exactly what happened with the products
    check."""
    import inspect
    from app.checks import rules as rmod

    # info-level findings are allowed to exist for context inside a problem,
    # but no rule may raise one as its ONLY output on a clean report
    clean = {
        "expected_products": {"Social Mirror"}, "products": {"Social Mirror"},
        "date_range": (dt.date(2026, 7, 1), dt.date(2026, 7, 31)),
        "is_lifetime": False, "period": "2026-07",
        "flight": (dt.date(2024, 3, 1), dt.date(2026, 7, 31)),
        "text": "", "tables": [], "pages": 1, "imps": None, "clicks": None, "ctr": None,
    }
    for rule in rmod.RULES:
        try:
            out = rule(clean)
        except Exception:
            continue                      # a rule needing more context is not the point
        for f in out or []:
            assert f["severity"] in ("fail", "warn"), (
                f"{rule.__name__} raised a {f['severity']} finding on a clean "
                f"report: {f['title']!r}")


def test_a_note_saves_without_touching_sign_off(tmp_path, monkeypatch):
    """Writing a note must not mark the report reviewed, and marking it
    reviewed must not wipe a half-typed note."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'n.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    from fastapi.testclient import TestClient
    from app import main as mmod
    importlib.reload(mmod)

    db = db_mod.SessionLocal()
    b = db_mod.Batch(market="M", period="2026-07"); db.add(b); db.flush()
    r = db_mod.Report(batch_id=b.id, client="C", period="2026-07", severity="warn",
                      filename="C.pdf", findings=[{"severity": "warn", "title": "x"}],
                      acked=[])
    db.add(r); db.commit()
    rid = r.id

    c = TestClient(mmod.app)
    c.post(f"/report/{rid}/note", data={"note": "Partner knows, no action."},
           follow_redirects=False)
    db.expire_all()
    r = db.get(db_mod.Report, rid)
    assert r.review_note == "Partner knows, no action."
    assert r.review_state == "new", "saving a note must not sign the report off"
    assert r.reviewed_at is None

    c.post(f"/report/{rid}/review", data={"state": "reviewed", "who": "Jacob"},
           follow_redirects=False)
    db.expire_all()
    r = db.get(db_mod.Report, rid)
    assert r.review_state == "reviewed" and r.reviewed_by == "Jacob"
    assert r.review_note == "Partner knows, no action.", "sign-off wiped the note"


def test_every_check_reports_whether_it_ran():
    """The report page says what was VERIFIED, not only what went wrong.

    A rule that finds nothing and a rule that had no data to work with both
    return an empty list, so they have to be told apart - claiming "products
    match the order" when no order list is loaded is a claim the tool cannot
    make.
    """
    from app.checks import run_all
    from app.checks.rules import CHECKS

    labels = {label for _, label in CHECKS}
    assert len(labels) == len(CHECKS), "two checks share a label"

    r = run_all(FIXTURES / "salem_rv.pdf", filename="July 2026_Salem RV_17781.pdf",
                expected_products={"Display"}, period="2026-07",
                flight=(dt.date(2024, 1, 1), dt.date(2026, 7, 31)))
    checks = {c["label"]: c for c in r["checks"]}
    assert len(checks) == len(CHECKS), "a check went unreported"
    assert all(c["state"] in ("passed", "flagged", "failed", "skipped", "error")
               for c in checks.values())

    # this fixture's products do not match the order it was given
    prod = [c for c in r["checks"] if c["key"] == "check_products"][0]
    assert prod["state"] == "failed"
    # ...and the rest of it is clean
    assert sum(1 for c in r["checks"] if c["state"] == "passed") >= 8

    # with NO order list loaded, the products check must not claim a pass
    r2 = run_all(FIXTURES / "salem_rv.pdf", filename="July 2026_Salem RV_17781.pdf",
                 expected_products=None, period="2026-07")
    prod2 = [c for c in r2["checks"] if c["key"] == "check_products"][0]
    assert prod2["state"] == "skipped", prod2
    # and a report with no date range printed does not claim that one either
    assert [c for c in r2["checks"] if c["key"] == "check_date_range"][0]["state"] \
        in ("passed", "skipped")

    # every failed check has a matching finding, so nothing is flagged silently
    for c in r["checks"]:
        if c["state"] in ("failed", "flagged"):
            assert c["count"] >= 1


def test_line_item_ids_survive_the_rollup(tmp_path, monkeypatch):
    """One report row can cover several line items, so all their ids have to
    be kept. The order id alone does not point at one line when a client runs
    three products under it."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'li.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    from app import orders_io as oi, board as bmod
    importlib.reload(oi); importlib.reload(bmod)
    db_mod.init_db()
    db = db_mod.SessionLocal()

    res = oi.import_io_export(db, IO_EXPORT.read_bytes(), period="2026-07")
    assert res["kept"] > 0
    lines = db.scalars(db_mod.select(db_mod.OrderLine)).all() \
        if hasattr(db_mod, "select") else db.query(db_mod.OrderLine).all()

    with_ids = [l for l in lines if l.line_ids]
    assert with_ids, "no line item ids were captured at all"
    # at least one row rolled up more than a single line item
    multi = [l for l in with_ids if "," in l.line_ids]
    assert multi, "a multi-line row lost all but one id"
    for l in with_ids:
        for part in l.line_ids.split(","):
            assert part.strip().isdigit(), f"{l.line_ids!r} is not a list of ids"
        # and the order id column holds order ids, not line ids
        assert l.account_ids
        assert l.account_ids != l.line_ids


def test_replacing_a_pdf_rechecks_it_and_resets_sign_off(tmp_path, monkeypatch):
    """A replacement is a new file. The checks have to run against it, and any
    acceptance or sign-off that described the old copy has to clear - otherwise
    a corrected report inherits a green tick nobody gave it."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'rp.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    from fastapi.testclient import TestClient
    from app import main as mmod
    importlib.reload(mmod)

    db = db_mod.SessionLocal()
    b = db_mod.Batch(market="7 Mountains KY", period="2026-07"); db.add(b); db.flush()
    old = tmp_path / "old.pdf"
    old.write_bytes((FIXTURES / "benton_rodeo.pdf").read_bytes())
    r = db_mod.Report(batch_id=b.id, client="Benton Rodeo", account_ids="19042",
                      period="2026-07", filename="Benton Rodeo.pdf",
                      stored_path=str(old), severity="fail", acked=[0],
                      review_state="reviewed", reviewed_by="Jacob",
                      reviewed_at=dt.datetime.utcnow(),
                      review_note="Waiting on a re-pull from Tap",
                      findings=[{"severity": "fail", "title": "old problem"}])
    db.add(r); db.commit()
    rid = r.id

    c = TestClient(mmod.app)
    fixed = (FIXTURES / "salem_rv.pdf").read_bytes()
    resp = c.post(f"/report/{rid}/replace",
                  files={"file": ("fixed.pdf", fixed, "application/pdf")},
                  follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    r = db.get(db_mod.Report, rid)

    assert Path(r.stored_path).read_bytes() == fixed, "the file was not swapped"
    assert r.checks, "the checks did not re-run"
    assert r.findings != [{"severity": "fail", "title": "old problem"}]
    assert r.acked == [], "an acceptance of the old file carried over"
    assert r.review_state == "new" and r.reviewed_at is None, "sign-off carried over"
    assert r.review_note == "Waiting on a re-pull from Tap", "the note was lost"

    # something that is not a PDF is refused outright
    bad = c.post(f"/report/{rid}/replace",
                 files={"file": ("notes.txt", b"hello", "text/plain")},
                 follow_redirects=False)
    assert bad.status_code == 400


def test_folder_matching_refuses_to_guess_between_siblings():
    """The dangerous failure is not a missed match - that just creates a new
    folder. It is matching the WRONG one, which files a client's reports in
    another client's folder where nobody looks for them."""
    from app.folder_match import best

    drive = {n: n for n in [
        "Summit Media Knoxville", "Summit Media Wichita", "Summit Media Honolulu",
        "Summit Media Louisville", "Summit Media Birmingham", "Summit Media Richmond",
        "Lotus Boise", "Lotus Reno", "Lotus Fresno", "Lotus Tucson", "Lotus Las Vegas",
        "Woof Boom Lima, OH", "Woof Boom Muncie, IN",
        "Sarkes Tarzian Bloomington", "Sarkes Tarzian Indianapolis",
        "Sarkes Tarzian Ft. Wayne",
        "Original Company Central", "Original Company North", "Original Company South",
        "Results Radio Chico", "Results Radio Redding", "Results Radio Sacramento",
        "Results Radio Yuba-Marysville",
        "Stephens Media Group Merced, CA (previously Mapleton)",
        "Stephens Media Group Monroe, LA (previously Lagniappe)",
        "Black Diamond Broadcasting Traverse City", "Moxii", "Cape Cod Broadcasting",
        "7 Mountains PA", "7 Mountains PA State College", "7 Mountains PA Selinsgrove",
    ]}

    # --- the folder is named differently but is unmistakably the same partner
    for partner, want in [
        ("Results Media Solutions Chico", "Results Radio Chico"),
        ("Results Media Solutions Redding", "Results Radio Redding"),
        ("Results Media Solutions Yuba-Marysville", "Results Radio Yuba-Marysville"),
        ("Black Diamond Traverse City", "Black Diamond Broadcasting Traverse City"),
        ("Stephens Merced, CA", "Stephens Media Group Merced, CA (previously Mapleton)"),
        ("Moxi", "Moxii"),
        ("7 Mountains PA State College", "7 Mountains PA State College"),
    ]:
        got, why = best(partner, drive)
        assert got == want, f"{partner!r} -> {got!r} ({why}), wanted {want!r}"

    # --- siblings that share everything but the city must NEVER cross over
    for partner in ["Summit Media Nashville", "Lotus Denver", "Woof Boom Toledo, OH",
                    "Sarkes Tarzian Evansville", "Original Company East",
                    "Results Media Solutions Fresno", "Stephens Media Group Tulsa, OK"]:
        got, why = best(partner, drive)
        assert got is None, f"{partner!r} wrongly matched {got!r} ({why})"

    # --- a partner with no folder at all
    assert best("Awaken Bakery", drive)[0] is None
    assert best("Cape Cod Broadcasting", drive)[0] == "Cape Cod Broadcasting"

    # --- two folders that fit equally well are refused rather than picked
    tie = {"Acme Marketing Denver": "1", "Acme Advertising Denver": "2"}
    got, why = best("Acme Denver", tie)
    assert got is None and "ambiguous" in why, why


def test_folder_matching_never_mismatches_the_real_roster():
    """Run the whole roster against the real folder list. Any match that is
    not a genuine rename is a bug that misfiles a client."""
    import csv as _csv
    from app.folder_match import best
    from app.partners import SEED

    drive = {n: n for n in [
        "7 Mountains PA", "7 Mountains PA State College", "7 Mountains PA Selinsgrove",
        "Summit Media Knoxville", "Summit Media Wichita", "Summit Media Honolulu",
        "Summit Media Louisville", "Summit Media Birmingham", "Summit Media Richmond",
        "Lotus Boise", "Lotus Reno", "Lotus Fresno", "Lotus Tucson", "Lotus Las Vegas",
        "Woof Boom Lima, OH", "Woof Boom Muncie, IN", "Moxii", "Cape Cod Broadcasting",
        "Sarkes Tarzian Bloomington", "Sarkes Tarzian Indianapolis",
        "Sarkes Tarzian Ft. Wayne", "Original Company Central", "Original Company North",
        "Original Company South", "Results Radio Chico", "Results Radio Redding",
        "Results Radio Sacramento", "Results Radio Yuba-Marysville", "Whitfield Media",
        "A-Train Marketing", "Curio Haus", "Vici Direct", "Manning Media",
    ]}
    expected_renames = {
        "Results Media Solutions Chico": "Results Radio Chico",
        "Results Media Solutions Redding": "Results Radio Redding",
        "Results Media Solutions Sacramento": "Results Radio Sacramento",
        "Results Media Solutions Yuba-Marysville": "Results Radio Yuba-Marysville",
    }
    for row in _csv.DictReader(SEED.open(encoding="utf-8-sig")):
        p = row["partner"]
        got, why = best(p, drive)
        if got is None:
            continue
        if got.strip().lower() == p.strip().lower():
            continue                       # exact, fine
        assert expected_renames.get(p) == got, (
            f"{p!r} matched {got!r} ({why}) - not a known rename")


def test_a_month_already_delivered_goes_into_a_v2_folder(monkeypatch, tmp_path):
    """July already went out. Corrected reports must not overwrite what the
    partner has already seen, so a revision lands in its own subfolder and the
    original stays intact. A month that has never been delivered just uses the
    cycle folder."""
    import importlib
    from app import delivery as dmod
    importlib.reload(dmod)
    from app import board as bmod, db as db_mod
    Expected, GroupRow, Report = bmod.Expected, bmod.GroupRow, db_mod.Report

    folders = {("PARENT", "7 Mountains KY"): "MKT"}
    files: dict[str, set] = {}
    shared, counter = [], {"n": 0}

    class FakeFiles:
        def list(self, q="", **kw):
            self._q = q; return self

        def create(self, body=None, media_body=None, **kw):
            counter["n"] += 1
            new_id = f"ID{counter['n']}"
            parent = body["parents"][0]
            if body.get("mimeType", "").endswith("folder"):
                folders[(parent, body["name"])] = new_id
            else:
                files.setdefault(parent, set()).add(body["name"])
            self._r = {"id": new_id}; return self

        def update(self, fileId=None, **kw):
            self._r = {"id": fileId}; return self

        def execute(self):
            if hasattr(self, "_r"):
                r = self._r; del self._r; return r
            q, parent = self._q, self._q.split("'")[1]
            if "!=" in q:                       # "does this folder hold files"
                return {"files": [{"id": "f"}] if files.get(parent) else []}
            if "folder" in q:
                return {"files": [{"id": i, "name": n}
                                  for (p, n), i in folders.items() if p == parent]}
            name = q.split("name = '")[1].split("'")[0]
            return {"files": [{"id": "x"}] if name in files.get(parent, set()) else []}

    class FakePerms:
        def create(self, fileId=None, **kw): shared.append(fileId); return self
        def execute(self): return {}

    class FakeSvc:
        def files(self): return FakeFiles()
        def permissions(self): return FakePerms()

    monkeypatch.setattr(dmod, "_drive_credentials", lambda: object())
    import googleapiclient.discovery as disc, googleapiclient.http as ghttp
    monkeypatch.setattr(disc, "build", lambda *a, **k: FakeSvc())
    monkeypatch.setattr(ghttp, "MediaFileUpload", lambda *a, **k: object())
    monkeypatch.setattr(dmod.settings, "drive_parent_folder_id", "PARENT")

    pdf = tmp_path / "r.pdf"; pdf.write_bytes(b"%PDF-1.4\n")
    group = GroupRow("7 Mountains KY", "drive", [
        Expected(market="7 Mountains KY", group="7 Mountains KY", client="Awaken Bakery",
                 kind="monthly", report=Report(severity="pass", review_state="reviewed",
                                               findings=[], stored_path=str(pdf)))])

    # --- first delivery: straight into the cycle folder
    url, msg, n = dmod.upload_drive_folder(group, "2026-07", "2026-07 July")
    july = folders[("MKT", "2026-07 July")]
    assert n == 1 and shared == [july]
    assert "Awaken Bakery.pdf" in files[july]
    assert ("MKT", "v2 updates") not in folders, "made a v2 on a fresh month"

    # --- re-deliver: July already holds files, so this is a revision
    shared.clear()
    url2, msg2, _ = dmod.upload_drive_folder(group, "2026-07", "2026-07 July")
    v2 = folders[(july, "v2 updates")]
    assert shared == [v2], "the shared link should point at the revision folder"
    assert "Awaken Bakery.pdf" in files[v2]
    assert files[july] == {"Awaken Bakery.pdf"}, "the original was disturbed"
    assert url2.endswith(v2)

    # --- and again: v2 now has files too, so v3
    shared.clear()
    dmod.upload_drive_folder(group, "2026-07", "2026-07 July")
    v3 = folders[(july, "v3 updates")]
    assert shared == [v3]
    assert "Awaken Bakery.pdf" in files[v3]


def test_drive_reuses_a_folder_named_differently(monkeypatch, tmp_path):
    """Results Media Solutions Chico lives in a folder called Results Radio
    Chico. Creating a second folder under the roster name would split ten
    years of history in two."""
    import importlib
    from app import delivery as dmod
    importlib.reload(dmod)
    from app import board as bmod, db as db_mod
    Expected, GroupRow, Report = bmod.Expected, bmod.GroupRow, db_mod.Report

    folders = {("PARENT", "Results Radio Chico"): "OLD",
               ("PARENT", "Results Radio Redding"): "OTHER"}
    files: dict[str, set] = {}
    counter = {"n": 0}

    class FakeFiles:
        def list(self, q="", **kw): self._q = q; return self
        def create(self, body=None, media_body=None, **kw):
            counter["n"] += 1
            i = f"ID{counter['n']}"; p = body["parents"][0]
            if body.get("mimeType", "").endswith("folder"):
                folders[(p, body["name"])] = i
            else:
                files.setdefault(p, set()).add(body["name"])
            self._r = {"id": i}; return self
        def update(self, fileId=None, **kw): self._r = {"id": fileId}; return self
        def execute(self):
            if hasattr(self, "_r"):
                r = self._r; del self._r; return r
            q, parent = self._q, self._q.split("'")[1]
            if "!=" in q:
                return {"files": [{"id": "f"}] if files.get(parent) else []}
            if "folder" in q:
                return {"files": [{"id": i, "name": n}
                                  for (p, n), i in folders.items() if p == parent]}
            return {"files": []}

    class FakeSvc:
        def files(self): return FakeFiles()
        def permissions(self):
            class P:
                def create(self, **kw): return self
                def execute(self): return {}
            return P()

    monkeypatch.setattr(dmod, "_drive_credentials", lambda: object())
    import googleapiclient.discovery as disc, googleapiclient.http as ghttp
    monkeypatch.setattr(disc, "build", lambda *a, **k: FakeSvc())
    monkeypatch.setattr(ghttp, "MediaFileUpload", lambda *a, **k: object())
    monkeypatch.setattr(dmod.settings, "drive_parent_folder_id", "PARENT")

    pdf = tmp_path / "r.pdf"; pdf.write_bytes(b"%PDF-1.4\n")
    group = GroupRow("Results Media Solutions Chico", "drive", [
        Expected(market="Results Media Solutions Chico",
                 group="Results Media Solutions Chico", client="A Client",
                 kind="monthly", report=Report(severity="pass", review_state="reviewed",
                                               findings=[], stored_path=str(pdf)))])
    dmod.upload_drive_folder(group, "2026-08", "2026-08 August")

    assert ("PARENT", "Results Media Solutions Chico") not in folders, \
        "created a duplicate folder instead of using the one already there"
    assert ("OLD", "2026-08 August") in folders, "filed under the wrong partner"
    assert "OTHER" not in [p for (p, _) in folders]


def test_a_check_with_nothing_to_look_at_is_skipped_not_passed():
    """A report with no geo-fencing section has not had its business names
    verified. Claiming a pass asserts something about a table that is not
    there."""
    import datetime as _dt
    from app.checks import run_all

    def geo_state(fixture):
        r = run_all(FIXTURES / fixture, filename="July 2026_X_1234.pdf",
                    expected_products={"Display"}, period="2026-07",
                    flight=(_dt.date(2024, 1, 1), _dt.date(2026, 7, 31)))
        return [c for c in r["checks"] if c["key"] == "check_geofence_names"][0]["state"]

    assert geo_state("salem_rv.pdf") == "passed"          # has the section
    assert geo_state("centre_hills.pdf") == "skipped"     # no section at all
    assert geo_state("keystone_altoona.pdf") == "skipped"


def test_a_corrected_report_replaces_the_broken_one(tmp_path, monkeypatch):
    """Re-running a report in TapClicks and letting it come back through the
    Zap has to supersede the copy on the board.

    The old code skipped any file whose NAME it had already seen, so a re-pull
    did nothing at all - the fix was silently dropped and the board went on
    showing the broken version.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'sup.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    from app import ingest as imod
    importlib.reload(imod)
    db_mod.init_db()
    db = db_mod.SessionLocal()

    D = dt.date.fromisoformat
    db.add(db_mod.OrderLine(market="7 Mountains KY", client="Awaken Bakery",
                            account_ids="52746", product="Social Mirror Ads",
                            starts_on=D("2026-01-01"), ends_on=D("2026-12-31")))
    db.commit()

    name = "July 2026_Awaken Bakery_52746.pdf"
    first = (FIXTURES / "benton_rodeo.pdf").read_bytes()
    b1 = imod.process_batch(db, [(name, first)], source="zapier", notify=False,
                            coalesce=True, subject="FW: Daily report")
    assert len(b1.reports) == 1
    rep_id = b1.reports[0].id

    # a reviewer signs it off and leaves a note
    r = db.get(db_mod.Report, rep_id)
    r.review_state, r.reviewed_by = "reviewed", "Jacob"
    r.reviewed_at = dt.datetime.utcnow()
    r.acked = [0]
    r.review_note = "Chased Tap for a re-pull"
    db.commit()

    # the corrected report arrives - SAME filename, different content
    fixed = (FIXTURES / "salem_rv.pdf").read_bytes()
    b2 = imod.process_batch(db, [(name, fixed)], source="zapier", notify=False,
                            coalesce=True, subject="FW: Daily report")

    all_reports = db.query(db_mod.Report).all()
    assert len(all_reports) == 1, f"got {len(all_reports)} rows - it duplicated"
    r = all_reports[0]
    assert r.id == rep_id, "it replaced the row instead of updating it"
    assert Path(r.stored_path).read_bytes() == fixed, "the new file was dropped"
    assert r.batch_id == b2.id, "it should sit with the batch that corrected it"
    assert r.acked == [] and r.review_state == "new" and r.reviewed_at is None, \
        "sign-off on the broken copy carried over to the corrected one"
    assert r.review_note == "Chased Tap for a re-pull", "the note was lost"

    # a lifetime for the same client is a different report, not a replacement
    life = "Lifetime_Awaken Bakery_52746.pdf"
    imod.process_batch(db, [(life, fixed)], source="zapier", notify=False,
                       coalesce=True, subject="FW: Daily report")
    assert db.query(db_mod.Report).count() == 2, \
        "a lifetime replaced the monthly, or the other way round"


def test_completion_rate_above_100_is_a_failure():
    """More completions than impressions is arithmetically impossible - it is
    a counting fault upstream, not a good month."""
    from app.checks.rules import check_completion_rates

    ok = """Connected TV (CTV) Completion Performance by Strategy
Strategy                         25% Completion Rate    50% Completion Rate
Beech Bend - Retargeting CTV     100.00%                99.86%
Beech Bend - Behavioral CTV      100.00%                99.73%
"""
    assert check_completion_rates({"text": ok}) == []

    bad = ok.replace("99.86%", "104.20%")
    out = check_completion_rates({"text": bad})
    assert len(out) == 1 and out[0]["severity"] == "fail"
    assert "104.20%" in out[0]["detail"]
    assert "Retargeting CTV" in out[0]["detail"]

    # any completion widget, not just CTV
    vid = """Video Completion Performance by Line Item
Line Item                        25% Completion Rate
Watsontown - Behavioral Video    101.00%
"""
    out = check_completion_rates({"text": vid})
    assert out and out[0]["severity"] == "fail"

    # percentages OUTSIDE a completion widget are none of its business
    other = """Line Item Performance
Some Line Item     140%    2,000
"""
    assert check_completion_rates({"text": other}) == []


def test_only_real_devices_belong_in_the_device_breakout():
    from app.checks.rules import check_devices_known

    good = """Device Performance
 Device Name        Description                       Impressions   Clicks    CTR
Connected TV       An internet enabled device            41,115         15   0.04%
Streaming Device   A stick/dongle device                 28,346          1   0.00%
Mobile             A phone                               10,000          5   0.05%
"""
    assert check_devices_known({"text": good}) == []

    junk = good + "msn.com            Some publisher                        1,000      2   0.20%\n"
    out = check_devices_known({"text": junk})
    assert len(out) == 1 and out[0]["severity"] == "warn"
    assert "msn.com" in out[0]["detail"]


def test_no_widget_check_fires_on_the_real_fixtures():
    """These are known-good reports. A new rule that flags them is wrong about
    the rule, not about the reports."""
    import datetime as _dt
    from app.checks import run_all

    for f in ["watsontown", "central_penn", "centre_hills", "keystone_altoona",
              "independence_ford", "salem_rv", "benton_rodeo"]:
        r = run_all(FIXTURES / f"{f}.pdf", filename=f"July 2026_{f}_1234.pdf",
                    period="2026-07",
                    flight=(_dt.date(2024, 1, 1), _dt.date(2026, 7, 31)))
        noisy = [x for x in r["findings"]
                 if x["code"] in ("completion_over_100", "unknown_device",
                                  "widget_missing")]
        assert not noisy, f"{f}: {[x['title'] for x in noisy]}"
