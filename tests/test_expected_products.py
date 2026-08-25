"""expected_products has to respect the report's month.

The real report that exposed this: Ashley HomeStore, fourteen line items over
six years in one order and a single live line in another. Mobile Conquesting's
last line ended on New Year's Eve 2025, and the July 2026 report was failed for
not carrying it.
"""
import datetime as dt
import importlib

import pytest


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    s = db_mod.SessionLocal()
    yield s
    s.close()


CLIENT = "Ashley HomeStore - Blacksburg, Virginia"


def _line(db, product, starts, ends):
    from app.db import OrderLine
    db.add(OrderLine(market="Conquest Digital Solutions", client=CLIENT,
                     account_ids="8485", line_ids="86012", campaign=product,
                     product=product,
                     starts_on=dt.date.fromisoformat(starts) if starts else None,
                     ends_on=dt.date.fromisoformat(ends) if ends else None))


def _exp(db, period="2026-07"):
    from app.roster import expected_products
    return expected_products(db, CLIENT, "8485", period=period)


def test_a_product_that_ended_before_the_period_is_not_expected(db):
    _line(db, "Mobile Conquesting Display & Video", "2024-05-01", "2025-12-31")
    _line(db, "Social Mirror Ads", "2026-01-02", "2026-09-30")
    db.commit()
    assert _exp(db) == {"Social Mirror Ads"}


def test_a_product_that_starts_after_the_period_is_not_expected(db):
    _line(db, "Social Mirror Ads", "2026-09-01", "2026-12-31")
    db.commit()
    assert _exp(db) == set()


def test_everything_ended_means_nothing_owed_not_unknown(db):
    """An empty set passes the check. None would skip it and claim nothing."""
    _line(db, "Mobile Conquesting Display & Video", "2024-05-01", "2025-12-31")
    db.commit()
    out = _exp(db)
    assert out == set() and out is not None


def test_a_client_not_on_the_order_list_still_returns_none(db):
    _line(db, "Social Mirror Ads", "2026-01-02", "2026-09-30")
    db.commit()
    from app.roster import expected_products
    assert expected_products(db, "Someone Else Entirely", "999",
                             period="2026-07") is None


def test_an_open_ended_flight_counts(db):
    """No end date means still running, not ended."""
    _line(db, "CTV + Video Ads", "2024-01-01", None)
    db.commit()
    assert _exp(db) == {"CTV + Video Ads"}


def test_a_flight_that_ends_inside_the_period_still_counts(db):
    """An order ending mid-month ran that month and owes a report."""
    _line(db, "Social Mirror Ads", "2026-01-02", "2026-07-15")
    db.commit()
    assert _exp(db) == {"Social Mirror Ads"}


def test_no_period_keeps_the_old_behaviour(db):
    """Callers that cannot say which month still get every product."""
    _line(db, "Mobile Conquesting Display & Video", "2024-05-01", "2025-12-31")
    db.commit()
    assert _exp(db, period=None) == {"Mobile Conquesting Display & Video"}
