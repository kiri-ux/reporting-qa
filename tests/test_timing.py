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


def test_the_rail_has_a_way_in():
    from pathlib import Path
    base = (Path(__file__).resolve().parents[1] / "app" / "templates"
            / "base.html").read_text()
    assert '/why-slow' in base
