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
                        lambda db, **k: type("C", (), {"id": 1})())
    monkeypatch.setattr("app.orders_s3.sync",
                        lambda db, **k: called.append(k) or type("R", (), {"message": "ok"})())
    rmod._remap_orders_if_stale()
    assert called and called[0].get("force") is True
    # And it says who started it, because three different things can and none
    # of them used to say so.
    assert called[0].get("trigger") == "rules"


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
                        lambda db, **k: (_ for _ in ()).throw(RuntimeError("no s3")))
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


def test_the_order_re_read_is_not_gated_behind_the_report_sweep(monkeypatch):
    """Blair Regional YMCA's trace said "Social Mirror · 2025-01-03 to
    2026-12-31", which is three Social Mirror CTV line items merged by the OLD
    import - the one that had no Social Mirror CTV key and no per-order flights.

    The re-read that would have fixed it sat behind auto_recheck, so on a deploy
    with the report sweep off the export was never read again. Re-reading the
    orders is not the same job as re-reading the PDFs.
    """
    import inspect
    from app import recheck as rmod
    src = inspect.getsource(rmod.start_sweeper)
    assert "_remap_orders_if_stale()" in src
    before = src.index("_remap_orders_if_stale()")
    after = src.index("if not settings.auto_recheck")
    assert before < after, "the re-read still runs only when the sweep is on"


def test_the_stale_orders_banner_is_a_button_not_a_bar():
    """A full-width yellow bar for a condition that clears itself was
    shouting. The orders button in the top bar turns amber and says the same
    thing on hover."""
    from pathlib import Path as _P
    assert 'class="stalebar"' not in _P("app/templates/viewer.html").read_text()
    cycle = _P("app/templates/cycle.html").read_text()
    assert 'class="stalebar"' not in cycle
    assert "orders_stale" in cycle, "the amber state still has to be driven by it"


# ------------------------------------------- budgets, merged in from a sheet
def test_a_single_product_sheet_never_deletes_anything(db):
    """A file of 368 Performance Max rows through the normal import would
    delete every order for every other product and leave PMax standing. This
    reads, matches and updates - it never deletes a row and never creates one."""
    from app.budgets import import_budgets
    db.add_all([
        OrderLine(market="m", client="Has PMax", account_ids="51033",
                  line_ids="119990", product="Performance Max"),
        OrderLine(market="m", client="Has Display", account_ids="900",
                  line_ids="901", product="Display"),
    ])
    db.commit()
    sheet = ("Order Id,ID,Product,Monthly Campaign Budget\n"
             "51033,119990,Performance Max Ads,2000\n")
    res = import_budgets(db, sheet.encode(), "pmax.csv")
    assert res["lines_updated"] == 1
    assert db.query(OrderLine).count() == 2, "it deleted something"
    rows = {l.client: l.budget for l in db.query(OrderLine).all()}
    assert rows["Has PMax"] == 2000.0
    assert rows["Has Display"] is None


def test_it_matches_on_line_item_id_first_then_the_order(db):
    from app.budgets import import_budgets
    db.add_all([
        OrderLine(market="m", client="By line", account_ids="1", line_ids="777",
                  product="Performance Max"),
        OrderLine(market="m", client="By order", account_ids="44807",
                  line_ids="", product="Performance Max"),
    ])
    db.commit()
    sheet = ("Order Id,ID,Monthly Campaign Budget\n"
             "1,777,1500\n"
             "44807,888,1750\n")
    res = import_budgets(db, sheet.encode(), "b.csv")
    assert res["matched_on_line_item"] == 1
    assert res["matched_on_order"] == 1
    rows = {l.client: l.budget for l in db.query(OrderLine).all()}
    assert rows["By line"] == 1500.0 and rows["By order"] == 1750.0


def test_money_survives_however_it_is_written(db):
    from app.budgets import _money
    assert _money("$1,215.08") == 1215.08
    assert _money(1500) == 1500.0
    assert _money("-") is None
    assert _money("") is None and _money(None) is None


def test_the_display_spelling_of_the_export_is_recognized():
    """The nightly S3 file is snake_case; a sheet pulled by hand out of the IO
    tool uses the display names. Same columns, and a reader that knows only one
    spelling rejects a good file with a confusing message."""
    from app.orders_io import looks_like_io_export, normalize_header
    assert normalize_header("Order's Status") == "orders_status"
    assert normalize_header("Client Business Unit") == "client_business_unit"
    assert normalize_header("Monthly Campaign Budget") == "monthly_campaign_budget"
    assert looks_like_io_export(["Client Business Unit", "Order's Status",
                                 "Product", "Order's End Date"])
    assert looks_like_io_export(["client_business_unit", "orders_status",
                                 "product", "orders_end_date"])


def test_a_sheet_ahead_of_the_board_is_reported_not_dropped(db):
    from app.budgets import import_budgets
    sheet = ("Order Id,ID,Monthly Campaign Budget\n99999,88888,500\n")
    res = import_budgets(db, sheet.encode(), "b.csv")
    assert res["lines_updated"] == 0
    assert res["not_on_the_board"] == 1


def test_a_repeated_column_reads_whichever_one_has_the_value():
    """The export repeats start_date and end_date, and the populated one is not
    the same of the pair for both: the second start_date carries the value, the
    first end_date does. Reading the last of each left every line item's end
    date blank, so it fell through to the order header - and a line item that
    finished in June looked live to the end of the order."""
    from app.orders_io import _open_source
    csv = ("client_business_unit,orders_status,client,orders_id,product,id,"
           "status,orders_start_date,start_date,start_date,end_date,end_date,"
           "orders_end_date\n"
           "BU,IO Live,Blair,31449,Social Mirror OTT Ads,96533,IO Complete,"
           "2025-01-01,,2025-01-03,2025-06-30,,2026-12-31\n")
    row = next(iter(_open_source(csv.encode())))
    assert row["start_date"] == "2025-01-03"
    assert row["end_date"] == "2025-06-30"
    assert row["orders_end_date"] == "2026-12-31"


def test_a_line_item_that_finished_is_not_kept_alive_by_its_order(db):
    """The whole Blair Regional YMCA run of false failures in one row."""
    from app.db import OrderLine
    from app.orders_io import import_io_export
    csv = ("client_business_unit,orders_status,client,orders_id,product,id,"
           "status,orders_start_date,start_date,start_date,end_date,end_date,"
           "orders_end_date\n"
           "7 Mountains PA,IO Live,Blair Regional YMCA,31449,"
           "Social Mirror OTT Ads,96533,IO Complete,"
           "2025-01-01,,2025-01-03,2025-06-30,,2026-12-31\n")
    res = import_io_export(db, csv.encode(), period="2026-07")
    assert res["skipped"].get("ended before the period") == 1
    assert db.query(OrderLine).count() == 0


# ------------------------------------------- pacing: a full month vs the budget
def test_the_spend_tiles_are_read_off_a_real_report():
    """The Spend Overview prints a tile per product. Two tiles share a line -
    "PPC Ad Cost" and "PPC Cost-Per-Click" - and their values share the line
    below, so the first dollar amount is the cost-per-click as often as the
    cost. The value belonging to a tile is the one nearest its own column."""
    from pathlib import Path as _P
    import pytest as _pt
    sample = _P("/root/work/sample.txt")
    if not sample.exists():
        _pt.skip("everything-sample not present")
    from app.checks.spend import report_spend, tile_value
    got = report_spend(sample.read_text())
    assert got["PPC"] == 4037.06
    assert got["LinkedIn"] == 562.37
    assert got["Performance Max"] == 7027.70
    assert tile_value(sample.read_text(), "PPC Cost-Per-Click") == 0.96


