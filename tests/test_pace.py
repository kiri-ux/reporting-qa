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
    from app.pace import humanize, working_days
    assert humanize(0.4) == "about 24 minutes"
    assert humanize(1.0) == "about 1 hour"
    assert humanize(3.7) == "about 4 hours"
    assert humanize(50) == "about 2 days"
    assert humanize(None) == ""
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


def test_a_wholly_cancelled_buy_does_not_fall_back_to_the_campaign_total():
    """Kerr-Bilt's PPC was cancelled on both its line items and the spend panel
    still paced it against $2,800 a month - "the campaign total over 3 months".

    The campaign total on the row is every line item added up, cancelled ones
    included, so falling back to it put the called-off money straight back into
    the goal the live-line read had just taken out. Its money was then inside
    "All spend $412/$4,800" and the report read 91% short of a figure more than
    half of which was cancelled.
    """
    import datetime as dt
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, OrderLine
    from app.roster import ordered_for

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(OrderLine(market="M", client="Kerr-Bilt Trailers", account_ids="51323",
                     line_ids="131199,132806", product="PPC",
                     campaign="Pay-Per-Click Ads",
                     starts_on=dt.date(2026, 6, 1), ends_on=dt.date(2026, 8, 31),
                     flights=[["2026-06-01", "2026-08-31"]],
                     live=True, budget=None, total_budget=8400.0,
                     detail=[{"line_id": "131199", "canceled": True,
                              "budget": 1000.0, "starts": "2026-06-01",
                              "ends": "2026-08-01"},
                             {"line_id": "132806", "canceled": True,
                              "budget": 1400.0, "starts": "2026-08-01",
                              "ends": "2026-08-01"}]))
    db.commit()
    row = ordered_for(db, "Kerr-Bilt Trailers", "51323", "2026-08")["PPC"]
    assert row["budget"] is None and row["basis"] == ""
    assert row["stopped"] is True
    db.close(); eng.dispose()


def test_a_live_line_beside_a_cancelled_one_still_gets_its_total():
    """The fallback is only switched off when there is nothing live left. One
    cancelled line beside a live one is not a campaign that was called off."""
    import datetime as dt
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, OrderLine
    from app.roster import ordered_for

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(OrderLine(market="M", client="Acme", account_ids="1",
                     line_ids="1,2", product="PPC", campaign="Pay-Per-Click Ads",
                     starts_on=dt.date(2026, 6, 1), ends_on=dt.date(2026, 8, 31),
                     flights=[["2026-06-01", "2026-08-31"]],
                     live=True, budget=None, total_budget=9000.0,
                     detail=[{"line_id": "1", "canceled": True,
                              "starts": "2026-06-01", "ends": "2026-08-31"},
                             {"line_id": "2", "canceled": False,
                              "starts": "2026-06-01", "ends": "2026-08-31"}]))
    db.commit()
    row = ordered_for(db, "Acme", "1", "2026-08")["PPC"]
    assert row["budget"] == 3000.0            # 9,000 over the 3 months it runs
    assert row["stopped"] is False
    db.close(); eng.dispose()


def test_a_cancelled_buy_is_not_a_pacing_row():
    """Both pacing checks drop these already - a cancelled buy is not short of
    a goal that stopped being asked for the day somebody called it off - but
    the panel was building them anyway. Kerr-Bilt's cancelled PPC sat in the
    spend list as "-/$2,800 no comparison" with its money inside "All spend
    $412/$4,800", so the report read 91% short of a figure more than half of
    which had been cancelled."""
    from app.checks.served import pacing_rows
    text = ("Line Item Performance\n"
            " Kerr-Bilt - Performance Max   1,000   10   1.00%\n"
            "Spend\n Performance Max   $412.00\n PPC   $0.00\n")
    ordered = {
        "Performance Max": {"budget": 2000.0, "impressions": None, "basis": ""},
        "PPC": {"budget": 2800.0, "impressions": None, "basis": "",
                "stopped": True},
    }
    rows = pacing_rows(text, ordered)
    assert "PPC" not in [r["product"] for r in rows]
    total = [r for r in rows if r["product"] == "All spend"]
    assert not total or total[0]["ordered"] == 2000.0, \
        "the cancelled money is still in the total"
    # And it is only the cancelled one that goes.
    assert "Performance Max" in [r["product"] for r in rows]
    ordered["PPC"].pop("stopped")
    assert "PPC" in [r["product"] for r in pacing_rows(text, ordered)]


