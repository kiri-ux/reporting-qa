"""The serving file - what actually delivered, by client, by day.

Every rule about "did this run in July" has been an inference off two dates.
A line sold January to December and paused on the 2nd reads exactly like one
paused on the 30th, and "IO Complete" is where every campaign that ever
finished comes to rest. This is the file that answers it instead.
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


def _rows(header, *body):
    return [list(header)] + [list(r) for r in body]


HEAD = ["Client", "Client Business Unit", "Date", "Impressions"]


def _days(client, market, first, n, imps=1000):
    d = dt.date.fromisoformat(first)
    return [[client, market, (d + dt.timedelta(days=i)).isoformat(), str(imps)]
            for i in range(n)]


def test_it_counts_the_days_a_client_delivered_on(db):
    from app.serving import import_serving, served_days
    res = import_serving(db, _rows(HEAD, *_days("Acme Co", "Market One",
                                                "2026-07-03", 19)))
    assert res["clients"] == 1 and res["periods"] == ["2026-07"]
    assert served_days(db, "2026-07") == {("marketone", "acmeco"): 19}


def test_a_row_with_nothing_in_it_is_not_a_day(db):
    """Several of these exports write a row for every calendar day of the
    flight and put zeros in it. Counting those back is the same guess the
    dates were already making."""
    from app.serving import import_serving, served_days
    rows = _rows(HEAD, *_days("Acme Co", "Market One", "2026-07-01", 5),
                 *_days("Acme Co", "Market One", "2026-07-06", 20, imps=0))
    import_serving(db, rows)
    assert served_days(db, "2026-07") == {("marketone", "acmeco"): 5}


def test_the_same_day_twice_is_one_day(db):
    """One row per client per day is the grain, and it is not always kept -
    a client running four products can turn up four times on the 3rd."""
    from app.serving import import_serving, served_days
    rows = _rows(HEAD,
                 ["Acme Co", "Market One", "2026-07-03", "500"],
                 ["Acme Co", "Market One", "2026-07-03", "500"],
                 ["Acme Co", "Market One", "2026-07-04", "500"])
    import_serving(db, rows)
    assert served_days(db, "2026-07") == {("marketone", "acmeco"): 2}


def test_the_column_names_are_read_off_the_header(db):
    """This file comes out of a different tool than the order export and
    nobody should have to rename a column to make it load."""
    from app.serving import import_serving, served_days
    rows = _rows(["Advertiser", "Business Unit", "Serve Date", "Delivered"],
                 ["Acme Co", "Market One", "2026-07-03", "900"])
    res = import_serving(db, rows)
    assert served_days(db, "2026-07") == {("marketone", "acmeco"): 1}
    # And it says which ones it used, so a file that loads wrong says so.
    assert res["columns"]["client"] == "Advertiser"
    assert res["columns"]["day"] == "Serve Date"


def test_a_file_with_no_date_column_says_so(db):
    from app.serving import import_serving
    with pytest.raises(ValueError) as exc:
        import_serving(db, _rows(["Client", "Business Unit", "Impressions"],
                                 ["Acme Co", "Market One", "900"]))
    assert "date" in str(exc.value)


def test_months_are_kept_apart(db):
    from app.serving import import_serving, served_days
    rows = _rows(HEAD, *_days("Acme Co", "Market One", "2026-06-20", 8),
                 *_days("Acme Co", "Market One", "2026-07-20", 3))
    import_serving(db, rows)
    assert served_days(db, "2026-06") == {("marketone", "acmeco"): 8}
    assert served_days(db, "2026-07") == {("marketone", "acmeco"): 3}


def test_a_reload_replaces_that_month_and_leaves_the_others(db):
    from app.serving import import_serving, served_days
    import_serving(db, _rows(HEAD, *_days("Acme Co", "M", "2026-06-01", 9)))
    import_serving(db, _rows(HEAD, *_days("Acme Co", "M", "2026-07-01", 4)))
    assert served_days(db, "2026-06") == {("m", "acmeco"): 9}
    assert served_days(db, "2026-07") == {("m", "acmeco"): 4}


def test_the_order_export_is_not_mistaken_for_a_serving_file():
    """It carries a client, a business unit and dates too."""
    from app.serving import looks_like_serving
    assert not looks_like_serving(
        ["orders_id", "orders_status", "client", "product",
         "client_business_unit", "orders_start_date", "orders_end_date"])
    assert looks_like_serving(["Client", "Client Business Unit", "Date",
                               "Impressions"])


# ------------------------------------------------- and what the board does
def _order(db, client="Acme Co", market="Market One",
           start="2026-01-01", end="2026-12-31"):
    from app.db import Batch, OrderLine
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market=market, client=client, account_ids="53392",
                     line_ids="1", product="Display", campaign="Display Ads",
                     live=True, starts_on=D(start), ends_on=D(end),
                     order_starts_on=D(start), order_ends_on=D(end)))
    db.commit()


def test_the_serving_file_takes_a_client_off_the_board(db):
    """53392 IS PAUSED. Its flight covers the whole year, so the dates say it
    ran all of July and the board asks for a report. The serving file says it
    delivered on two days."""
    from app.board import expected_for
    from app.serving import import_serving
    _order(db)
    import_serving(db, _rows(HEAD, *_days("Acme Co", "Market One",
                                          "2026-07-01", 2)))
    skipped: list = []
    rows = expected_for(db, "2026-07", skipped=skipped)
    assert [e.kind for e in rows] == []
    assert "per the serving file" in skipped[0]["why"]


def test_nothing_at_all_in_the_file_says_it_did_not_serve(db):
    from app.board import expected_for
    from app.serving import import_serving
    _order(db)
    import_serving(db, _rows(HEAD, *_days("Someone Else", "Market One",
                                          "2026-07-01", 20)))
    skipped: list = []
    assert expected_for(db, "2026-07", skipped=skipped) == []
    assert "not in the serving file at all" in skipped[0]["why"]
    # AND IT SAYS WHY THAT IS TWO DIFFERENT THINGS, because a client the two
    # tools spell differently would otherwise read as a campaign that went dark.
    assert "spells this client differently" in skipped[0]["why"]


def test_a_client_that_served_a_full_month_stays_on_the_board(db):
    from app.board import expected_for
    from app.serving import import_serving
    _order(db)
    import_serving(db, _rows(HEAD, *_days("Acme Co", "Market One",
                                          "2026-07-01", 19)))
    assert [e.kind for e in expected_for(db, "2026-07")] == ["monthly"]


def test_a_month_with_no_file_loaded_reads_the_dates_as_before(db):
    """ABSENT IS NOT ZERO. Concluding nobody ran in July from a file that was
    never uploaded would empty the whole board."""
    from app.board import expected_for
    _order(db)
    assert [e.kind for e in expected_for(db, "2026-07")] == ["monthly"]


def test_the_serving_file_can_put_a_short_flight_back_on_the_board(db):
    """The dates say this started on the 30th and ran two days. The serving
    file says it delivered on 21, because the flight on the order is not when
    the campaign actually ran."""
    from app.board import expected_for
    from app.serving import import_serving
    _order(db, start="2026-07-30", end="2026-08-31")
    import_serving(db, _rows(HEAD, *_days("Acme Co", "Market One",
                                          "2026-07-10", 21)))
    assert [e.kind for e in expected_for(db, "2026-07")] == ["monthly"]


def test_july_2026_in_the_period_box_means_2026_07():
    """IT IS A TEXT FIELD. "July 2026" matched none of a file full of July and
    reported "0 clients across no month, 16,574 rows read" - which reads like
    the file is broken when the file is fine."""
    from app.serving import normalize_period
    for typed in ("July 2026", "july 2026", "2026-7", "7/2026", "2026-07"):
        assert normalize_period(typed) == "2026-07", typed
    assert normalize_period("") is None


def test_a_period_that_matches_nothing_says_what_the_file_holds(db):
    from app.serving import import_serving
    with pytest.raises(ValueError) as exc:
        import_serving(db, _rows(HEAD, *_days("Acme Co", "M", "2026-07-01", 9)),
                       period="2026-05")
    assert "2026-07" in str(exc.value)


def test_a_campaign_that_stopped_months_ago_is_not_closed_out_now(db):
    """CANCELLED AND IO COMPLETE SAY A CAMPAIGN IS OVER, NOT WHICH MONTH.
    Closing out on the flag alone dumped years of finished campaigns into one
    cycle. The serving file dates it: no delivery this month, it did not finish
    this month."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    from app.serving import import_serving
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="M", client="Long Gone", account_ids="1",
                     line_ids="1", product="Display", campaign="Display Ads",
                     live=False, canceled=True,
                     starts_on=D("2025-01-01"), ends_on=D("2026-12-31"),
                     order_starts_on=D("2025-01-01"), order_ends_on=D("2026-12-31")))
    db.commit()
    # With no serving file the flag stands on its own, as before.
    assert [e.kind for e in expected_for(db, "2026-07")] == ["lifetime"]
    # With one that says somebody else ran in July and this client did not.
    import_serving(db, _rows(HEAD, *_days("Someone Else", "M", "2026-07-01", 20)))
    assert [e.kind for e in expected_for(db, "2026-07")] == []