def test_half_a_months_budget_adrift_is_flagged():
    from app.checks.rules import check_pacing, check_pacing_off
    ctx = {"text": " PPC Ad Cost\n Amount spent\n $400.00\n", "is_lifetime": False,
           "budgets": {"PPC": 1000.0}}
    out = check_pacing(ctx)
    assert len(out) == 1 and out[0]["severity"] == "warn"
    assert "60% under budget" in out[0]["title"]


def test_a_normal_month_says_nothing():
    from app.checks.rules import check_pacing, check_pacing_off
    for spent in ("$900.00", "$1,000.00", "$1,400.00"):
        ctx = {"text": f" PPC Ad Cost\n Amount spent\n {spent}\n",
               "is_lifetime": False, "budgets": {"PPC": 1000.0}}
        assert check_pacing(ctx) == [], spent


def test_overspending_by_half_is_flagged_too():
    from app.checks.rules import check_pacing, check_pacing_off
    ctx = {"text": " PPC Ad Cost\n Amount spent\n $1,600.00\n",
           "is_lifetime": False, "budgets": {"PPC": 1000.0}}
    out = check_pacing(ctx)
    assert out and "60% over budget" in out[0]["title"]


def test_a_lifetime_report_is_not_paced():
    """It covers a campaign's whole flight, and a monthly budget says nothing
    about that."""
    from app.checks.rules import _rule_applies, check_pacing, check_pacing_off
    ctx = {"text": " PPC Ad Cost\n x\n $10.00\n", "is_lifetime": True,
           "budgets": {"PPC": 1000.0}}
    assert check_pacing(ctx) == []
    assert _rule_applies(check_pacing_off, ctx) is False


def test_no_budget_loaded_means_no_claim():
    from app.checks.rules import _rule_applies, check_pacing_off
    ctx = {"text": " PPC Ad Cost\n x\n $10.00\n", "is_lifetime": False, "budgets": {}}
    assert _rule_applies(check_pacing_off, ctx) is False


def test_a_product_whose_spend_is_not_on_the_report_is_skipped():
    """Most products print no spend at all. Comparing a budget against nothing
    would fail every one of them."""
    from app.checks.rules import check_pacing, check_pacing_off
    ctx = {"text": "nothing here\n", "is_lifetime": False,
           "budgets": {"Display": 1000.0}}
    assert check_pacing(ctx) == []


def test_budgets_are_summed_across_the_flights_that_ran(db):
    from app.roster import budgets_for
    db.add_all([
        OrderLine(market="m", client="Two Flights", account_ids="1", product="PPC",
                  live=True, budget=600, flights=[["2026-07-01", "2026-07-15"]]),
        OrderLine(market="m", client="Two Flights", account_ids="1", product="PPC",
                  live=True, budget=400, flights=[["2026-07-16", "2026-07-31"]]),
        OrderLine(market="m", client="Two Flights", account_ids="1", product="PPC",
                  live=True, budget=999, flights=[["2025-01-01", "2025-02-01"]]),
        OrderLine(market="m", client="Two Flights", account_ids="1", product="Meta",
                  live=False, budget=500, flights=[["2026-07-01", "2026-07-31"]]),
    ])
    db.commit()
    got = budgets_for(db, "Two Flights", "1", period="2026-07")
    assert got == {"PPC": 1000.0}, "paused lines and other months must not count"


def test_a_line_with_no_budget_on_file_contributes_nothing(db):
    """"No budget loaded" and "a budget of nothing" are different claims and
    only one of them is true."""
    from app.roster import budgets_for
    db.add(OrderLine(market="m", client="No Budget", account_ids="2",
                     product="PPC", live=True, budget=None,
                     flights=[["2026-07-01", "2026-07-31"]]))
    db.commit()
    assert budgets_for(db, "No Budget", "2", period="2026-07") == {}


def test_the_pull_range_is_per_partner_and_oldest_first(db):
    """One date for 146 partners is why the guidance says 2018. Per partner,
    almost none of them need it - and a campaign that finished years ago must
    not drag one back."""
    import datetime as _d
    D = _d.date
    db.add_all([
        OrderLine(market="Recent Radio", client="a", account_ids="1", live=True,
                  product="PPC", starts_on=D(2026, 5, 1),
                  order_starts_on=D(2026, 5, 1), order_ends_on=D(2026, 12, 31)),
        OrderLine(market="Old Media", client="b", account_ids="2", live=True,
                  product="PPC", starts_on=D(2018, 3, 4),
                  order_starts_on=D(2018, 3, 4), order_ends_on=D(2026, 12, 31)),
        OrderLine(market="Old Media", client="c", account_ids="3", live=True,
                  product="Meta", starts_on=D(2026, 1, 1),
                  order_starts_on=D(2026, 1, 1), order_ends_on=D(2026, 12, 31)),
        # An order that finished years ago is done being reported on, and must
        # not drag its partner's date backwards.
        OrderLine(market="Recent Radio", client="d", account_ids="4", live=False,
                  product="Meta", starts_on=D(2011, 1, 1),
                  order_starts_on=D(2011, 1, 1), order_ends_on=D(2012, 1, 1)),
    ])
    db.commit()

    from app.main import pull_range_rows
    got = pull_range_rows(db, today=D(2026, 8, 26))
    assert [(m, e.isoformat()) for m, e, _n in got] == [
        ("Old Media", "2018-03-04"),
        ("Recent Radio", "2026-05-01"),
    ], "oldest first, and a finished order must not drag a partner back"
    assert dict((m, n) for m, _e, n in got)["Old Media"] == 2


def test_the_pull_reaches_back_to_the_orders_start_not_the_live_lines(db):
    """RETHINK MEDIA GROUP. Order 4701, Memorial Hospital, IO Live, dated
    2018-11-01 to 2026-12-31. Its four oldest line items are IO Complete and
    finished between 2021 and 2023; the live ones start in 2021 and 2024.

    Reading the earliest LIVE line answered 2021-08-11 and the page said one
    pull would do - while everything that order did in its first three years
    fell outside the range and was never loaded. A lifetime for 4701 covers all
    of it, so the pull has to reach 2018."""
    import datetime as _d
    D = _d.date
    db.add_all([
        # The live lines, which is all the old rule looked at.
        OrderLine(market="ReThink Media Group", client="Memorial Hospital",
                  account_ids="4701", live=True, product="Native Display",
                  starts_on=D(2021, 8, 11), ends_on=D(2026, 12, 31),
                  order_starts_on=D(2018, 11, 1), order_ends_on=D(2026, 12, 31)),
        OrderLine(market="ReThink Media Group", client="Memorial Hospital",
                  account_ids="4701", live=True, product="Online Audio",
                  starts_on=D(2024, 1, 3), ends_on=D(2026, 12, 31),
                  order_starts_on=D(2018, 11, 1), order_ends_on=D(2026, 12, 31)),
    ])
    db.commit()
    from app.main import pull_range_rows, pull_strategy
    got = pull_range_rows(db, today=D(2026, 8, 26))
    assert got[0][1] == D(2018, 11, 1), got

    # And that puts it in the list of partners needing their own pull, rather
    # than quietly inside a bulk window that does not reach it.
    plan = pull_strategy(db, today=D(2026, 8, 26))
    assert [s["market"] for s in plan["stragglers"]] == ["ReThink Media Group"]