# ------------------------------------------- a monthly goal is a rate, not a
# ------------------------------------------- target for the calendar month
def _kermit_rows(period="2026-08"):
    """Kermit Celebration Days, August 2026. Four products, two launch dates,
    and a report the panel called 62% short while every one of them was on
    pace. Order 55727: Display, Mobile Conquesting and Online Audio from the
    20th, CTV and Video from the 27th."""
    import datetime as dt

    import app.checks.served as S
    from app.checks.served import pacing_rows

    def want(imps, day, days):
        return {"impressions": float(imps), "budget": None, "basis": "",
                "started": dt.date(2026, 8, day), "days": days}

    ordered = {"CTV, Video": want(160_000, 27, 5),
               "Display": want(150_000, 20, 12),
               "Mobile Conquesting": want(100_000, 20, 12),
               "Online Audio": want(30_000, 20, 12)}
    real = S.served_impressions
    S.served_impressions = lambda _t: {
        "by_product": {"CTV": 32_365.0, "Video": 0.0, "Display": 59_323.0,
                       "Mobile Conquesting": 39_222.0, "Online Audio": 13_007.0},
        "total": 168_637.0, "unattributed": 0.0, "flat": 0.0}
    try:
        rows = pacing_rows("Line Item Performance\n", ordered, period=period)
    finally:
        S.served_impressions = real
    return {r["product"]: r for r in rows}


def test_a_part_month_is_paced_against_the_days_it_actually_ran():
    rows = _kermit_rows()
    # 160,000 a month over 5 of 31 days is 25,806, and 32,365 is ahead of it.
    assert round(rows["CTV, Video"]["ordered"]) == 25_806
    assert rows["CTV, Video"]["pace"] > 0
    for name, goal in (("Display", 58_065), ("Mobile Conquesting", 38_710),
                       ("Online Audio", 11_613)):
        assert round(rows[name]["ordered"]) == goal, name
        assert abs(rows[name]["pace"]) < 15, name
    # The whole report flips from 62% short to comfortably over.
    total = rows["All impressions"]
    assert round(total["ordered"]) == 134_194
    assert total["pace"] > 0


def test_the_row_says_what_the_goal_was_cut_from():
    """The figure being divided by is on neither the order nor the report, so
    the row has to say where it came from."""
    note = _kermit_rows()["Display"]["month_note"]
    assert "150,000 a month" in note
    assert "12 of 31 days" in note and "Aug 20" in note
    assert _kermit_rows()["All impressions"]["month_note"] == \
        "440,000 a month across the products above"


def test_a_full_month_is_left_alone():
    """A line that ran the whole month is already being measured against the
    right figure, and cutting it would be arithmetic for its own sake."""
    import datetime as dt

    import app.checks.served as S
    from app.checks.served import pacing_rows

    ordered = {"Display": {"impressions": 150_000.0, "budget": None, "basis": "",
                           "started": dt.date(2026, 8, 1), "days": 31}}
    real = S.served_impressions
    S.served_impressions = lambda _t: {"by_product": {"Display": 60_000.0},
                                       "total": 60_000.0, "unattributed": 0.0,
                                       "flat": 0.0}
    try:
        row = pacing_rows("x", ordered, period="2026-08")[0]
    finally:
        S.served_impressions = real
    assert row["ordered"] == 150_000.0
    assert row["in_month"] is None and row["month_note"] == ""


def test_a_lifetime_is_not_pro_rated():
    """A lifetime is the whole campaign. It carries no day count, so there is
    nothing to cut the goal by and no period is passed for it."""
    import app.checks.served as S
    from app.checks.served import pacing_rows

    ordered = {"Display": {"impressions": 900_000.0, "budget": None,
                           "basis": "6 months at the monthly figure",
                           "started": None, "days": None}}
    real = S.served_impressions
    S.served_impressions = lambda _t: {"by_product": {"Display": 850_000.0},
                                       "total": 850_000.0, "unattributed": 0.0,
                                       "flat": 0.0}
    try:
        row = pacing_rows("x", ordered, period="2026-08")[0]
    finally:
        S.served_impressions = real
    assert row["ordered"] == 900_000.0 and row["month_note"] == ""


def test_spend_is_pro_rated_the_same_way():
    """A month's budget is a rate too. A line twelve days old spending twelve
    days of it is not 61% under budget."""
    import datetime as dt

    from app.checks.rules import check_pacing

    ctx = {"period": "2026-08", "is_lifetime": False,
           "budgets": {"PPC": 3100.0},
           "ordered": {"PPC": {"started": dt.date(2026, 8, 20), "days": 12}},
           "text": "PPC Spend\n Total Spend   $1,200.00\n"}
    import app.checks.spend as SP
    real = SP.report_spend
    SP.report_spend = lambda _t: {"PPC": 1200.0}
    try:
        # 3,100 a month over 12 of 31 days is 1,200 - exactly on budget.
        assert check_pacing(ctx) == []
        SP.report_spend = lambda _t: {"PPC": 300.0}
        out = check_pacing(ctx)
        assert len(out) == 1 and "75% under" in out[0]["title"]
        assert "12 of 31 days" in out[0]["detail"]
        assert "$3,100.00 a month" in out[0]["detail"]
    finally:
        SP.report_spend = real