def test_a_campaign_that_stopped_this_month_still_closes_out(db):
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    from app.serving import import_serving
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="M", client="Just Finished", account_ids="1",
                     line_ids="1", product="Display", campaign="Display Ads",
                     live=False, canceled=True,
                     starts_on=D("2025-01-01"), ends_on=D("2026-12-31"),
                     order_starts_on=D("2025-01-01"), order_ends_on=D("2026-12-31")))
    db.commit()
    import_serving(db, _rows(HEAD, *_days("Just Finished", "M", "2026-07-01", 18),
                             *_days("Still Running", "M", "2026-07-01", 31)))
    assert "lifetime" in {e.kind for e in expected_for(db, "2026-07")}


# ------------------------------------------------------------ hand overrides
def test_a_row_the_rules_dropped_can_be_put_back(db):
    """EVERY RULE IS A RULE ABOUT THE USUAL CASE. Somebody who knows this
    client gets the last word."""
    import datetime as dt
    from app.board import expected_for
    from app.db import CycleDone
    from app.serving import import_serving
    _order(db)
    import_serving(db, _rows(HEAD, *_days("Acme Co", "Market One",
                                          "2026-07-01", 2)))
    assert expected_for(db, "2026-07") == []
    db.add(CycleDone(period="2026-07", ident="marketone|acmeco|monthly",
                     market="Market One", client="Acme Co", kind="monthly",
                     reason="needed", note="client asked for it anyway",
                     marked_by="kiri"))
    db.commit()
    rows = expected_for(db, "2026-07")
    assert [e.kind for e in rows] == ["monthly"]
    assert rows[0].forced_by == "kiri"
    assert rows[0].forced_note == "client asked for it anyway"
    # And it is not confused with a row somebody checked off.
    assert rows[0].done_by == "" and not rows[0].done_only