def test_a_long_range_is_cut_into_windows_tapclicks_will_accept(db):
    """TapClicks will not export more than 2,000 days in one go, so a partner
    with a campaign running since 2018 needs more than one pull."""
    import datetime as _d
    from app.main import pull_plan
    db.add_all([
        OrderLine(market="Old Media", client="a", account_ids="1", live=True,
                  product="PPC", starts_on=_d.date(2018, 3, 4)),
        OrderLine(market="Recent Radio", client="b", account_ids="2", live=True,
                  product="PPC", starts_on=_d.date(2026, 5, 1)),
    ])
    db.commit()
    plan = {r["market"]: r for r in pull_plan(db, today=_d.date(2026, 8, 25))}

    recent = plan["Recent Radio"]
    assert recent["pulls"] == 1
    assert recent["windows"] == [(_d.date(2026, 5, 1), _d.date(2026, 8, 25))]

    old = plan["Old Media"]
    assert old["days"] == (_d.date(2026, 8, 25) - _d.date(2018, 3, 4)).days + 1
    assert old["pulls"] == 2
    # Consecutive, no gap, no overlap, and the last one ends today.
    (a1, b1), (a2, b2) = old["windows"]
    assert a1 == _d.date(2018, 3, 4)
    assert (b1 - a1).days + 1 == 2000
    assert a2 == b1 + _d.timedelta(days=1)
    assert b2 == _d.date(2026, 8, 25)


def test_the_full_length_window_comes_first(db):
    """Cutting from the recent end would leave the odd remainder on the oldest
    window, which is the one nobody wants to run twice."""
    import datetime as _d
    from app.main import pull_plan
    db.add(OrderLine(market="m", client="a", account_ids="1", live=True,
                     product="PPC", starts_on=_d.date(2016, 1, 1)))
    db.commit()
    windows = pull_plan(db, today=_d.date(2026, 8, 25))[0]["windows"]
    lengths = [(b - a).days + 1 for a, b in windows]
    assert lengths[:-1] == [2000] * (len(windows) - 1)
    assert lengths[-1] <= 2000


def test_the_health_check_does_not_count_the_queue():
    """The platform pings it every few seconds with a five-second timeout, and
    a COUNT over the reports table while the sweeper has the box busy is a
    health check that fails because the service is working."""
    import inspect
    from app import main as mmod
    assert "stale_count" not in inspect.getsource(mmod.healthz)
    assert "stale_count" in inspect.getsource(mmod.healthz_deep)


def test_a_recheck_does_not_re_fingerprint_the_logo():
    """It shells out to pdftoppm - a fifth of a second - and doing that on
    every report in an 838-deep queue is what timed the health check out."""
    import inspect
    from app import recheck as rmod
    assert "rep.logo_hash or header_logo_hash(path)" in inspect.getsource(rmod.recheck)


def test_every_way_of_starting_a_sync_says_which_it_was():
    """Opening the page and finding one running looked like the tool doing
    something on its own for no reason anybody could name. Three different
    things can start one."""
    from pathlib import Path as _P
    from app.orders_s3 import TRIGGERS
    # "clock" is the scheduled check (build 128) and "sheet" is the breakout
    # sheet changing. Every way a sync can start has to have words for itself,
    # or a sync nobody started is a mystery on the page.
    assert set(TRIGGERS) == {"button", "rules", "batch", "clock", "sheet"}
    assert 'trigger="button"' in _P("app/main.py").read_text()
    assert 'trigger="rules"' in _P("app/recheck.py").read_text()
    assert 'trigger="batch"' in _P("app/ingest.py").read_text()
    assert "triggers[running.trigger]" in _P("app/templates/orders.html").read_text()


# ------------------------------------------- refusing to answer from stale data
def test_the_product_check_abstains_while_the_orders_are_stale():
    """It was producing findings from an import four builds old - the same one,
    three times, on a report that was right. No amount of explaining beats not
    saying it."""
    from app.checks.rules import _rule_applies, check_products
    fresh = {"expected_products": {"Meta"}, "products": {"Meta"},
             "orders_current": True}
    assert _rule_applies(check_products, fresh) is True
    stale = dict(fresh, orders_current=False)
    assert _rule_applies(check_products, stale) is False


def test_the_report_page_can_show_the_rows_it_is_judged_against(db):
    """Three rounds of "why am I still seeing this" all came down to the stored
    rows being older than the code, and there was no way to look at them
    without me guessing from a screenshot."""
    from pathlib import Path as _P
    tpl = _P("app/templates/report_orders_body.html").read_text()
    # "Would be" was its own column; it is now a line under the product, said
    # in words, because it is only ever interesting when it disagrees.
    assert "reads as {{ r.would_be }} today" in tpl
    assert "merged span" in tpl
    assert "/report/{{ rep.id }}/orders" in _P("app/templates/viewer.html").read_text()
    assert '@app.get("/report/{report_id}/orders")' in _P("app/main.py").read_text()


# ------------------------------------- one row per order and per line item
_HEAD = ("client_business_unit,orders_status,client,orders_id,product,id,"
         "status,orders_start_date,start_date,end_date,orders_end_date\n")
# Two orders selling one product. 101 ran through July and stops mid-August;
# 102 does not start until August. Merged they are one buy running June to
# December, which is true of neither of them.
TWO_FLIGHTS = ("BU,IO Live,Acme,101,Social Mirror Ads,7001,IO Live,"
               "2026-06-01,2026-06-01,2026-08-15,2026-08-15\n"
               "BU,IO Live,Acme,102,Social Mirror Ads,7002,IO Live,"
               "2026-08-01,2026-08-01,2026-12-31,2026-12-31\n")


def test_a_closed_orders_end_date_beats_its_line_items(db):
    """Order 48135 is IO Complete and ended on 28 February. Its Social Mirror
    line item is still dated 1 April to 31 December, so the line item's own end
    kept it alive and it sat on Long Jewelers' JULY report as a running buy,
    five months after somebody closed the order."""
    from app.db import OrderLine
    from app.orders_io import import_io_export
    rows = (
        "VB Blvd,IO Complete,Long Jewelers,48135,Social Mirror Ads,127806,,"
        "2025-11-11,2026-04-01,2026-12-31,2026-02-28\n"
        "VB Blvd,IO Live,Long Jewelers,53342,Social Mirror Ads,127821,IO Live,"
        "2026-05-01,2026-05-01,2026-12-31,2026-12-31\n")
    import_io_export(db, (_HEAD + rows).encode(), period="2026-07")
    row = db.query(OrderLine).filter_by(product="Social Mirror").one()
    assert row.account_ids == "53342", "48135 closed in February"
    assert [d["order"] for d in row.detail] == ["53342"]


def test_a_live_order_with_a_stale_header_date_keeps_its_line_item(db):
    """The other way round. A line item re-flighted under a live order is the
    newer fact of the two, so this is not a plain min() of the two dates."""
    from app.db import OrderLine
    from app.orders_io import import_io_export
    rows = ("BU,IO Live,Acme,900,Social Mirror Ads,9001,IO Live,"
            "2026-01-01,2026-04-01,2026-12-31,2026-02-28\n")
    import_io_export(db, (_HEAD + rows).encode(), period="2026-07")
    assert db.query(OrderLine).filter_by(product="Social Mirror").one()


