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
