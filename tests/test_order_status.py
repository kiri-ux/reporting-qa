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