def test_a_campaign_that_never_served_is_not_owed_a_lifetime(db):
    """ORDERS 51217 AND 50760. Both sold, both cancelled, neither served a
    single impression - 50760's own notes say so - and both were owed a
    lifetime, because their end dates fall inside the cycle and a date is all
    the order export gives. There is no report to pull for a campaign with no
    delivery behind it."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    from app.serving import import_serving
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="7 Mountains KY", client="Glasgow Garage",
                     account_ids="51217", line_ids="120588", product="CTV",
                     campaign="Connected TV Ads", live=False, canceled=True,
                     starts_on=D("2026-01-15"), ends_on=D("2026-07-31"),
                     order_starts_on=D("2026-01-15"), order_ends_on=D("2026-07-31")))
    db.commit()
    # No file loaded: the dates are all there is, and it stays.
    assert [e.kind for e in expected_for(db, "2026-07")] == ["lifetime"]
    # A file that shows somebody else running and not this one settles it.
    import_serving(db, _rows(HEAD, *_days("Someone Else", "7 Mountains KY",
                                          "2026-07-01", 20)))
    skipped: list = []
    assert expected_for(db, "2026-07", skipped=skipped) == []


def test_a_campaign_that_ended_mid_month_still_gets_its_lifetime(db):
    """It ran to the 15th, so it is in the file for fifteen days."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    from app.serving import import_serving
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="M", client="Real Campaign", account_ids="1",
                     line_ids="1", product="CTV", campaign="Connected TV Ads",
                     live=True, starts_on=D("2026-01-15"), ends_on=D("2026-07-15"),
                     order_starts_on=D("2026-01-15"), order_ends_on=D("2026-07-15")))
    db.commit()
    import_serving(db, _rows(HEAD, *_days("Real Campaign", "M", "2026-07-01", 15),
                             *_days("Still Running", "M", "2026-07-01", 31)))
    assert "lifetime" in {e.kind for e in expected_for(db, "2026-07")}


