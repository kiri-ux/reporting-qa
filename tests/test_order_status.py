"""Which order line items get a report.

The bug this exists for: order 55216 sat at "IO Pending Launch" while the line
item under it was "IO Live". The importer judged on the order header, dropped
the whole order, never put the product in the expected set - and then failed
the report for carrying a product with no live order. The header was the only
thing that was wrong.
"""
import datetime as dt
import io

import pytest
from sqlalchemy import select


@pytest.fixture()
def db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


HEADER = ("orders_id,id,orders_status,status,client,product,client_business_unit,"
          "orders_start_date,orders_end_date,start_date,end_date,campaign_manager\n")


def _csv(*rows) -> bytes:
    return (HEADER + "".join(rows)).encode()


def _row(order_status, line_status, product="Social Mirror Ads",
         start="2026-01-01", end="2026-12-31", order_id="55216", line_id="1"):
    return (f"{order_id},{line_id},{order_status},{line_status},Acme Co,{product},"
            f"Market One,{start},{end},{start},{end},Stacy Sroka (s@vicimediainc.com)\n")


def _import(db, blob, period="2026-07"):
    from app.orders_io import import_io_export
    return import_io_export(db, blob, period=period)


def _products(db):
    from app.db import OrderLine
    return sorted({l.product for l in db.query(OrderLine).all()})


def test_a_live_line_under_a_pending_order_is_kept(db):
    """Order 55216, exactly as it came in."""
    res = _import(db, _csv(_row("IO Pending Launch", "IO Live")))
    assert res["kept"] == 1
    assert _products(db) == ["Social Mirror"]
    assert res["header_overruled"] == 1


def test_the_order_header_still_decides_when_the_line_says_nothing(db):
    res = _import(db, _csv(_row("IO Live", "")))
    assert res["kept"] == 1
    assert _import(db, _csv(_row("IO Pending Launch", "")))["kept"] == 0


def test_a_line_item_can_only_rescue_an_order_never_drop_one(db):
    """W&L Subaru's Meta lines sit at "IO Paused" under a live order. A line
    paused halfway through the month still delivered for half of it, so the
    rule only ever adds line items - it never removes one that was expected."""
    assert _import(db, _csv(_row("IO Live", "IO Paused")))["kept"] == 1
    assert _import(db, _csv(_row("IO Live", "IO Pending Launch")))["kept"] == 1


def test_a_cancelled_line_is_kept_but_marked(db):
    """IT USED TO BE THROWN AWAY, and that is why a report carrying the product
    read as carrying one nobody ordered. Roto Rooter's PPC was cancelled on 28
    July and its July report was failed for showing it.

    A cancelled buy is not OWED on the report. It is not a surprise there
    either - it ran, it was stopped, and the data is real."""
    from app.db import OrderLine
    assert _import(db, _csv(_row("IO Live", "Cancelled")))["kept"] == 1
    row = db.scalars(select(OrderLine)).first()
    assert row.canceled is True and row.live is False


def test_a_cancelled_order_marks_its_lines_whatever_they_say(db):
    """Cancelling an order is a deliberate act, unlike a header nobody moved."""
    from app.db import OrderLine
    assert _import(db, _csv(_row("Cancelled", "IO Live")))["kept"] == 1
    assert db.scalars(select(OrderLine)).first().canceled is True


def test_a_cancelled_line_is_not_owed_and_not_a_surprise(db):
    from app.db import OrderLine
    from app.roster import expected_products, quiet_products
    _import(db, _csv(_row("IO Live", "Cancelled")))
    row = db.scalars(select(OrderLine)).first()
    exp = expected_products(db, row.client, row.account_ids, period="2026-07")
    quiet = quiet_products(db, row.client, row.account_ids, period="2026-07")
    assert row.product not in (exp or set())
    assert row.product in quiet


def test_a_cancelled_line_owes_a_lifetime_but_not_a_monthly(db):
    """Cancelling does not mean it never ran - it ran and was stopped, so the
    campaign still needs closing out. What it does not need is another month's
    report on the month it was cancelled in."""
    from app.board import expected_for
    _import(db, _csv(_row("IO Live", "Cancelled")))
    kinds = sorted(e.kind for e in expected_for(db, "2026-07"))
    assert kinds == ["lifetime"]


def test_an_rfp_is_never_reported_from_either_level(db):
    assert _import(db, _csv(_row("RFP", "IO Live")))["kept"] == 0
    assert _import(db, _csv(_row("IO Live", "RFP")))["kept"] == 0


