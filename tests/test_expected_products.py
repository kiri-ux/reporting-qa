"""expected_products has to respect the report's month.

The real report that exposed this: Ashley HomeStore, fourteen line items over
six years in one order and a single live line in another. Mobile Conquesting's
last line ended on New Year's Eve 2025, and the July 2026 report was failed for
not carrying it.
"""
import datetime as dt

import pytest


@pytest.fixture()
def db(tmp_path):
    """A throwaway database built from the live metadata.

    Reloading app.db here would re-register its mappers, and every test that
    ran afterwards without reloading would fail to resolve 'Batch'. Building a
    second engine off the same Base leaves the module alone.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


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


def test_no_period_keeps_the_old_behavior(db):
    """Callers that cannot say which month still get every product."""
    _line(db, "Mobile Conquesting Display & Video", "2024-05-01", "2025-12-31")
    db.commit()
    assert _exp(db, period=None) == {"Mobile Conquesting Display & Video"}


# ------------------------------------------- Blair Regional YMCA, order 31449
#
# Social Mirror CTV ran to 30 June 2026 and starts again on 1 August. Merged
# across those two orders that is one flight from 2025 to December 2026, and
# July - a month in which none ran at all - sits inside it. The July report was
# failed twice for it: once for the product, once for the CTV publishers widget
# the product would have owed.
import datetime as _dt

from app.roster import _ran_during


class _Line:
    def __init__(self, starts, ends, flights=None):
        self.starts_on = starts
        self.ends_on = ends
        self.flights = flights or []


def test_a_gap_between_two_flights_is_not_running():
    line = _Line(_dt.date(2025, 1, 3), _dt.date(2026, 12, 31),
                 [["2025-01-03", "2026-06-30"], ["2026-08-01", "2026-12-31"]])
    assert _ran_during(line, "2026-06") is True
    assert _ran_during(line, "2026-08") is True
    assert _ran_during(line, "2026-07") is False, "July is the gap"


def test_the_merged_span_is_what_got_this_wrong():
    """The same line without its windows: the old answer, kept as the fallback
    for rows loaded before the windows were recorded."""
    line = _Line(_dt.date(2025, 1, 3), _dt.date(2026, 12, 31))
    assert _ran_during(line, "2026-07") is True


def test_one_continuous_flight_still_covers_its_months():
    line = _Line(_dt.date(2026, 4, 1), _dt.date(2026, 8, 31),
                 [["2026-04-01", "2026-08-31"]])
    for p in ("2026-04", "2026-06", "2026-08"):
        assert _ran_during(line, p) is True
    assert _ran_during(line, "2026-03") is False
    assert _ran_during(line, "2026-09") is False


def test_an_open_ended_flight_runs_until_somebody_says_otherwise():
    line = _Line(_dt.date(2026, 4, 1), None, [["2026-04-01", None]])
    assert _ran_during(line, "2027-01") is True


# ------------------------------- River Valley Builders / The Home Store, 2026-07
#
# THE BUG BEHIND A WHOLE RUN OF FALSE POSITIVES.
#
# expected_products matched a client's orders by account id, and only fell back
# to matching by NAME if the id match found nothing. This client's report
# carries order 31050, so the id match found something - and the name match
# that would have found their live Live Chat order under 31171 never ran.
#
# The report was then failed twice: for a Live Chat "with no live order" that
# was sitting right there, and for two products that had stopped in 2024, which
# were the only orders anybody was looking at.
import datetime as _d

from sqlalchemy import create_engine as _ce
from sqlalchemy.orm import sessionmaker as _sm

from app.db import Base as _B, OrderLine as _OL
from app.roster import client_lines, expected_products, expected_why


def _db():
    eng = _ce("sqlite://")
    _B.metadata.create_all(eng)
    s = _sm(bind=eng)()
    s.add_all([
        _OL(market="7 Mountains PA", client="River Valley Builders/The Home Store",
            account_ids="31050", product="Mobile Conquesting",
            starts_on=_d.date(2023, 5, 5), ends_on=_d.date(2024, 6, 30),
            flights=[["2023-05-05", "2024-04-30"], ["2024-05-01", "2024-06-30"]]),
        _OL(market="7 Mountains PA", client="River Valley Builders/The Home Store",
            account_ids="31050", product="Social Mirror",
            starts_on=_d.date(2024, 7, 17), ends_on=_d.date(2024, 11, 30),
            flights=[["2024-07-17", "2024-11-30"]]),
        _OL(market="7 Mountains PA", client="River Valley Builders/The Home Store",
            account_ids="31050", product="Meta",
            starts_on=_d.date(2023, 8, 5), ends_on=_d.date(2026, 7, 31),
            flights=[["2024-12-06", "2026-07-31"]]),
        # The second order. Same client, different id, and live.
        _OL(market="7 Mountains PA", client="River Valley Builders/The Home Store",
            account_ids="31171", product="Live Chat",
            starts_on=_d.date(2024, 7, 17), ends_on=_d.date(2026, 12, 31),
            flights=[["2024-07-17", "2026-12-31"]]),
    ])
    s.commit()
    return s


def test_every_order_for_a_client_counts_not_just_the_one_on_the_report():
    s = _db()
    lines = client_lines(s, "River Valley Builders The Home Store", "31050")
    assert {l.account_ids for l in lines} == {"31050", "31171"}


def test_the_live_chat_order_stops_live_chat_being_rogue():
    s = _db()
    got = expected_products(s, "River Valley Builders The Home Store", "31050",
                            period="2026-07")
    assert "Live Chat" in got


def test_products_that_stopped_in_2024_are_not_expected_in_2026():
    s = _db()
    got = expected_products(s, "River Valley Builders The Home Store", "31050",
                            period="2026-07")
    assert got == {"Meta", "Live Chat"}


def test_the_finding_can_show_its_working():
    """Three rounds of "this is a false positive" all needed the same thing to
    settle them: which orders were looked at and what their dates were."""
    s = _db()
    rows = expected_why(s, "River Valley Builders The Home Store", "31050",
                        period="2026-07")
    flat = " | ".join(f"{a} = {b}" for a, b in rows)
    assert "Live Chat · order 31171" in flat
    assert "Mobile Conquesting · order 31050" in flat
    assert "not running in July 2026" in flat
    assert "counted" in flat


def test_a_client_with_no_orders_at_all_still_says_so():
    s = _db()
    assert client_lines(s, "Someone Else", "99999") is None
    assert expected_products(s, "Someone Else", "99999", period="2026-07") is None


# ------------------------------------------- W&L Subaru, order 14885
#
# The only Meta line item is IO Paused and ended on 30 June. The July report
# was failed for a missing Meta section. A paused buy is not delivering, so it
# is not owed on the report - and if its product does turn up, that is not a
# surprise either. It makes no claim in either direction.
from app.roster import quiet_products


def _paused_db():
    eng = _ce("sqlite://")
    _B.metadata.create_all(eng)
    s = _sm(bind=eng)()
    s.add_all([
        _OL(market="7 Mountains PA", client="W&L Subaru", account_ids="14885", product="Meta", live=False,
            starts_on=_d.date(2020, 8, 10), ends_on=_d.date(2026, 6, 30),
            flights=[["2020-08-10", "2026-06-30"]]),
        _OL(market="7 Mountains PA", client="W&L Subaru", account_ids="14885", product="Video", live=True,
            starts_on=_d.date(2020, 8, 10), ends_on=_d.date(2026, 12, 31),
            flights=[["2020-08-10", "2026-12-31"]]),
    ])
    s.commit()
    return s


def test_a_paused_line_item_is_not_expected():
    s = _paused_db()
    assert expected_products(s, "W&L Subaru", "14885", period="2026-07") == {"Video"}


def test_a_paused_product_on_the_report_is_not_a_surprise_either():
    """"if a line item is paused we don't need an alert for or against"."""
    s = _paused_db()
    quiet = quiet_products(s, "W&L Subaru", "14885", period="2026-07")
    assert "Meta" in quiet

    from app.checks.rules import check_products
    out = check_products({"expected_products": {"Video"},
                          "products": {"Video", "Meta"},
                          "quiet_products": quiet})
    assert out == []


def test_a_product_out_of_flight_is_quiet_too():
    s = _paused_db()
    assert quiet_products(s, "W&L Subaru", "14885", period="2027-06") >= {"Video"}


def test_the_trace_says_why_it_was_left_out():
    s = _paused_db()
    rows = dict(expected_why(s, "W&L Subaru", "14885", period="2026-07"))
    assert "paused, so not owed either way" in rows["Meta · order 14885"]


def test_a_product_live_on_one_order_and_paused_on_another_is_live():
    s = _paused_db()
    s.add(_OL(market="7 Mountains PA", client="W&L Subaru", account_ids="14886", product="Meta", live=True,
              starts_on=_d.date(2026, 1, 1), ends_on=_d.date(2026, 12, 31),
              flights=[["2026-01-01", "2026-12-31"]]))
    s.commit()
    assert "Meta" in expected_products(s, "W&L Subaru", "14885", period="2026-07")


# ------------------------------------------- the trace, readable
def test_the_trace_shows_the_flight_that_settles_it_not_all_of_them():
    """"2024-12-13 to 2026-12-31; 2026-02-06 to 2026-12-31" is a wall of dates
    you have to subtract in your head. The question is always the same: which
    flight covers this month, or if none does, how close the nearest came."""
    s = _paused_db()
    s.add(_OL(market="m", client="W&L Subaru", account_ids="14885",
              product="TikTok", live=True, starts_on=_d.date(2024, 12, 13),
              ends_on=_d.date(2026, 12, 31),
              flights=[["2024-12-13", "2026-12-31"], ["2026-02-06", "2026-12-31"]]))
    s.commit()
    rows = dict(expected_why(s, "W&L Subaru", "14885", period="2026-07"))
    line = rows["TikTok · order 14885"]
    assert line.startswith("2024-12-13 to 2026-12-31")
    assert "+1 more covering July 2026" in line
    assert ";" not in line, "it is still listing every window"


def test_a_product_that_stopped_says_when_rather_than_listing_flights():
    s = _paused_db()
    s.add(_OL(market="m", client="W&L Subaru", account_ids="14885",
              product="Social Mirror", live=True,
              starts_on=_d.date(2025, 1, 3), ends_on=_d.date(2026, 6, 30),
              flights=[["2025-01-03", "2025-06-30"], ["2025-08-01", "2026-06-30"]]))
    s.commit()
    rows = dict(expected_why(s, "W&L Subaru", "14885", period="2026-07"))
    assert rows["Social Mirror · order 14885"].startswith("ran to 2026-06-30")


def test_a_product_that_has_not_started_says_when_it_will():
    s = _paused_db()
    s.add(_OL(market="m", client="W&L Subaru", account_ids="14885",
              product="DOOH", live=True, starts_on=_d.date(2026, 8, 1),
              ends_on=None, flights=[["2026-08-01", None]]))
    s.commit()
    rows = dict(expected_why(s, "W&L Subaru", "14885", period="2026-07"))
    assert rows["DOOH · order 14885"].startswith("starts 2026-08-01")


def test_a_lifetime_paces_against_its_own_campaign_only(monkeypatch):
    """Field Of Dreams' lifetime covers Mobile Conquesting, 17 Dec to 13 Jul.
    A Display order starting 28 Jul - after that campaign finished - was
    counted into the same goal, so the panel asked a six-page report about
    750,000 impressions it was never going to carry and called the whole thing
    41% short."""
    import datetime as dt

    from app import roster as R

    class L:
        def __init__(self, product, s, e, imps):
            self.product, self.starts_on, self.ends_on = product, s, e
            self.impressions, self.budget = imps, None
            self.total_impressions = self.total_budget = None
            self.order_starts_on, self.order_ends_on = s, e
            self.sold_with, self.live, self.flights = "", True, None
            self.line_ids, self.account_ids = product, ""

    lines = [L("Mobile Conquesting", dt.date(2025, 12, 17), dt.date(2026, 7, 13), 166666),
             L("Display", dt.date(2026, 7, 28), dt.date(2026, 10, 14), 250000)]
    monkeypatch.setattr(R, "client_lines", lambda *a, **k: lines)

    both = R.ordered_for(None, "Field Of Dreams", "51118", "2026-07", lifetime=True)
    assert set(both) == {"Mobile Conquesting", "Display"}

    own = R.ordered_for(None, "Field Of Dreams", "51118", "2026-07", lifetime=True,
                        window=(dt.date(2025, 12, 17), dt.date(2026, 7, 13)))
    assert set(own) == {"Mobile Conquesting"}


def test_a_grouped_buy_counts_its_goal_once(monkeypatch):
    """"CTV + Video Ads" is one line item sold as two products, so the import
    writes two order rows for it - both carrying the SAME monthly goal, because
    it is one goal. Grouped back into one pacing row and then added, it came out
    doubled: Russell Law's lifetime was measured against 250,000 impressions on
    a campaign sold 125,000, and finished "45% under"."""
    import datetime as dt

    from app import roster as R

    class L:
        def __init__(self, product):
            self.product = product
            self.sold_with = "CTV, Video"
            self.line_ids, self.account_ids = "120341", "51091"
            self.starts_on = self.order_starts_on = dt.date(2026, 2, 15)
            self.ends_on = self.order_ends_on = dt.date(2026, 7, 15)
            self.impressions, self.budget = 50000, None
            self.total_impressions = self.total_budget = None
            self.live, self.flights, self.canceled = True, None, False

    monkeypatch.setattr(R, "client_lines", lambda *a, **k: [L("CTV"), L("Video")])
    life = R.ordered_for(None, "Russell Law", "51091", "2026-07", lifetime=True)
    assert list(life) == ["CTV, Video"]
    assert life["CTV, Video"]["impressions"] == 250000.0     # 5 months x 50,000

    month = R.ordered_for(None, "Russell Law", "51091", "2026-07")
    assert month["CTV, Video"]["impressions"] == 50000.0


# --------------------------------------------------- a cancelled buy on a lifetime
def test_a_product_cancelled_on_every_line_is_not_owed_on_the_lifetime(db):
    """SKYPAC 51251. The August lifetime was failed for "Ordered but not on the
    report: CTV, Performance Max, Video" - and all three of those were called
    off. The pacing panel on the same screen already said so, in as many words:
    a cancelled buy "is not part of what the campaign was asked to deliver"."""
    from app.db import OrderLine
    from app.roster import expected_products
    db.add(OrderLine(market="7 Mountains KY", client=CLIENT,
                     account_ids="51251", line_ids="120731", campaign="CTV",
                     product="CTV", canceled=True, live=False,
                     starts_on=dt.date(2026, 1, 1), ends_on=dt.date(2026, 6, 30),
                     detail=[{"line_id": "120731", "canceled": True,
                              "starts": "2026-01-01", "ends": "2026-06-30"}]))
    db.add(OrderLine(market="7 Mountains KY", client=CLIENT,
                     account_ids="51251", line_ids="120750",
                     campaign="Video Ads", product="Video",
                     starts_on=dt.date(2026, 1, 1), ends_on=dt.date(2026, 8, 31),
                     detail=[{"line_id": "120750", "canceled": False,
                              "starts": "2026-01-01", "ends": "2026-08-31"}]))
    db.commit()
    got = expected_products(db, CLIENT, "51251", lifetime=True)
    assert "CTV" not in got
    assert "Video" in got


def test_a_product_with_one_cancelled_line_and_one_live_one_still_counts(db):
    """Social Mirror CTV on that same campaign: cancelled on 120751, complete
    on 122725 and 128151. One OrderLine row holds all three, so reading the
    rolled-up flag would drop a product that plainly ran."""
    from app.db import OrderLine
    from app.roster import expected_products
    db.add(OrderLine(market="7 Mountains KY", client=CLIENT,
                     account_ids="51251", line_ids="120751 122725",
                     campaign="Social Mirror", product="Social Mirror Ads",
                     canceled=True, live=False,
                     starts_on=dt.date(2026, 1, 1), ends_on=dt.date(2026, 8, 31),
                     detail=[{"line_id": "120751", "canceled": True,
                              "starts": "2026-01-01", "ends": "2026-06-30"},
                             {"line_id": "122725", "canceled": False,
                              "starts": "2026-07-01", "ends": "2026-08-31"}]))
    db.commit()
    assert expected_products(db, CLIENT, "51251",
                             lifetime=True) == {"Social Mirror Ads"}


def test_a_row_with_no_line_detail_falls_back_to_its_own_flag(db):
    """Rows written before the detail existed still have to answer."""
    from app.db import OrderLine
    from app.roster import expected_products
    db.add(OrderLine(market="7 Mountains KY", client=CLIENT,
                     account_ids="51251", line_ids="1", campaign="CTV",
                     product="CTV", canceled=False,
                     starts_on=dt.date(2026, 1, 1), ends_on=dt.date(2026, 8, 31)))
    db.commit()
    assert expected_products(db, CLIENT, "51251", lifetime=True) == {"CTV"}
