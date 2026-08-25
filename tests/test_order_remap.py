"""When the mapping code changes, the loaded orders are stale.

The order export is not stored. Every line item is turned into a product name
on the way in and only the product is kept, so a fix to the mapping does
nothing for the orders already in the database. And because the S3 sync skips
an object whose ETag has not changed, "nothing" means forever: the file has not
changed, so it is never read again, so the wrong answer stands.

That is not a hypothetical. SKyPAC's live TikTok order read as Video, the fix
went out, and the board still said Video - because the export was untouched.
"""
import datetime as dt

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.db import Base, Batch, OrderLine, Report
from app.orders_io import _products_by_client, _restamp_changed_clients


@pytest.fixture()
def db():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    yield s
    s.close()


def _report(db, client, *, stamp="abc123"):
    b = Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1))
    db.add(b)
    db.flush()
    r = Report(batch_id=b.id, client=client, filename=f"{client}.pdf",
               period="2026-07", rules_version=stamp)
    db.add(r)
    db.commit()
    return r


def _line(db, client, product):
    db.add(OrderLine(market="7 Mountains KY", client=client, product=product,
                     account_ids="51251"))
    db.commit()


def test_a_client_whose_products_changed_is_queued_for_a_recheck(db):
    rep = _report(db, "Southern Kentucky Performing Arts Center SKyPAC")
    _line(db, "Southern Kentucky Performing Arts Center SKyPAC", "Video")
    before = _products_by_client(db)

    db.query(OrderLine).delete()
    _line(db, "Southern Kentucky Performing Arts Center SKyPAC", "TikTok")

    assert _restamp_changed_clients(db, before) == 1
    db.refresh(rep)
    assert rep.rules_version == ""      # the sweep re-reads it from here


def test_a_client_whose_products_did_not_move_is_left_alone(db):
    """Every sync must not re-read every PDF in the cycle.

    The export changes daily during a cycle and almost none of those changes
    touch a given client. Restamping everything would turn a mapping fix into
    a twelve-hundred-PDF re-read, every day, for the same answer.
    """
    rep = _report(db, "Awaken Bakery")
    _line(db, "Awaken Bakery", "Social Mirror")
    before = _products_by_client(db)

    db.query(OrderLine).delete()
    _line(db, "Awaken Bakery", "Social Mirror")

    assert _restamp_changed_clients(db, before) == 0
    db.refresh(rep)
    assert rep.rules_version == "abc123"


def test_a_client_that_dropped_off_the_order_list_still_counts(db):
    """Losing every order changes the answer as much as gaining one."""
    rep = _report(db, "Gone Motors")
    _line(db, "Gone Motors", "Display")
    before = _products_by_client(db)
    db.query(OrderLine).delete()
    db.commit()

    assert _restamp_changed_clients(db, before) == 1
    db.refresh(rep)
    assert rep.rules_version == ""


def test_client_names_are_matched_loosely(db):
    """"Service One Credit Union (1)" and "Service One Credit Union" are one
    client, and the check that reads them as two is the one that quietly
    skips the report that needed re-reading."""
    rep = _report(db, "Service One Credit Union (1)")
    _line(db, "Service One Credit Union", "Video")
    before = _products_by_client(db)
    db.query(OrderLine).delete()
    _line(db, "Service One Credit Union", "DOOH")

    assert _restamp_changed_clients(db, before) == 1
    db.refresh(rep)
    assert rep.rules_version == ""


def test_the_map_version_moves_when_the_mapping_code_does():
    """It is a hash of the source, not a number somebody remembers to bump -
    because the one deploy it gets forgotten on is the one that needed it."""
    from app.version import product_map_version
    v = product_map_version()
    assert v and len(v) == 16
    assert v == product_map_version()          # stable within a process


# ------------------------------------------------ the re-read happens by itself
def test_the_sweeper_leaves_the_orders_alone_when_the_mapping_is_current(monkeypatch):
    """Otherwise every deploy re-downloads an 850 MB export for nothing."""
    from app import recheck as rmod
    from app.db import OrderSync
    from app.version import product_map_version

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    s.add(OrderSync(ok=True, state="done", map_version=product_map_version(),
                    synced_at=dt.datetime.utcnow()))
    s.commit()
    s.close()

    called = []
    monkeypatch.setattr(rmod, "SessionLocal", Session)
    monkeypatch.setattr("app.orders_s3.sync", lambda *a, **k: called.append(1))
    rmod._remap_orders_if_stale()
    assert called == []


def test_the_sweeper_re_reads_the_orders_when_the_mapping_moved(monkeypatch):
    from app import recheck as rmod
    from app.db import OrderSync

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    s.add(OrderSync(ok=True, state="done", map_version="an-older-one",
                    synced_at=dt.datetime.utcnow()))
    s.commit()
    s.close()

    called = []
    monkeypatch.setattr(rmod, "SessionLocal", Session)
    monkeypatch.setattr("app.orders_s3.begin_sync",
                        lambda db: type("C", (), {"id": 1})())
    monkeypatch.setattr("app.orders_s3.sync",
                        lambda db, **k: called.append(k) or type("R", (), {"message": "ok"})())
    rmod._remap_orders_if_stale()
    assert called and called[0].get("force") is True


def test_an_unreachable_bucket_does_not_stop_the_report_sweep(monkeypatch):
    """The re-read runs first. If it can throw, it takes the sweep with it."""
    from app import recheck as rmod
    from app.db import OrderSync

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    s = Session()
    s.add(OrderSync(ok=True, state="done", map_version="older",
                    synced_at=dt.datetime.utcnow()))
    s.commit()
    s.close()

    monkeypatch.setattr(rmod, "SessionLocal", Session)
    monkeypatch.setattr("app.orders_s3.begin_sync",
                        lambda db: (_ for _ in ()).throw(RuntimeError("no s3")))
    rmod._remap_orders_if_stale()          # must not raise


# ------------------------------------------- saying so, on the page she works on
def test_the_board_says_when_the_orders_were_read_by_older_code(monkeypatch):
    """The export is parsed once and only the answer is kept, so an import fix
    does nothing until the file is read again. The sweeper does it on its own,
    but while it has not the board goes on showing the old answer with no sign
    anywhere that it is doing so - and that silence turned one bug into three
    rounds of screenshots."""
    from app import main as mmod
    from app.db import OrderSync
    from app.version import product_map_version

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()

    s.add(OrderSync(ok=True, state="done", map_version="an-older-one",
                    synced_at=dt.datetime.utcnow()))
    s.commit()
    assert mmod._orders_stale(s) is True

    s.query(OrderSync).delete()
    s.add(OrderSync(ok=True, state="done", map_version=product_map_version(),
                    synced_at=dt.datetime.utcnow()))
    s.commit()
    assert mmod._orders_stale(s) is False


def test_no_orders_loaded_is_not_stale():
    """"Nothing loaded" is a different problem with its own message on the
    orders page. It is not this banner's job."""
    from app import main as mmod
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    assert mmod._orders_stale(s) is False


def test_a_failed_sync_is_not_reported_as_stale():
    from app import main as mmod
    from app.db import OrderSync
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    s.add(OrderSync(ok=False, state="done", map_version="",
                    synced_at=dt.datetime.utcnow(), message="S3 unreachable"))
    s.commit()
    assert mmod._orders_stale(s) is False