def test_io_complete_counts_as_having_run(db):
    assert _import(db, _csv(_row("IO Complete", "IO Complete")))["kept"] == 1
    assert _import(db, _csv(_row("IO Pending Launch", "IO Complete")))["kept"] == 1


def test_the_date_rules_still_apply_to_a_line_the_header_would_have_dropped(db):
    """Trusting the line item is not a license to ignore its flight."""
    res = _import(db, _csv(_row("IO Pending Launch", "IO Live",
                                start="2024-01-01", end="2025-07-08")))
    assert res["kept"] == 0
    assert "ended before the period" in res["skipped"]


def test_the_skip_reason_names_both_statuses(db):
    """So the next person can see which of the two was the problem."""
    res = _import(db, _csv(_row("IO Pending Launch", "IO Pending Launch")))
    assert any("IO Pending Launch" in k and "line item" in k
               for k in res["skipped"]), res["skipped"]


def test_the_count_of_overruled_headers_is_reported(db):
    """A handful is housekeeping. A hundred is a process problem, and the
    import should be able to say which."""
    res = _import(db, _csv(
        _row("IO Pending Launch", "IO Live", line_id="1"),
        _row("IO Pending Launch", "IO Live", product="Display Ads", line_id="2"),
        _row("IO Live", "IO Live", product="Video Ads", line_id="3")))
    assert res["kept"] == 3 and res["header_overruled"] == 2


def test_an_rfp_is_caught_by_the_order_type_not_only_the_status(db):
    """ORDER 51217. Order Type "Request for Proposal", Order Status
    "Cancelled" - so nothing said RFP anywhere the import was looking, and a
    proposal that was never sold went on the board owed a lifetime report."""
    head = ("orders_id,id,orders_status,status,order type,client,product,"
            "client_business_unit,orders_start_date,orders_end_date,"
            "start_date,end_date,campaign_manager\n")
    row = ("51217,120588,Cancelled,Cancelled,Request for Proposal,Glasgow Garage,"
           "Connected TV Ads,7 Mountains KY,2026-01-15,2026-07-31,"
           "2026-01-15,2026-07-31,Someone\n")
    res = _import(db, (head + row).encode())
    assert res["kept"] == 0
    assert "RFP" in res["skipped"]


def test_a_real_insertion_order_is_still_kept(db):
    """The type column must only ever drop proposals."""
    head = ("orders_id,id,orders_status,status,order type,client,product,"
            "client_business_unit,orders_start_date,orders_end_date,"
            "start_date,end_date,campaign_manager\n")
    row = ("50760,119158,IO Live,IO Live,Insertion Order,The Vincent Group,"
           "Connected TV Ads,7 Mountains KY,2026-01-15,2026-07-15,"
           "2026-01-15,2026-07-15,Someone\n")
    assert _import(db, (head + row).encode())["kept"] == 1


# ------------------------------------------- only the order-database exports
def test_the_sync_takes_only_the_order_database_files():
    """The bucket holds more than the orders now, and "every CSV under the
    prefix" would merge whatever else lands there into the order list.

    Punctuation is ignored on purpose: the files arrive as
    "ordersdb7moupa_20260826_1508_0.csv" while the naming convention is written
    down as "orders-db-", and a filter that reads those as two different things
    is a silent empty sync waiting to happen.
    """
    from app.orders_s3 import _name_matches
    assert _name_matches("io/ordersdb7moupa_20260826_1508_0.csv")
    assert _name_matches("io/orders-db-anne_20260826.csv")
    assert _name_matches("io/ORDERS_DB_foo.CSV")
    assert not _name_matches("io/roster.csv")
    assert not _name_matches("io/serving-2026-07.csv")


def test_a_repeated_column_is_read_first_non_empty():
    """The new export repeats months_running thirty-four times, social_platforms
    five, total_campaign_impressions four. Across both sample files - 527 rows -
    no repeated field ever carries two different values on one row, so taking
    the first non-empty is the whole answer."""
    from app.orders_io import _open_source
    csv = ("client,orders_id,id,product,total_campaign_impressions,"
           "total_campaign_impressions,start_date,start_date,end_date,end_date\n"
           "Acme,1,10,Display Ads,,250000,,2026-01-01,2026-12-31,\n")
    row = next(iter(_open_source(csv.encode())))
    assert row["total_campaign_impressions"] == "250000"
    assert row["start_date"] == "2026-01-01"
    assert row["end_date"] == "2026-12-31"