def test_each_line_item_is_kept_with_its_own_order_and_its_own_dates(db):
    """The stored row is one answer per client and product, merged across every
    order that sells it, because that is what the checks ask. Read by a person
    it hides what they came for: the order ids, the line ids and the flights
    were three separate lists with nothing tying them together."""
    from app.db import OrderLine
    from app.orders_io import import_io_export
    import_io_export(db, (_HEAD + TWO_FLIGHTS).encode(), period="2026-08")
    row = db.query(OrderLine).filter_by(product="Social Mirror").one()
    assert row.account_ids == "101, 102"          # still merged, as the checks want
    detail = {d["order"]: d for d in row.detail}
    assert set(detail) == {"101", "102"}
    assert detail["101"]["line"] == "7001" and detail["101"]["ends"] == "2026-08-15"
    assert detail["102"]["line"] == "7002" and detail["102"]["starts"] == "2026-08-01"


def test_what_todays_code_would_map_a_raw_name_to_is_shown():
    """Where that differs from the stored product, the row was written by an
    older import and that is the whole answer."""
    from app.checks.products import map_order_products
    assert map_order_products("Social Mirror CTV Ads") == ["Social Mirror CTV"]
    # The old import had no Social Mirror CTV key, so a row stored as plain
    # "Social Mirror" with this raw name is provably from older code.


# ------------------------------------------------- money and impressions
def test_the_orders_own_money_and_impressions_are_loaded(db):
    """They were being read off the export and thrown away, so budgets only
    existed if a spreadsheet was uploaded by hand - and the next full sync
    deleted the rows and took them with it."""
    from app.db import OrderLine
    from app.orders_io import import_io_export
    head = ("client_business_unit,orders_status,client,orders_id,product,id,"
            "status,orders_start_date,start_date,end_date,orders_end_date,"
            "monthly_campaign_budget,monthly_pm_ad_spend,"
            "monthly_campaign_impressions\n")
    rows = ("BU,IO Live,Acme,1,Mobile Conquesting Display & Video Ads,10,IO Live,"
            "2026-01-01,2026-01-01,2026-12-31,2026-12-31,1500,,250000\n"
            "BU,IO Live,Acme,1,Performance Max Ads,11,IO Live,"
            "2026-01-01,2026-01-01,2026-12-31,2026-12-31,999,750,\n")
    import_io_export(db, (head + rows).encode(), period="2026-07")
    by = {l.product: l for l in db.query(OrderLine).all()}
    assert by["Mobile Conquesting"].budget == 1500
    assert by["Mobile Conquesting"].impressions == 250000
    # Performance Max paces on its own column, not on the campaign budget.
    assert by["Performance Max"].budget == 750


def test_pacing_is_served_over_ordered(db):
    from app.checks.served import pacing_pct, pacing_rows
    # NEGATIVE IS SHORT, so the sign agrees with the word beside it.
    assert pacing_pct(50, 100) == -50.0           # half short
    assert pacing_pct(120, 100) == 20.0           # a fifth over
    assert pacing_pct(None, 100) is None          # not printed on the report
    assert pacing_pct(50, None) is None           # not on the order

    text = (" Line Item Performance\n"
            " Acme - Keyword Social Mirror     40,000      100     0.25%\n"
            " Acme - a line with no product    10,000       20     0.20%\n")
    rows = pacing_rows(text, {"Social Mirror": {"budget": None,
                                                "impressions": 50000}})
    first = rows[0]
    assert first["served"] == 40000 and first["ordered"] == 50000
    assert round(first["pace"]) == -20
    total = rows[-1]
    # A line item whose name names no product counts in the total and against
    # no single product, and the page says so rather than guessing.
    assert total["total"] and total["served"] == 50000
    assert total["unattributed"] == 10000


def test_report_line_items_are_read_back_to_a_product():
    """The order says "Mobile Conquesting Display & Video Ads"; the report says
    "Close Lumber - Geo-Retargeting Mobile". Matching only the order's spelling
    left every Mobile Conquesting line on every report unattributed."""
    from app.checks.served import report_product
    assert report_product("Close Lumber - Geo-Retargeting Mobile") == "Mobile Conquesting"
    assert report_product("Lookalike Facebook/Instagram Premium") == "Meta"
    assert report_product("Jeff Stanley - AI Social Mirror CTV") == "Social Mirror CTV"
    assert report_product("Keyword Social Mirror") == "Social Mirror"
    # Specificity, not word order: this is a billboard, not a video buy.
    assert report_product("Carpet Place - Venue Targeting DOOH Video") == "DOOH"
    assert report_product("Wick Buildings - AI Native") == "Native Display"
    assert report_product("Matt Heilala - YouTube Channels") == "YouTube"
    assert report_product("Some Client - a name with no product in it") is None


def test_the_everything_report_is_almost_entirely_attributed():
    """A per-product breakdown that only covers half the impressions is worse
    than no breakdown, so this is pinned against a real report."""
    import pytest as _pt
    from pathlib import Path as _P
    sample = _P("/root/work/sample.txt")
    if not sample.exists():
        _pt.skip("everything-sample not present")
    from app.checks.served import served_impressions
    got = served_impressions(sample.read_text())
    assert got["unattributed"] / got["total"] < 0.02
    assert got["by_product"]["Mobile Conquesting"] > 1_000_000


# ------------------------------------------- the order's dates, not the line's
def test_a_lifetime_follows_the_order_not_the_line_item(db):
    """A line item is re-flighted, paused and restarted inside an order that
    runs for years. Reading the line item for both questions put a lifetime on
    the board that nobody owed - River Valley Builders' Performance Max, moved
    to run 1 Aug to 31 Dec, was still stored as ending 31 July."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    D = dt.date.fromisoformat

    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="7 Mountains PA", client="River Valley Builders",
                     account_ids="52182", line_ids="134665",
                     product="Performance Max", campaign="Performance Max Ads",
                     # the old flight is what the export still carries
                     starts_on=D("2026-02-01"), ends_on=D("2026-07-31"),
                     order_starts_on=D("2026-02-01"),
                     order_ends_on=D("2026-12-31"), live=True))
    db.commit()

    rows = expected_for(db, "2026-07")
    kinds = {e.kind for e in rows}
    assert "monthly" in kinds
    # NO LIFETIME. The order runs to the end of the year.
    assert "lifetime" not in kinds


def test_the_lifetime_range_is_the_orders_range(db):
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    D = dt.date.fromisoformat

    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="7 Mountains PA",
                     client="River Valley Builders/The Home Store",
                     account_ids="31050", line_ids="96645, 96647",
                     product="Meta", campaign="Meta Display & Video Ads",
                     starts_on=D("2024-07-17"), ends_on=D("2026-12-31"),
                     order_starts_on=D("2023-05-05"),
                     order_ends_on=D("2026-07-31"), live=True))
    db.commit()

    life = [e for e in expected_for(db, "2026-07") if e.kind == "lifetime"]
    assert len(life) == 1
    assert life[0].starts_on == D("2023-05-05")
    assert life[0].ends_on == D("2026-07-31")


# ------------------------------------------- what a cycle does NOT owe
def _grove(db, **kw):
    import datetime as dt
    from app.db import OrderLine
    base = dict(market="7 Mountains KY", client="The Grove Event Venue",
                account_ids="54913", line_ids="132979",
                product="Social Mirror CTV", campaign="Social Mirror CTV Ads",
                live=True)
    base.update(kw)
    db.add(OrderLine(**base))
    db.commit()


def test_a_campaign_that_barely_ran_is_not_owed_a_monthly(db):
    """The Grove started on 30 July, so its July report would cover two days of
    delivery - a page of near-zero numbers that reads as a broken report."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch
    D = dt.date.fromisoformat

    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    _grove(db, starts_on=D("2026-07-30"), ends_on=D("2026-10-31"),
          order_starts_on=D("2026-07-30"), order_ends_on=D("2026-10-31"))
    skipped: list = []
    rows = expected_for(db, "2026-07", skipped=skipped)
    assert rows == []
    assert skipped and "2 days" in skipped[0]["why"]

    # A week is enough.
    from app.db import OrderLine
    db.query(OrderLine).delete(); db.commit()
    _grove(db, starts_on=D("2026-07-25"), ends_on=D("2026-10-31"),
          order_starts_on=D("2026-07-25"), order_ends_on=D("2026-10-31"))
    assert [e.kind for e in expected_for(db, "2026-07")] == ["monthly"]


