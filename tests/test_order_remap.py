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


def test_the_display_spelling_of_the_export_is_recognised():
    """The nightly S3 file is snake_case; a sheet pulled by hand out of the IO
    tool uses the display names. Same columns, and a reader that knows only one
    spelling rejects a good file with a confusing message."""
    from app.orders_io import looks_like_io_export, normalise_header
    assert normalise_header("Order's Status") == "orders_status"
    assert normalise_header("Client Business Unit") == "client_business_unit"
    assert normalise_header("Monthly Campaign Budget") == "monthly_campaign_budget"
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
    from app.checks.rules import check_pacing
    ctx = {"text": " PPC Ad Cost\n Amount spent\n $400.00\n", "is_lifetime": False,
           "budgets": {"PPC": 1000.0}}
    out = check_pacing(ctx)
    assert len(out) == 1 and out[0]["severity"] == "warn"
    assert "60% under budget" in out[0]["title"]


def test_a_normal_month_says_nothing():
    from app.checks.rules import check_pacing
    for spent in ("$900.00", "$1,000.00", "$1,400.00"):
        ctx = {"text": f" PPC Ad Cost\n Amount spent\n {spent}\n",
               "is_lifetime": False, "budgets": {"PPC": 1000.0}}
        assert check_pacing(ctx) == [], spent


def test_overspending_by_half_is_flagged_too():
    from app.checks.rules import check_pacing
    ctx = {"text": " PPC Ad Cost\n Amount spent\n $1,600.00\n",
           "is_lifetime": False, "budgets": {"PPC": 1000.0}}
    out = check_pacing(ctx)
    assert out and "60% over budget" in out[0]["title"]


def test_a_lifetime_report_is_not_paced():
    """It covers a campaign's whole flight, and a monthly budget says nothing
    about that."""
    from app.checks.rules import check_pacing, _rule_applies
    ctx = {"text": " PPC Ad Cost\n x\n $10.00\n", "is_lifetime": True,
           "budgets": {"PPC": 1000.0}}
    assert check_pacing(ctx) == []
    assert _rule_applies(check_pacing, ctx) is False


def test_no_budget_loaded_means_no_claim():
    from app.checks.rules import check_pacing, _rule_applies
    ctx = {"text": " PPC Ad Cost\n x\n $10.00\n", "is_lifetime": False, "budgets": {}}
    assert _rule_applies(check_pacing, ctx) is False


def test_a_product_whose_spend_is_not_on_the_report_is_skipped():
    """Most products print no spend at all. Comparing a budget against nothing
    would fail every one of them."""
    from app.checks.rules import check_pacing
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
    almost none of them need it."""
    import datetime as _d
    db.add_all([
        OrderLine(market="Recent Radio", client="a", account_ids="1", live=True,
                  product="PPC", starts_on=_d.date(2026, 5, 1)),
        OrderLine(market="Old Media", client="b", account_ids="2", live=True,
                  product="PPC", starts_on=_d.date(2018, 3, 4)),
        OrderLine(market="Old Media", client="c", account_ids="3", live=True,
                  product="Meta", starts_on=_d.date(2026, 1, 1)),
        # A finished line must not drag a partner's date backwards.
        OrderLine(market="Recent Radio", client="d", account_ids="4", live=False,
                  product="Meta", starts_on=_d.date(2011, 1, 1)),
    ])
    db.commit()

    from app.main import pull_range_rows
    got = pull_range_rows(db)
    assert [(m, e.isoformat()) for m, e, _n in got] == [
        ("Old Media", "2018-03-04"),
        ("Recent Radio", "2026-05-01"),
    ], "oldest first, and a finished line must not drag a partner back"
    assert dict((m, n) for m, _e, n in got)["Old Media"] == 2


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
    assert set(TRIGGERS) == {"button", "rules", "batch"}
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
    assert "Would be" in tpl and "none recorded" in tpl
    assert "/report/{{ rep.id }}/orders" in _P("app/templates/viewer.html").read_text()
    assert '@app.get("/report/{report_id}/orders")' in _P("app/main.py").read_text()


def test_what_todays_code_would_map_a_raw_name_to_is_shown():
    """Where that differs from the stored product, the row was written by an
    older import and that is the whole answer."""
    from app.checks.products import map_order_products
    assert map_order_products("Social Mirror CTV Ads") == ["Social Mirror CTV"]
    # The old import had no Social Mirror CTV key, so a row stored as plain
    # "Social Mirror" with this raw name is provably from older code.
