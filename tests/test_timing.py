"""The page that answers "why is it slow" with numbers instead of guesses.

Every theory about the slowness was argued from a local copy that said 1.4
seconds while the real box said a minute, and two of them shipped as fixes
before anybody measured the thing they were meant to fix. These tests are about
the measuring, not the fixing.
"""
import datetime as dt
import importlib

import pytest


@pytest.fixture()
def live(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'slow.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTO_RECHECK", "false")
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    from fastapi.testclient import TestClient
    from app import main as mmod
    importlib.reload(mmod)
    from app import timing
    timing.RECENT.clear()

    db = db_mod.SessionLocal()
    yield TestClient(mmod.app), db, db_mod, timing
    db.close()
    monkeypatch.undo()
    importlib.reload(cfg_mod)
    importlib.reload(db_mod)
    importlib.reload(mmod)


def test_the_stopwatch_records_a_request(live):
    client, _db, _dbm, timing = live
    client.get("/cycle")
    rows = timing.recent(50)
    assert rows, "the middleware recorded nothing"
    row = next((r for r in rows if r["path"] == "/cycle"), None)
    assert row is not None
    assert row["seconds"] >= 0
    # THE QUERY COUNT IS THE POINT. A number that is always zero looks exactly
    # like a page that runs no queries, which is the one thing it never is.
    assert row["queries"] > 0
    assert row["db_seconds"] >= 0


def test_the_query_counter_survives_the_threadpool(live):
    """The route runs in a worker thread, not the one that started the clock.

    A plain context variable set in there is set in a COPY of the context and
    the middleware never sees it, so the counter is a mutable box instead. This
    is the test that catches it going back to an int.
    """
    client, _db, _dbm, timing = live
    client.get("/orders")
    row = next(r for r in timing.recent(50) if r["path"] == "/orders")
    assert row["queries"] > 0


def test_every_query_is_counted_once(live):
    """The listener is on the Engine class and this module gets reloaded.

    Registered twice, it counts twice, and the page reports a query load the
    board never had.
    """
    client, _db, _dbm, timing = live
    client.get("/people")            # warm anything that is only asked once
    timing.RECENT.clear()
    client.get("/people")
    first = next(r for r in timing.recent(50) if r["path"] == "/people")
    from app import main as mmod
    importlib.reload(mmod)
    timing.RECENT.clear()
    client.get("/people")
    second = next(r for r in timing.recent(50) if r["path"] == "/people")
    assert second["queries"] == first["queries"], \
        "reloading the app registered the listener again and doubled the count"


def test_the_page_loads_and_names_the_worker(live):
    client, _db, _dbm, _t = live
    r = client.get("/why-slow")
    assert r.status_code == 200
    assert "Why is it slow" in r.text
    assert "Restarts" in r.text


def test_drawing_the_page_is_timed_separately_from_building_it(live):
    """A four second page with a fifth of a second in the database is one of
    two completely different problems, and the total cannot tell them apart."""
    client, _db, _dbm, timing = live
    client.get("/cycle")
    row = next(r for r in timing.recent(50) if r["path"] == "/cycle")
    assert "render" in row["phases"], "the template render was not timed"
    assert row["phases"]["render"] <= row["seconds"] + 0.01


def test_a_slow_page_writes_itself_down(live):
    """The in-memory list needs somebody at the screen while it happens, on the
    right one of the two workers. This one can be read the next day."""
    client, db, db_mod, _t = live
    from app import main as mmod
    mmod.SLOW_SECONDS = 0.0              # everything counts as slow
    try:
        client.get("/cycle")
    finally:
        mmod.SLOW_SECONDS = 3.0
    rows = db.query(db_mod.SlowRequest).all()
    assert rows, "a slow page left no record"
    assert rows[0].path == "/cycle"
    assert rows[0].queries > 0
    assert "render" in (rows[0].phases or {})


def test_the_health_check_is_never_logged_as_slow(live):
    """The platform pings it every few seconds. A bad minute would write a
    thousand rows and bury the pages somebody actually waited on."""
    client, db, db_mod, _t = live
    from app import main as mmod
    mmod.SLOW_SECONDS = 0.0
    try:
        client.get("/healthz")
    finally:
        mmod.SLOW_SECONDS = 3.0
    assert db.query(db_mod.SlowRequest).count() == 0


def test_a_busy_box_says_the_number_might_not_be_ours(live):
    """Inside a container the load average is usually the whole host's."""
    _c, _db, _dbm, timing = live
    real = timing.load_average
    timing.load_average = lambda: (7.5, 7.4, 7.2)
    try:
        lines = timing.verdict(boots_last_hour=0)
    finally:
        timing.load_average = real
    assert any("neighbors" in line for line in lines)


def test_the_worker_wrote_down_that_it_started(live):
    client, db, db_mod, _t = live
    # The startup hook only fires when the client is entered as a context
    # manager, which is the whole reason the boot record is easy to lose.
    with client:
        client.get("/healthz")
    boots = db.query(db_mod.WorkerBoot).all()
    assert boots, "a worker came up and left no record of it"
    assert boots[0].build


def test_a_flood_of_restarts_is_called_out(live):
    _c, _db, _dbm, timing = live
    lines = timing.verdict(boots_last_hour=6)
    assert any("restarted 6 times" in line for line in lines)


def test_a_quiet_worker_says_the_time_is_being_spent_elsewhere(live):
    _c, _db, _dbm, timing = live
    timing.RECENT.clear()
    for _ in range(8):
        timing.record("/cycle", "GET", 200, 0.4, 20, 0.1)
    lines = timing.verdict(boots_last_hour=0)
    assert any("before the request reaches this code" in line for line in lines)


def test_the_json_shape_is_pasteable(live):
    client, _db, _dbm, _t = live
    body = client.get("/healthz/slow").json()
    for key in ("build", "pid", "uptime_seconds", "boots_last_hour", "recent"):
        assert key in body


def test_memory_limit_is_a_number_or_nothing():
    """Never the v1 sentinel, which is a nine-exabyte way of saying no limit."""
    from app import timing
    val = timing.memory_limit_mb()
    assert val is None or 0 < val < 64 * 1024


def test_a_big_upload_is_not_read_into_memory_three_times(tmp_path):
    """148 MB of bytes, decoded and wrapped, peaked at 1,046 MB on a 512 MB box.

    Over the threshold it goes to disk and takes the streaming path, and the
    rows that come back have to be identical either way - a fix that quietly
    changed what was imported would be worse than the leak.
    """
    from app import orders_io
    header = "order_id,line_item_id,client,product,start_date,end_date\n"
    body = "".join(f"{i},{i}0,Client {i},Social Mirror Ads,2026-08-01,2026-08-31\n"
                   for i in range(160000))
    blob = (header + body).encode()
    assert len(blob) > 8 * 1024 * 1024, "the fixture is too small to take the path"
    big = list(orders_io._open_source(blob))
    small = list(orders_io._open_source((header + body[:200]).encode()))
    assert len(big) == 160000
    assert big[0] == small[0], "the two paths read the same row differently"


def test_shortening_a_name_still_knows_a_real_clash():
    """The clash set is worked out once per roster now instead of once per
    label. Same answers, or the saving is worthless."""
    from app.partners import first_name
    roster = {"Katie Oxman", "Katie Reed", "Lauren Hunter", "Todd Beal"}
    assert first_name("Katie Oxman", roster) == "Katie Oxman"
    assert first_name("Lauren Hunter", roster) == "Lauren"
    # A DIFFERENT ROSTER MUST GET A DIFFERENT ANSWER. The cache is keyed on the
    # set of names, so this is the test that catches it being keyed on nothing.
    assert first_name("Katie Oxman", {"Katie Oxman", "Lauren Hunter"}) == "Katie"


def test_one_spelling_and_a_full_name_is_one_person_not_a_clash():
    from app.partners import first_name
    assert first_name("Lauren Hunter", {"Lauren", "Lauren Hunter"}) == "Lauren"


def test_the_audit_page_folds_away_its_wall_of_partner_names():
    """A hundred and thirty-seven names sat between the heading and the tables
    people open that page to read."""
    from pathlib import Path
    tpl = (Path(__file__).resolve().parents[1] / "app" / "templates"
           / "audit.html").read_text()
    assert "Which partners" in tpl
    assert "the list covers: <b>" not in tpl


def test_a_live_chat_only_order_is_explained_as_a_rule_not_a_missing_line(live):
    """It said "not in the export, or the export is out of date" for an order
    sitting right there, which sends somebody to check a feed for nothing."""
    import datetime as dt
    _c, db, db_mod, _t = live
    D = dt.date.fromisoformat
    db.add(db_mod.OrderLine(market="7 Mountains Media", client="7 Mountains Media",
                            account_ids="26734", product="Live Chat",
                            starts_on=D("2026-08-01"), ends_on=D("2026-12-31")))
    db.commit()
    from app.audit import _why
    why = _why(db, "2026-08",
               {"client": "7 Mountains Media", "ids": ["26734"],
                "kind": "monthly"}, [])
    assert "does not earn a report on its own" in why
    assert "not in the export" not in why


def test_an_order_that_really_is_missing_still_says_so(live):
    _c, db, _dbm, _t = live
    from app.audit import _why
    why = _why(db, "2026-08",
               {"client": "Nobody", "ids": ["99999"], "kind": "monthly"}, [])
    assert "not in the export" in why


def test_check_a_list_says_which_cycle_it_is_deciding(live):
    """Every approve and reject on it is scoped to one cycle and nothing on the
    page said so, which makes a reject look permanent."""
    client, _db, _dbm, _t = live
    text = client.get("/cycle/audit").text
    assert "Everything on this page is" in text


def test_the_rail_has_a_way_in():
    from pathlib import Path
    base = (Path(__file__).resolve().parents[1] / "app" / "templates"
            / "base.html").read_text()
    assert '/why-slow' in base