def test_a_short_run_still_owes_its_lifetime(db):
    """A campaign that ended in the first days of the month ran for its whole
    flight. That only two of those days fall in this cycle says nothing about
    the report it owes."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch
    D = dt.date.fromisoformat

    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    _grove(db, starts_on=D("2025-01-01"), ends_on=D("2026-07-02"),
          order_starts_on=D("2025-01-01"), order_ends_on=D("2026-07-02"))
    kinds = {e.kind for e in expected_for(db, "2026-07")}
    assert "lifetime" in kinds


def test_a_lifetime_already_delivered_is_not_asked_for_again(db):
    """A client running in two markets is asked for the same lifetime twice -
    the report attaches to one row and the other sits on "Not received" for
    good, and the only way to clear it is to pull a duplicate."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, Report
    D = dt.date.fromisoformat

    b = Batch(email_subject="x", received_at=dt.datetime(2026, 7, 6))
    db.add(b); db.flush()
    db.add(Report(batch_id=b.id, filename="Lifetime_The Grove Event Venue 54913.pdf",
                  client="The Grove Event Venue", account_ids="54913",
                  market="7 Mountains KY", period="2026-07", is_lifetime=True,
                  review_state="reviewed"))
    for market in ("7 Mountains KY", "7 Mountains PA"):
        _grove(db, market=market, starts_on=D("2025-01-01"), ends_on=D("2026-07-02"),
               order_starts_on=D("2025-01-01"), order_ends_on=D("2026-07-02"))
    db.commit()

    skipped: list = []
    life = [e for e in expected_for(db, "2026-07", skipped=skipped)
            if e.kind == "lifetime"]
    assert len(life) == 1 and life[0].report is not None
    assert any("already delivered" in r["why"] for r in skipped)


def test_an_extended_campaign_goes_back_on_the_schedule(db):
    """Not often, but it happens: the end date moves out after the lifetime has
    gone. The report that went out was about a different finish, so the client
    is back on monthlies and owes a new lifetime when it ends for real."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, Report
    D = dt.date.fromisoformat

    b = Batch(email_subject="x", received_at=dt.datetime(2026, 6, 6))
    db.add(b); db.flush()
    db.add(Report(batch_id=b.id, filename="Lifetime_The Grove Event Venue 54913.pdf",
                  client="The Grove Event Venue", account_ids="54913",
                  market="7 Mountains KY", period="2026-05", is_lifetime=True,
                  review_state="reviewed"))
    # Extended: it now ends in July, so July owes both.
    _grove(db, starts_on=D("2025-01-01"), ends_on=D("2026-07-31"),
          order_starts_on=D("2025-01-01"), order_ends_on=D("2026-07-31"))
    db.commit()
    kinds = {e.kind for e in expected_for(db, "2026-07")}
    assert kinds == {"monthly", "lifetime"}


def test_a_lifetime_paces_against_the_whole_flight(db):
    """"523,636 / 1, +52,363,500% over" - the export has four columns called
    total_campaign_impressions and the populated one carries 0.999999999999,
    a share of goal rather than a count. So a total under a thousand is not a
    campaign total, and the figure is built from the monthly goal instead."""
    import datetime as dt
    from app.roster import ordered_for
    from app.db import OrderLine
    D = dt.date.fromisoformat

    db.add(OrderLine(market="7 Mountains NY Olean/Hornell", client="Burt Young Sales",
                     account_ids="51751", line_ids="122691", product="Social Mirror",
                     campaign="Social Mirror Ads", live=True,
                     impressions=87_000, budget=1500,
                     starts_on=D("2026-01-09"), ends_on=D("2026-07-08"),
                     order_starts_on=D("2026-01-09"), order_ends_on=D("2026-07-08")))
    db.commit()

    month = ordered_for(db, "Burt Young Sales", "51751", "2026-07")
    assert month["Social Mirror"]["impressions"] == 87_000

    life = ordered_for(db, "Burt Young Sales", "51751", "2026-07", lifetime=True)
    # Six months of a six-month flight, not one month and not a ratio.
    assert life["Social Mirror"]["impressions"] == 87_000 * 6
    assert "6 months" in life["Social Mirror"]["basis"]


def test_a_share_of_goal_is_not_an_impression_total(db):
    from app.db import OrderLine
    from app.orders_io import import_io_export
    head = ("client_business_unit,orders_status,client,orders_id,product,id,"
            "status,orders_start_date,start_date,end_date,orders_end_date,"
            "monthly_campaign_impressions,total_campaign_impressions\n")
    row = ("BU,IO Live,Acme,1,Social Mirror Ads,10,IO Live,"
           "2026-01-01,2026-01-01,2026-12-31,2026-12-31,87000,0.999999999999\n")
    import_io_export(db, (head + row).encode(), period="2026-07")
    line = db.query(OrderLine).one()
    assert line.impressions == 87000
    assert line.total_impressions is None


def test_an_ended_order_that_overlaps_a_running_one_waits(db):
    """The client is not dark - the buy changed shape - and a campaign-to-date
    report in the middle of a live campaign is not what a lifetime is for."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="M", client="C", account_ids="1", line_ids="1",
                     product="Mobile Conquesting", campaign="Mobile Conquesting Ads",
                     live=True, starts_on=D("2025-12-17"), ends_on=D("2026-07-31"),
                     order_starts_on=D("2025-12-17"), order_ends_on=D("2026-07-31")))
    db.add(OrderLine(market="M", client="C", account_ids="2", line_ids="2",
                     product="Meta", campaign="Meta Display & Video Ads",
                     live=True, starts_on=D("2026-02-01"), ends_on=D("2026-10-14"),
                     order_starts_on=D("2026-02-01"), order_ends_on=D("2026-10-14")))
    db.commit()
    skipped: list = []
    kinds = {e.kind for e in expected_for(db, "2026-07", skipped=skipped)}
    assert kinds == {"monthly"}
    assert any("still running" in r["why"] for r in skipped)