def test_a_cancelled_campaign_says_when_it_actually_stopped(db):
    """THE END DATE ON THE ORDER IS WHAT IT WAS SOLD TO RUN TO. Nothing on the
    export says when somebody hit cancel, so a lifetime pulled to that date
    covers weeks of nothing. The last day with delivery on it is where the
    campaign actually ended, and that is the date to pull to."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    from app.serving import import_serving
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="M", client="Stopped Early", account_ids="51217",
                     line_ids="1", product="CTV", campaign="Connected TV Ads",
                     live=False, canceled=True,
                     starts_on=D("2026-01-15"), ends_on=D("2026-07-31"),
                     order_starts_on=D("2026-01-15"), order_ends_on=D("2026-07-31")))
    db.commit()
    import_serving(db, _rows(HEAD, *_days("Stopped Early", "M", "2026-07-01", 12),
                             # The file runs to the end of the month for
                             # everybody else, which is how we know this one
                             # stopped rather than the data running out.
                             *_days("Still Running", "M", "2026-07-01", 31)))
    life = [e for e in expected_for(db, "2026-07") if e.kind == "lifetime"]
    assert len(life) == 1
    assert life[0].stopped_on == D("2026-07-12")
    assert life[0].ends_on == D("2026-07-31")      # the order still says this


def test_a_campaign_that_ran_to_its_end_date_is_not_annotated(db):
    """Saying so anyway would put a note on half the board."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    from app.serving import import_serving
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="M", client="Ran To The End", account_ids="1",
                     line_ids="1", product="CTV", campaign="Connected TV Ads",
                     live=False, complete=True,
                     starts_on=D("2026-01-15"), ends_on=D("2026-07-15"),
                     order_starts_on=D("2026-01-15"), order_ends_on=D("2026-07-15")))
    db.commit()
    import_serving(db, _rows(HEAD, *_days("Ran To The End", "M", "2026-07-01", 15)))
    life = [e for e in expected_for(db, "2026-07") if e.kind == "lifetime"]
    assert len(life) == 1 and life[0].stopped_on is None