def test_the_new_exports_budget_columns_are_read():
    """The orders-db export dropped monthly_campaign_budget and carries the
    same figure as budget_combined, with client_monthly_budget holding it
    again. "Client" in that name is misleading: order 36184 has three line
    items and they read 1500, 500 and 500, which is the LINE ITEM's own
    monthly budget."""
    from app.orders_io import _open_source, normalize_header
    assert normalize_header("budget_combined") == "monthly_campaign_budget"
    assert normalize_header("client_monthly_budget") == "monthly_campaign_budget"
    assert normalize_header("total_budget_combined") == "total_campaign_budget"
    assert normalize_header("client_total_budget") == "total_campaign_budget"

    csv = ("client,orders_id,id,product,budget_combined,client_monthly_budget,"
           "total_budget_combined,client_total_budget\n"
           "Acme,1,10,Display Ads,500,500.00,49500,49500.00\n")
    row = next(iter(_open_source(csv.encode())))
    assert row["monthly_campaign_budget"] == "500"
    assert row["total_campaign_budget"] == "49500"


def test_the_orders_db_export_loads_end_to_end():
    """Not just parsed - imported, with the money landing where pacing reads
    it. Runaway Tractor: $1,500 a month, $136,500 for the campaign."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, OrderLine
    from app.orders_io import import_io_export

    head = ("client_business_unit,orders_status,client,orders_id,product,id,"
            "status,orders_type,orders_start_date,start_date,end_date,"
            "orders_end_date,budget_combined,client_monthly_budget,"
            "total_budget_combined,monthly_campaign_impressions\n")
    # The daily grain, and the dates carry a time now.
    rows = "".join(
        "Stephens Medford OR,IO Live,Runaway Tractor,7820,"
        "Mobile Conquesting Display & Video Ads,13941,IO Live,Insertion Order,"
        "2019-06-10 21:00:00,2019-06-10 21:00:00,2026-12-31 21:00:00,"
        "2026-12-31 21:00:00,1500,1500.00,136500,100000\n" for _ in range(5))

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    res = import_io_export(db, (head + rows).encode(), period="2026-07")
    assert res["rows_read"] == 5 and res["duplicate_rows"] == 4
    line = db.query(OrderLine).one()
    assert line.budget == 1500.0 and line.total_budget == 136500.0
    assert line.impressions == 100000.0
    assert line.starts_on.isoformat() == "2019-06-10"
    db.close(); eng.dispose()


# ------------------------------------------------- the disk, which fills up
def test_an_abandoned_order_download_is_swept(tmp_path, monkeypatch):
    """The tempdir is removed on both the success and the failure path, and
    neither runs if the process is killed - a deploy, a restart, the OOM killer
    - which is exactly when a sync is most likely to be halfway through. Every
    one of those leaves the whole export on the disk forever."""
    import os
    import time
    from app import orders_s3 as s3
    monkeypatch.setattr(s3.settings, "data_dir", tmp_path)

    old = tmp_path / "orders-abandoned"
    old.mkdir()
    (old / "000-export.csv").write_bytes(b"x" * 5000)
    long_ago = time.time() - 7200
    os.utime(old, (long_ago, long_ago))

    # A sync running right now in the other worker keeps its own files.
    live = tmp_path / "orders-running-now"
    live.mkdir()
    (live / "000-export.csv").write_bytes(b"y" * 100)

    assert s3.sweep_leftovers() == 5000
    assert not old.exists()
    assert live.exists(), "a download in progress is not somebody else's mess"


def test_the_disk_is_reported_before_it_bites():
    """A full disk does not announce itself: it comes back as an unrelated
    write failing somewhere else."""
    from pathlib import Path
    from app.orders_s3 import disk_free, disk_note
    free, total = disk_free()
    assert total > 0 and 0 < free <= total
    assert "GB free of" in disk_note()
    html = (Path(__file__).resolve().parents[1] / "app" / "templates"
            / "orders.html").read_text()
    assert "The disk is {{ disk.pct }}% full" in html
    assert "Deleting files in the S3 bucket does not" in html


def test_no_module_refers_to_a_name_that_does_not_exist():
    """A sync crashed on "NameError: name 'log' is not defined" - a logging
    line added to a code path only real S3 credentials reach, so no test ran
    it and nothing caught a typo Python cannot see until it executes.

    Undefined names only. Unused imports and shadowed locals are style.
    """
    import subprocess
    import sys
    from pathlib import Path
    pytest.importorskip("pyflakes")
    root = Path(__file__).resolve().parents[1]
    files = sorted(str(p) for p in (root / "app").rglob("*.py"))
    out = subprocess.run([sys.executable, "-m", "pyflakes", *files],
                         capture_output=True, text=True).stdout
    bad = [ln for ln in out.splitlines()
           if "undefined name" in ln or "may be undefined" in ln]
    assert not bad, "\n".join(bad)