def test_a_later_order_that_does_not_overlap_still_gets_its_lifetime(db):
    """Field Of Dreams: Mobile Conquesting ended 13 July, Display starts on the
    28th. Two campaigns, not one - so the first one gets its lifetime."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="M", client="Field Of Dreams", account_ids="51118",
                     line_ids="120253", product="Mobile Conquesting",
                     campaign="Mobile Conquesting Ads", live=True,
                     starts_on=D("2025-12-17"), ends_on=D("2026-07-13"),
                     order_starts_on=D("2025-12-17"), order_ends_on=D("2026-07-13")))
    db.add(OrderLine(market="M", client="Field Of Dreams", account_ids="55216",
                     line_ids="133917", product="Display", campaign="Display Ads",
                     live=True, starts_on=D("2026-07-28"), ends_on=D("2026-10-14"),
                     order_starts_on=D("2026-07-28"), order_ends_on=D("2026-10-14")))
    db.commit()
    rows = expected_for(db, "2026-07")
    life = [e for e in rows if e.kind == "lifetime"]
    assert len(life) == 1
    # And it covers the campaign that ended, not the one that just started.
    assert life[0].ends_on == D("2026-07-13")
    assert life[0].products == ["Mobile Conquesting"]


def test_a_live_chat_order_does_not_hold_up_a_lifetime(db):
    """RIVER VALLEY BUILDERS. Their Mobile Conquesting ended in July and the
    lifetime never appeared, because two other orders were still open - both
    Live Chat, which gets no report of its own. Nobody is waiting on those."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="M", client="River Valley Builders",
                     account_ids="31050", line_ids="1",
                     product="Mobile Conquesting", campaign="Mobile Conquesting Ads",
                     live=True, starts_on=D("2025-12-01"), ends_on=D("2026-07-31"),
                     order_starts_on=D("2025-12-01"), order_ends_on=D("2026-07-31")))
    for order, line in (("31171", "2"), ("43182", "3")):
        db.add(OrderLine(market="M", client="River Valley Builders",
                         account_ids=order, line_ids=line, product="Live Chat",
                         campaign="Live Chat", live=True,
                         starts_on=D("2025-01-01"), ends_on=D("2026-12-31"),
                         order_starts_on=D("2025-01-01"),
                         order_ends_on=D("2026-12-31")))
    db.commit()
    assert "lifetime" in {e.kind for e in expected_for(db, "2026-07")}


def test_a_real_running_order_still_holds_up_a_lifetime(db):
    """The same client with one live Meta order instead. That one is a campaign
    somebody is reading, so the lifetime waits for it."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="M", client="River Valley Builders",
                     account_ids="31050", line_ids="1",
                     product="Mobile Conquesting", campaign="Mobile Conquesting Ads",
                     live=True, starts_on=D("2025-12-01"), ends_on=D("2026-07-31"),
                     order_starts_on=D("2025-12-01"), order_ends_on=D("2026-07-31")))
    db.add(OrderLine(market="M", client="River Valley Builders",
                     account_ids="31171", line_ids="2", product="Live Chat",
                     campaign="Live Chat", live=True,
                     starts_on=D("2025-01-01"), ends_on=D("2026-12-31"),
                     order_starts_on=D("2025-01-01"), order_ends_on=D("2026-12-31")))
    db.add(OrderLine(market="M", client="River Valley Builders",
                     account_ids="52000", line_ids="3", product="Meta",
                     campaign="Meta Display & Video Ads", live=True,
                     starts_on=D("2026-02-01"), ends_on=D("2026-10-14"),
                     order_starts_on=D("2026-02-01"), order_ends_on=D("2026-10-14")))
    db.commit()
    assert "lifetime" not in {e.kind for e in expected_for(db, "2026-07")}


def test_every_line_marked_io_complete_owes_a_lifetime(db):
    """ORDER 45911, SORGE FUNERAL HOME. Every line reads IO Complete and the end
    dates run into December, so nothing had "ended" and no lifetime was asked
    for. Complete is the seller saying it is finished - that is the same
    statement the end date makes, only earlier."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="M", client="Sorge Funeral Home", account_ids="45911",
                     line_ids="1", product="Display", campaign="Display Ads",
                     live=False, complete=True,
                     starts_on=D("2025-01-01"), ends_on=D("2026-12-31"),
                     order_starts_on=D("2025-01-01"), order_ends_on=D("2026-12-31")))
    db.commit()
    assert "lifetime" in {e.kind for e in expected_for(db, "2026-07")}


def test_one_stopped_line_under_a_running_client_is_not_a_lifetime(db):
    """7 MOUNTAINS GREW BY A HUNDRED ROWS OVERNIGHT. Reading "cancelled or
    complete" off a single line item put a lifetime on the board for the whole
    client - and a cancelled line inside a live order is an ordinary thing,
    while every campaign that ever ended sits at IO Complete forever."""
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="M", client="Busy Client", account_ids="1",
                     line_ids="1", product="Display", campaign="Display Ads",
                     live=False, canceled=True,
                     starts_on=D("2025-01-01"), ends_on=D("2026-12-31"),
                     order_starts_on=D("2025-01-01"), order_ends_on=D("2026-12-31")))
    db.add(OrderLine(market="M", client="Busy Client", account_ids="1",
                     line_ids="2", product="Meta", campaign="Meta Display & Video Ads",
                     live=True,
                     starts_on=D("2025-01-01"), ends_on=D("2026-12-31"),
                     order_starts_on=D("2025-01-01"), order_ends_on=D("2026-12-31")))
    db.commit()
    kinds = {e.kind for e in expected_for(db, "2026-07")}
    assert kinds == {"monthly"}


def test_the_lifetime_only_expects_what_that_campaign_ran(db):
    """"Ordered but not on the report: Display" on a lifetime covering Dec to
    July, where the Display order starts on 28 July."""
    import datetime as dt
    from app.roster import expected_products
    from app.db import OrderLine
    D = dt.date.fromisoformat
    db.add(OrderLine(market="M", client="Field Of Dreams", account_ids="51118",
                     line_ids="120253", product="Mobile Conquesting",
                     campaign="Mobile Conquesting Ads", live=True,
                     starts_on=D("2025-12-17"), ends_on=D("2026-07-13")))
    db.add(OrderLine(market="M", client="Field Of Dreams", account_ids="55216",
                     line_ids="133917", product="Display", campaign="Display Ads",
                     live=True, starts_on=D("2026-07-28"), ends_on=D("2026-10-14")))
    db.commit()
    got = expected_products(db, "Field Of Dreams", "51118", period="2026-07",
                            lifetime=True,
                            window=(D("2025-12-17"), D("2026-07-13")))
    assert got == {"Mobile Conquesting"}