def test_a_complete_order_still_running_into_next_month_waits(db):
    """ORDER 49421. Reads "IO Complete", ends 6 August - a day past this
    cycle's lifetime cutoff - and delivered every day of July. It is still
    running into August, so its lifetime belongs to August's cycle.

    45911 also reads complete, is dated to the end of December, and last
    delivered mid-July. That one really has finished, and waiting for December
    means waiting forever. The difference is the last day with delivery on it."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    from app.serving import import_serving
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="M", client="Still Going", account_ids="49421",
                     line_ids="1", product="Meta",
                     campaign="Meta Display & Video Ads",
                     live=False, complete=True,
                     starts_on=D("2026-02-01"), ends_on=D("2026-08-06"),
                     order_starts_on=D("2026-02-01"), order_ends_on=D("2026-08-06")))
    db.commit()
    # It ran every day of July, so it had not stopped by the end of the month.
    import_serving(db, _rows(HEAD, *_days("Still Going", "M", "2026-07-01", 31)))
    kinds = {e.kind for e in expected_for(db, "2026-07")}
    assert "lifetime" not in kinds, "closed out a campaign still running"
    assert kinds == {"monthly"}


def test_a_complete_order_that_stopped_mid_month_closes_out(db):
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    from app.serving import import_serving
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="M", client="Sorge", account_ids="45911",
                     line_ids="1", product="Meta",
                     campaign="Meta Display & Video Ads",
                     live=False, complete=True,
                     starts_on=D("2025-05-01"), ends_on=D("2026-12-31"),
                     order_starts_on=D("2025-05-01"), order_ends_on=D("2026-12-31")))
    db.commit()
    import_serving(db, _rows(HEAD, *_days("Sorge", "M", "2026-07-01", 14),
                             *_days("Still Running", "M", "2026-07-01", 31)))
    rows = expected_for(db, "2026-07")
    life = [e for e in rows if e.kind == "lifetime"]
    assert len(life) == 1
    # And the row says which of the two rules asked for it.
    assert "cancelled or complete" in life[0].life_note
    assert "2026-07-14" in life[0].life_note


def test_a_stopped_campaign_is_not_paced_against_what_it_was_sold(db):
    """Sorge's cancelled Meta line read "100% short, 503 served against 400,000
    ordered (20 months at the monthly figure on the order)" - a comparison
    against twenty months it was never going to run."""
    import datetime as dt
    from app.db import Batch, OrderLine
    from app.roster import ordered_for
    from app.checks.rules import check_lifetime_goal, check_impression_pacing
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="M", client="Sorge", account_ids="45911",
                     line_ids="1", product="Meta",
                     campaign="Meta Display & Video Ads",
                     live=False, canceled=True, impressions=20000,
                     starts_on=D("2025-05-01"), ends_on=D("2026-12-31")))
    db.commit()
    got = ordered_for(db, "Sorge", "45911", "2026-07", lifetime=True)
    assert got and all(v["stopped"] for v in got.values())
    ctx = {"is_lifetime": True, "text": "Impressions\n503\n", "ordered": got}
    assert check_lifetime_goal(ctx) == []
    assert check_impression_pacing(ctx) == []


def test_reloading_a_month_that_is_already_there_does_not_die(db):
    """THE FILE COVERS THE LAST 180 DAYS AND GETS RE-UPLOADED. Loading it a
    second time died on "duplicate key value violates unique constraint
    uq_served_days" - the deletes and the inserts went to the database in one
    batch and it did the inserts first."""
    from app.serving import import_serving, served_days
    rows = _rows(HEAD, *_days("Acme Co", "M", "2026-06-01", 30),
                 *_days("Acme Co", "M", "2026-07-01", 12))
    import_serving(db, rows)
    # Same file again, then a wider one that overlaps it. Both are normal.
    import_serving(db, rows)
    import_serving(db, _rows(HEAD, *_days("Acme Co", "M", "2026-05-01", 31),
                             *_days("Acme Co", "M", "2026-06-01", 30),
                             *_days("Acme Co", "M", "2026-07-01", 20)))
    assert served_days(db, "2026-05") == {("m", "acmeco"): 31}
    assert served_days(db, "2026-06") == {("m", "acmeco"): 30}
    assert served_days(db, "2026-07") == {("m", "acmeco"): 20}


def test_a_month_the_new_file_does_not_cover_is_left_alone(db):
    """Uploading July on its own must not wipe the June that is already there."""
    from app.serving import import_serving, served_days
    import_serving(db, _rows(HEAD, *_days("Acme Co", "M", "2026-06-01", 30)))
    import_serving(db, _rows(HEAD, *_days("Acme Co", "M", "2026-07-01", 12)))
    assert served_days(db, "2026-06") == {("m", "acmeco"): 30}
    assert served_days(db, "2026-07") == {("m", "acmeco"): 12}


def test_a_file_covering_half_the_month_does_not_stop_the_whole_board(db):
    """ONLY JULY WAS UPLOADED THE FIRST TIME and every row on the board read
    "stopped 2026-07-31" - because every client's last day was the last day
    there was any data. "It stopped" has to mean it stopped before the data
    did."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    from app.serving import import_serving
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    for who in ("Alpha", "Beta"):
        db.add(OrderLine(market="M", client=who, account_ids="1", line_ids="1",
                         product="CTV", campaign="Connected TV Ads",
                         live=False, complete=True,
                         starts_on=D("2026-01-01"), ends_on=D("2026-10-31"),
                         order_starts_on=D("2026-01-01"),
                         order_ends_on=D("2026-10-31")))
    db.commit()
    # The file stops on the 12th for everybody - it is a partial upload.
    import_serving(db, _rows(HEAD, *_days("Alpha", "M", "2026-07-01", 12),
                             *_days("Beta", "M", "2026-07-01", 12)))
    rows = expected_for(db, "2026-07")
    assert not any(e.stopped_on for e in rows), "flagged the end of the data"
    # And nothing was closed out on the strength of it either.
    assert {e.kind for e in rows} == {"monthly"}


