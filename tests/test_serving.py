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