def test_one_month_campaigns_owe_only_their_lifetime(db):
    import datetime as dt
    from app.board import expected_for
    from app.db import Batch, OrderLine
    D = dt.date.fromisoformat
    db.add(Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(OrderLine(market="M", client="Pop Up", account_ids="9", line_ids="9",
                     product="Meta", campaign="Meta Display & Video Ads",
                     live=True, starts_on=D("2026-07-02"), ends_on=D("2026-07-29"),
                     order_starts_on=D("2026-07-02"), order_ends_on=D("2026-07-29")))
    db.commit()
    skipped: list = []
    kinds = [e.kind for e in expected_for(db, "2026-07", skipped=skipped)]
    assert kinds == ["lifetime"]
    assert any("covers the same ground" in r["why"] for r in skipped)


def test_a_finished_campaign_under_its_goal_is_flagged():
    from app.checks.rules import check_lifetime_goal
    text = (" Line Item Performance\n"
            " Acme - Keyword Social Mirror   400,000   100   0.03%\n")
    ordered = {"Social Mirror": {"impressions": 1_000_000, "budget": None,
                                 "basis": "6 months at the monthly figure"}}
    out = check_lifetime_goal({"is_lifetime": True, "text": text,
                               "ordered": ordered})
    assert len(out) == 1 and out[0]["severity"] == "warn"
    assert "60% under" in out[0]["title"]
    # Inside the band, nothing to say.
    ordered["Social Mirror"]["impressions"] = 420_000
    assert check_lifetime_goal({"is_lifetime": True, "text": text,
                                "ordered": ordered}) == []


def test_a_ctv_plus_video_buy_paces_as_one_row(db):
    """"CTV + Video Ads" is one line item with one goal. Pacing each half
    against the whole goal said CTV was 46% short while Video had nothing to
    compare against at all."""
    import datetime as dt
    from app.db import OrderLine
    from app.orders_io import import_io_export
    from app.roster import ordered_for
    from app.checks.served import pacing_rows

    head = ("client_business_unit,orders_status,client,orders_id,product,id,"
            "status,orders_start_date,start_date,end_date,orders_end_date,"
            "monthly_campaign_impressions\n")
    row = ("BU,IO Live,WVU,45411,CTV + Video Ads,104706,IO Live,"
           "2025-07-30,2025-07-30,2026-07-31,2026-07-31,87500\n")
    import_io_export(db, (head + row).encode(), period="2026-07")
    assert {l.sold_with for l in db.query(OrderLine).all()} == {"CTV, Video"}

    want = ordered_for(db, "WVU", "45411", "2026-07")
    assert list(want) == ["CTV, Video"]

    text = (" Line Item Performance\n"
            " WVU - Behavioral CTV        572,099   100  0.02%\n"
            " WVU - Pre-roll Video        676,234   200  0.03%\n")
    rows = pacing_rows(text, want)
    assert rows[0]["product"] == "CTV, Video"
    assert rows[0]["served"] == 572_099 + 676_234


def test_products_bought_by_the_month_are_not_paced(db):
    """Live Chat and SEO have no impressions to pace and no spend on the
    report, so a row for them is a row of dashes."""
    from app.checks.served import pacing_rows
    rows = pacing_rows(" Line Item Performance\n Acme - Keyword Social Mirror  10  1  1%\n",
                       {"Live Chat": {"impressions": None, "budget": None},
                        "SEO": {"impressions": None, "budget": None},
                        "Social Mirror": {"impressions": 100, "budget": None}})
    assert [r["product"] for r in rows if not r.get("total")] == ["Social Mirror"]



# ------------------------------- a cancelled line is not part of what is owed
def test_cancelled_line_items_are_out_of_the_pacing_goal(db):
    """Houston Concierge Medicine's Social Mirror is three line items on order
    54985 added together: two cancelled at 120,000 each and one live at
    100,000. Paced against all 340,000 the report read 84% short of a goal
    that two thirds of was called off."""
    from app.orders_io import import_io_export
    from app.roster import ordered_for
    head = ("client_business_unit,orders_status,client,orders_id,product,id,"
            "status,orders_start_date,start_date,end_date,orders_end_date,"
            "monthly_campaign_impressions\n")
    rows = ("BU,IO Live,Houston Concierge Medicine,54985,Social Mirror Ads,133137,"
            "Cancelled,2026-07-15,2026-07-15,2026-10-14,2026-10-14,120000\n"
            "BU,IO Live,Houston Concierge Medicine,54985,Social Mirror Ads,133139,"
            "Cancelled,2026-07-15,2026-07-15,2026-10-14,2026-10-14,120000\n"
            "BU,IO Live,Houston Concierge Medicine,54985,Social Mirror Ads,133136,"
            "IO Live,2026-07-16,2026-07-16,2026-10-14,2026-10-14,100000\n")
    import_io_export(db, (head + rows).encode(), period="2026-07")
    got = ordered_for(db, "Houston Concierge Medicine", "54985", "2026-07")
    assert got["Social Mirror"]["impressions"] == 100_000
    # And one cancelled line beside a live one is not a campaign that was
    # called off - marking it so silenced the finding on the half still
    # delivering.
    assert got["Social Mirror"]["stopped"] is False


def test_a_wholly_cancelled_product_is_not_paced_at_all(db):
    from app.orders_io import import_io_export
    from app.roster import ordered_for
    head = ("client_business_unit,orders_status,client,orders_id,product,id,"
            "status,orders_start_date,start_date,end_date,orders_end_date,"
            "monthly_campaign_impressions\n")
    rows = ("BU,IO Live,Acme,700,Social Mirror Ads,8001,Cancelled,"
            "2026-07-01,2026-07-01,2026-10-14,2026-10-14,120000\n")
    import_io_export(db, (head + rows).encode(), period="2026-07")
    # Every line cancelled means the row itself is not live, and a product
    # nobody is running has no goal to be short of.
    assert ordered_for(db, "Acme", "700", "2026-07") == {}


def test_an_order_header_that_cannot_contain_its_line_item_is_set_aside(db):
    """MANNING MEDIA, order 55987, line item 136061. The IO tool shows that
    line running 29 June to 31 July 2026. The export's order header says the
    order ran 21 March to 18 May 2018 - six years before its own line item and
    not overlapping it at all - and that 2018 was what put Manning on the pull
    list asking for a six-year range.

    What separates it from ReThink's 4701, where the header IS the thing that
    reaches the old complete line items, is that 55987's header ENDS before its
    own line item does. A header that cannot contain its line is not describing
    it.
    """
    import datetime as _d
    D = _d.date
    from app.main import pull_range_rows, pull_range_why
    db.add_all([
        # The header agrees and reaches further back: it is used.
        OrderLine(market="ReThink Media Group", client="Memorial Hospital",
                  account_ids="4701", live=True, product="Native Display",
                  starts_on=D(2021, 8, 11), ends_on=D(2026, 12, 31),
                  order_starts_on=D(2018, 11, 1), order_ends_on=D(2026, 12, 31)),
        # The header starts AFTER its own line item: the line item wins.
        OrderLine(market="Hilbing", client="H", account_ids="36184", live=True,
                  product="Display", starts_on=D(2018, 1, 1), ends_on=D(2026, 12, 31),
                  order_starts_on=D(2024, 2, 7), order_ends_on=D(2026, 12, 31)),
        # The header does not overlap its line item: set aside.
        OrderLine(market="Manning Media", client="Transit of Frederick",
                  account_ids="55987", line_ids="136061", product="Social Mirror",
                  canceled=True, live=False, status="Cancelled",
                  starts_on=D(2026, 6, 29), ends_on=D(2026, 7, 31),
                  order_starts_on=D(2018, 3, 21), order_ends_on=D(2018, 5, 18)),
    ])
    db.commit()

    got = dict((m, e) for m, e, _n in pull_range_rows(db, today=D(2026, 8, 26)))
    assert got["ReThink Media Group"] == D(2018, 11, 1), "the header still counts"
    assert got["Hilbing"] == D(2018, 1, 1), "the line item reaches further"
    assert got["Manning Media"] == D(2026, 6, 29), "not 2018-03-21"

    # And the panel marks the row whose header was set aside, with both windows.
    row = pull_range_why(db, "Manning Media", today=D(2026, 8, 26))[0]
    assert row["odd"] is True
    assert row["starts"] == D(2026, 6, 29) and row["order_starts"] == D(2018, 3, 21)


def test_flat_products_are_out_of_the_pacing_panel_entirely(db):
    """Website Visitor ID and Additional Billing are sold by the month.

    NOT_PACED was a hand-written list and one of its entries read "Visitor ID"
    - which is not what the product is called - so the line meant to keep it
    out never matched, and every order carrying it got a row saying
    "-/- no comparison". Additional Billing was not in the list at all.

    And the total has to count what the rows count. It was summing every
    product in the order, so a goal with no row above it was still in the
    denominator and a flat product's delivery was still in the numerator.
    """
    from app.checks.served import is_paced, pacing_rows

    for flat in ("Website Visitor ID", "Additional Billing", "SEO",
                 "Live Chat", "Geo-Framing"):
        assert not is_paced(flat), f"{flat} is being paced"
    for real in ("Meta", "Mobile Conquesting", "Social Mirror", "CTV, Video"):
        assert is_paced(real), f"{real} stopped being paced"

    text = (" Line Item Performance\n"
            " Acme - Meta Ads                 90,000   100  0.11%\n"
            " Acme - Website Visitor ID       25,000    10  0.04%\n")
    rows = pacing_rows(text, {
        "Meta": {"impressions": 100_000, "budget": None},
        "Website Visitor ID": {"impressions": 40_000, "budget": None},
        "Additional Billing": {"impressions": None, "budget": 600},
        "SEO": {"impressions": None, "budget": 2660},
        "Live Chat": {"impressions": None, "budget": 300}})
    assert [r["product"] for r in rows if not r.get("total")] == ["Meta"]
    total = next(r for r in rows if r.get("total"))
    assert total["ordered"] == 100_000, "a flat product's goal is in the total"
    assert total["served"] == 90_000, "a flat product's delivery is in the total"
    # Not silently: 25,000 impressions vanishing from a total reads as the
    # total being broken.
    assert total["flat"] == 25_000


def test_an_order_closing_out_brings_its_finished_lines_with_it(db):
    """Grav order 51430 - a lifetime is a report on a whole campaign.

    The order ends 15 August. Its SEO line runs to that date; its Social Mirror
    Ads and Website Visitor ID lines stopped on 15 June, so the import threw
    them away as "ended before August" and the only thing left to build a
    lifetime out of was the one product that is never owed one. The board then
    said "SEO is not owed a lifetime" about an order owed one for everything
    else on it.

    What is owed here is two reports: a lifetime for Social Mirror, and a
    separate August SEO report, which is a different file pulled by hand.
    """
    from app.db import OrderLine
    from app.orders_io import import_io_export
    from app.board import expected_for

    head = ("client_business_unit,orders_status,client,orders_id,product,id,"
            "status,orders_start_date,start_date,end_date,orders_end_date,"
            "monthly_campaign_impressions\n")
    rows = (
        "Mobile 1st,IO Complete,Grav,51430,SEO,121309,IO Complete,"
        "2026-02-15,2026-02-15,2026-08-15,2026-08-15,\n"
        "Mobile 1st,IO Complete,Grav,51430,Social Mirror Ads,121307,IO Complete,"
        "2026-02-15,2026-02-15,2026-06-15,2026-08-15,375000\n"
        "Mobile 1st,IO Complete,Grav,51430,Website Visitor ID,121850,IO Complete,"
        "2026-02-15,2026-02-15,2026-06-15,2026-08-15,\n")
    import_io_export(db, (head + rows).encode(), period="2026-08")

    got = {l.product for l in db.query(OrderLine).all()}
    assert "Social Mirror" in got, (
        "the finished half of an order that closes out this cycle was dropped, "
        "so there is nothing to write its lifetime about")

    board = [(e.kind, tuple(e.products)) for e in expected_for(db, "2026-08")
             if e.client == "Grav"]
    assert ("lifetime", ("Social Mirror",)) in board
    assert ("seo", ("SEO",)) in board
    assert not any(k == "lifetime" and "SEO" in p for k, p in board), (
        "SEO is bought by the month - there is no campaign to close out")


def test_a_line_that_ended_before_a_live_order_is_still_dropped(db):
    """The rescue is for orders CLOSING OUT, not for every old line item.

    An order still running into next year has no lifetime coming, so its line
    items that finished months ago are exactly the ones that put a Social
    Mirror on a July report a year after it stopped.
    """
    from app.db import OrderLine
    from app.orders_io import import_io_export

    head = ("client_business_unit,orders_status,client,orders_id,product,id,"
            "status,orders_start_date,start_date,end_date,orders_end_date,"
            "monthly_campaign_impressions\n")
    rows = (
        "BU,IO Live,Acme,60001,Display,700001,IO Live,"
        "2026-01-01,2026-01-01,2026-12-31,2026-12-31,100000\n"
        "BU,IO Live,Acme,60001,Social Mirror Ads,700002,IO Complete,"
        "2026-01-01,2026-01-01,2026-03-31,2026-12-31,50000\n")
    import_io_export(db, (head + rows).encode(), period="2026-08")
    got = {l.product for l in db.query(OrderLine).all()}
    assert got == {"Display"}, f"kept a stale line off a live order: {got}"


def test_a_cancelled_line_is_out_of_the_lifetime_goal(db):
    """Paragon Casino Resort, order 55583.

    Two Social Mirror line items: 134958 cancelled at 97,500 and 134957 live at
    85,000. The monthly panel has taken cancelled lines out for a while; the
    lifetime panel was still reading the rolled-up row, which is every line
    item added together - so the lifetime was paced against 182,500 and read
    "53% short" of a goal more than half of which was called off.
    """
    from app.orders_io import import_io_export
    from app.roster import ordered_for

    head = ("client_business_unit,orders_status,client,orders_id,product,id,"
            "status,orders_start_date,start_date,end_date,orders_end_date,"
            "monthly_campaign_impressions\n")
    rows = (
        "Lotus Las Vegas,IO Live,Paragon Casino Resort,55583,Social Mirror Ads,"
        "134958,Cancelled,2026-07-14,2026-07-14,2026-07-31,2026-08-29,97500\n"
        "Lotus Las Vegas,IO Live,Paragon Casino Resort,55583,Social Mirror Ads,"
        "134957,IO Live,2026-08-06,2026-08-06,2026-08-29,2026-08-29,85000\n")
    import_io_export(db, (head + rows).encode(), period="2026-08")

    life = ordered_for(db, "Paragon Casino Resort", "55583", "2026-08",
                       lifetime=True)
    assert life["Social Mirror"]["impressions"] == 85_000, (
        f"the cancelled 97,500 is still in the goal: {life}")
    # And the derived basis names the months actually multiplied, not the
    # merged span across a cancelled July line and a live August one.
    assert life["Social Mirror"]["basis"].startswith("1 month "), life

    month = ordered_for(db, "Paragon Casino Resort", "55583", "2026-08")
    assert month["Social Mirror"]["impressions"] == 85_000
