"""How fast reports are arriving, and what that means for the ones still out.

"763 not received" does not answer the question anybody has, which is whether
that is a morning's work or the rest of the week.
"""
import datetime as dt

import pytest


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


NOW = dt.datetime(2026, 8, 25, 14, 0, 0)


def _arrived(db, when: dt.datetime, n: int, period="2026-07"):
    """n reports landing together, the way a batch actually arrives."""
    from app.db import Batch, Report
    b = Batch(received_at=when, period=period, market="m", status="done")
    db.add(b)
    db.flush()
    for i in range(n):
        db.add(Report(batch_id=b.id, period=period, client=f"c{b.id}-{i}",
                      account_ids="1", market="m", filename="f.pdf",
                      severity="pass", findings=[], checks=[], acked=[]))
    db.commit()


def test_nothing_received_makes_no_claim(db):
    from app.pace import pace
    out = pace(db, "2026-07", outstanding=763, now=NOW)
    assert out["received"] == 0 and out["rate"] is None and out["eta"] is None


def test_a_steady_rate_projects_the_rest(db):
    """20 an hour for three hours, 100 still out: about five hours."""
    from app.pace import pace
    for h in (1, 2, 3):
        _arrived(db, NOW - dt.timedelta(hours=h), 20)
    out = pace(db, "2026-07", outstanding=100, now=NOW)
    assert out["received"] == 60
    assert 15 <= out["rate"] <= 25
    assert 4 <= out["hours"] <= 7
    assert out["eta"] > NOW


def test_the_shortest_window_with_enough_in_it_is_used(db):
    """A burst ten minutes ago must not be read as the standing rate, but a
    genuinely busy last hour should be."""
    from app.pace import pace
    _arrived(db, NOW - dt.timedelta(minutes=20), 40)
    _arrived(db, NOW - dt.timedelta(hours=30), 10)
    out = pace(db, "2026-07", outstanding=100, now=NOW)
    assert out["basis"] == "the last hour"


def test_a_thin_recent_window_is_skipped_for_a_longer_one(db):
    """Three arrivals in the last hour is not a rate."""
    from app.pace import pace
    _arrived(db, NOW - dt.timedelta(minutes=30), 3)
    for h in range(4, 20):
        _arrived(db, NOW - dt.timedelta(hours=h), 5)
    out = pace(db, "2026-07", outstanding=100, now=NOW)
    assert out["basis"] in ("the last 12 hours", "the last day")


def test_a_window_reaching_back_before_the_first_report_is_not_divided_by_it(db):
    """Two hours in, a "last day" window must not divide by twenty-four - that
    would report a twelfth of the real rate and a wildly long estimate."""
    from app.pace import pace
    _arrived(db, NOW - dt.timedelta(hours=2), 40)
    out = pace(db, "2026-07", outstanding=40, now=NOW)
    assert out["rate"] >= 15                     # 40 over ~2h, not over 24h
    assert out["hours"] < 4


def test_nothing_outstanding_makes_no_claim(db):
    from app.pace import pace
    _arrived(db, NOW - dt.timedelta(hours=1), 20)
    assert pace(db, "2026-07", outstanding=0, now=NOW)["eta"] is None


def test_only_this_cycle_is_counted(db):
    from app.pace import pace
    _arrived(db, NOW - dt.timedelta(hours=1), 20, period="2026-06")
    out = pace(db, "2026-07", outstanding=100, now=NOW)
    assert out["received"] == 0


# ------------------------------------------------------------------ wording
def test_a_rough_projection_is_not_written_like_a_measurement():
    """"3.7 hours" makes an extrapolation from a bursty signal look like a
    reading off an instrument."""
    from app.pace import humanise, working_days
    assert humanise(0.4) == "about 24 minutes"
    assert humanise(1.0) == "about 1 hour"
    assert humanise(3.7) == "about 4 hours"
    assert humanise(50) == "about 2 days"
    assert humanise(None) == ""
    # and the same span in working hours, because nothing arrives overnight
    assert working_days(16) == "2 working days at that rate"
    assert working_days(3) == ""


def test_a_campaign_total_with_no_monthly_figure_is_spread_over_its_months():
    """Kerr-Bilt Trailers' Performance Max carries $20,000 for the whole
    campaign and nothing per month, so the spend row read "-/- no comparison"
    while the impressions rows above it were paced against real monthly goals.
    Two units on one panel, and the one the client is billed on was the blank.

    The lifetime panel already multiplies a monthly goal out across the flight
    and says so. This is the same thing the other way up.
    """
    import datetime as dt
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, OrderLine
    from app.roster import ordered_for

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(OrderLine(market="Lockwood Denison", client="Kerr-Bilt Trailers",
                     account_ids="53901", line_ids="129648",
                     product="Performance Max", campaign="Performance Max Ads",
                     starts_on=dt.date(2026, 5, 15), ends_on=dt.date(2026, 12, 31),
                     flights=[["2026-05-15", "2026-12-31"]],
                     live=True, budget=None, total_budget=20000.0))
    db.commit()

    got = ordered_for(db, "Kerr-Bilt Trailers", "53901", "2026-07")
    row = got["Performance Max"]
    assert row["budget"] == 2500.0            # 20,000 over the 8 months it runs
    assert "campaign total over 8 months" in row["basis"]
    db.close(); eng.dispose()


def test_a_real_monthly_figure_is_never_overwritten_by_a_derived_one():
    import datetime as dt
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, OrderLine
    from app.roster import ordered_for

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(OrderLine(market="M", client="Acme", account_ids="1", product="PPC",
                     starts_on=dt.date(2026, 1, 1), ends_on=dt.date(2026, 12, 31),
                     flights=[["2026-01-01", "2026-12-31"]],
                     live=True, budget=900.0, total_budget=20000.0))
    db.commit()
    got = ordered_for(db, "Acme", "1", "2026-07")
    assert got["PPC"]["budget"] == 900.0 and not got["PPC"]["basis"]
    db.close(); eng.dispose()