def test_a_line_not_launched_yet_keeps_the_campaign_open(db):
    """ORDERS 54169 AND 48327. Both have a line at IO Pending Launch dated to
    start in September, which this cycle's date filter drops - so every line
    the board could see was complete, and it closed out an order that has not
    launched half of what was sold."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    from app.serving import import_serving
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="M", client="Unkel Joes", account_ids="48327",
                     line_ids="122838", product="Social Mirror CTV",
                     campaign="Social Mirror CTV Ads", live=False, complete=True,
                     status="IO Complete",
                     starts_on=D("2025-09-22"), ends_on=D("2026-07-31"),
                     order_starts_on=D("2025-09-22"), order_ends_on=D("2026-12-31")))
    # Sold, not launched, starts in September - outside this cycle entirely.
    db.add(OrderLine(market="M", client="Unkel Joes", account_ids="48327",
                     line_ids="132514", product="Social Mirror",
                     campaign="Social Mirror Ads", live=False,
                     status="IO Pending Launch",
                     starts_on=D("2026-09-01"), ends_on=D("2026-12-31"),
                     order_starts_on=D("2025-09-22"), order_ends_on=D("2026-12-31")))
    db.commit()
    import_serving(db, _rows(HEAD, *_days("Unkel Joes", "M", "2026-07-01", 20),
                             *_days("Still Running", "M", "2026-07-01", 31)))
    kinds = {e.kind for e in expected_for(db, "2026-07")}
    assert "lifetime" not in kinds, "closed out an order with a line still to launch"


def test_the_status_is_kept_in_its_own_words(db):
    """Every flag on an order line is this fact reduced to a yes or a no, and
    when a row looks wrong the first question is what the order actually says."""
    from sqlalchemy import select
    from app.db import OrderLine
    from app.orders_io import import_io_export
    head = ("orders_id,id,orders_status,status,client,product,"
            "client_business_unit,orders_start_date,orders_end_date,"
            "start_date,end_date,campaign_manager\n")
    rows = ("48327,122838,IO Live,IO Complete,Unkel Joes,Social Mirror Ads,"
            "M,2025-09-22,2026-12-31,2025-09-22,2026-07-31,Someone\n"
            "48327,132514,IO Live,IO Pending Launch,Unkel Joes,Social Mirror Ads,"
            "M,2025-09-22,2026-12-31,2026-01-01,2026-12-31,Someone\n")
    import_io_export(db, (head + rows).encode(), period="2026-07")
    row = db.scalars(select(OrderLine)).first()
    assert "IO Complete" in row.status and "IO Pending Launch" in row.status
