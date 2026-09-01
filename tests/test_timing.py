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


def _roto(db, db_mod):
    import datetime as dt
    D = dt.date.fromisoformat
    db.add(db_mod.Partner(partner="7 Mountains Media", reporting_team="Dana"))
    db.add(db_mod.OrderLine(
        market="7 Mountains Media", client="Roto Rooter Williamsport",
        account_ids="52290", product="Social Mirror Ads",
        starts_on=D("2026-01-01"), ends_on=D("2026-12-31")))
    # The serving file was loaded and this client is not in it, which is what
    # took the row off the board in the first place.
    db.add(db_mod.ServedDays(period="2026-08", market_key="7mountainsmedia",
                             client_key="someoneelse", market="7 Mountains Media",
                             client="Someone Else", days=20))
    db.commit()


def _rows(db):
    from app import board
    return [e for e in board.expected_for(db, "2026-08")
            if e.client == "Roto Rooter Williamsport"]


def test_approving_from_the_list_puts_the_row_back_on_the_cycle(live):
    """It was taken off for not being in the serving file, and the serving file
    was wrong. Somebody who knows the client gets the last word."""
    client, db, db_mod, _t = live
    _roto(db, db_mod)
    assert _rows(db) == []
    client.post("/cycle/audit/call", data={
        "period": "2026-08", "ref": "52290", "kind": "monthly",
        "client": "Roto Rooter Williamsport", "call": "approved",
        "note": "client wasn't linked up", "who": "k"}, follow_redirects=False)
    db.expire_all()
    back = _rows(db)
    assert len(back) == 1
    assert back[0].forced_by == "k"
    assert back[0].forced_note == "client wasn't linked up"


def test_rejecting_after_approving_takes_the_row_back_off(live):
    """The list said Rejected and the board still carried the row.

    Two screens, two answers, and the one nobody was looking at was the one the
    reporters work from.
    """
    client, db, db_mod, _t = live
    _roto(db, db_mod)
    for call in ("approved", "rejected"):
        client.post("/cycle/audit/call", data={
            "period": "2026-08", "ref": "52290", "kind": "monthly",
            "client": "Roto Rooter Williamsport", "call": call,
            "note": "changed my mind", "who": "k"}, follow_redirects=False)
    db.expire_all()
    assert _rows(db) == []
    assert db.query(db_mod.CycleDone).count() == 0


def test_clearing_a_call_also_clears_what_it_did_to_the_board(live):
    client, db, db_mod, _t = live
    _roto(db, db_mod)
    for call in ("approved", "clear"):
        client.post("/cycle/audit/call", data={
            "period": "2026-08", "ref": "52290", "kind": "monthly",
            "client": "Roto Rooter Williamsport", "call": call,
            "who": "k"}, follow_redirects=False)
    db.expire_all()
    assert _rows(db) == []


def test_a_reject_with_only_an_order_id_still_finds_the_row(live):
    """The approve looked the client up off the order id, so the reject has
    to be able to as well."""
    client, db, db_mod, _t = live
    _roto(db, db_mod)
    client.post("/cycle/audit/call", data={
        "period": "2026-08", "ref": "52290", "kind": "monthly",
        "client": "Roto Rooter Williamsport", "call": "approved",
        "who": "k"}, follow_redirects=False)
    client.post("/cycle/audit/call", data={
        "period": "2026-08", "ref": "52290", "kind": "monthly",
        "call": "rejected", "who": "k"}, follow_redirects=False)
    db.expire_all()
    assert _rows(db) == []


def test_a_reject_leaves_other_clients_overrides_alone(live):
    """One decision, one row. A sweep that clears the cycle's overrides would
    be a very quiet way to lose somebody else's work."""
    client, db, db_mod, _t = live
    _roto(db, db_mod)
    db.add(db_mod.CycleDone(period="2026-08", ident="other|client|monthly",
                            market="Other", client="Client", kind="monthly",
                            reason="needed", marked_by="someone else"))
    db.commit()
    client.post("/cycle/audit/call", data={
        "period": "2026-08", "ref": "52290", "kind": "monthly",
        "client": "Roto Rooter Williamsport", "call": "rejected",
        "who": "k"}, follow_redirects=False)
    db.expire_all()
    left = db.query(db_mod.CycleDone).all()
    assert [m.client for m in left] == ["Client"]


def test_a_one_word_market_code_is_still_a_market_code():
    """"ADM - VSCU KC" kept its prefix because this wanted two words, so the
    client came out as "ADM - VSCU KC" and matched nothing on the board."""
    from app.audit import parse_list
    got = {r["client"]: r["ids"] for r in parse_list(
        "ADM - VSCU KC #52263\n7MOU SG - Benton Rodeo #53915")}
    assert got == {"VSCU KC": ["52263"], "Benton Rodeo": ["53915"]}


def test_a_client_whose_name_starts_with_a_word_and_a_dash_keeps_it():
    """The prefix is a shouted market code. Matching it case-insensitively
    while the second word is optional would eat real client names."""
    from app.audit import parse_list
    got = [r["client"] for r in parse_list("Bliss - Digital Innovations #41000")]
    assert got == ["Bliss - Digital Innovations"]


def test_the_board_reason_is_found_under_the_name_the_board_uses(live):
    """The two tools spell clients differently - that is why this page exists -
    and the order id is the one thing they agree on.

    Four VSCU orders read "the order is loaded and looks live, worth a closer
    look" when the truth was that they ran one day in August.
    """
    import datetime as dt
    _c, db, db_mod, _t = live
    D = dt.date.fromisoformat
    db.add(db_mod.Partner(partner="Adlytics Digital Marketing LLC",
                          reporting_team="Dana"))
    db.add(db_mod.OrderLine(
        market="Adlytics Digital Marketing LLC", client="VSCU KC",
        account_ids="52263", product="Online Audio",
        starts_on=D("2026-08-31"), ends_on=D("2026-09-13"),
        order_starts_on=D("2026-02-11"), order_ends_on=D("2026-11-29")))
    db.commit()
    from app.audit import audit
    out = audit(db, "2026-08", "ADM - VSCU KC #52263")
    whys = [m["why"] for m in out["missing"]]
    assert whys, "the row should be on the list and not on the board"
    assert "worth a closer look" not in whys[0]
    assert "1 day" in whys[0]


def test_a_whole_partner_missing_from_the_export_is_called_out(live):
    """One row at a time it reads as six unrelated missing orders. It is one
    problem, it is much worse, and nothing else here would mention it."""
    _c, db, _dbm, _t = live
    from app.audit import audit
    out = audit(db, "2026-08",
                "ROI SAM - AudioGo #52029\nROI SAM - Something Else #52030")
    assert out["gone"] == [{"prefix": "ROI SAM", "rows": 2}]


def test_one_missing_order_is_not_called_a_missing_partner(live):
    """With one row under a code there is no way to tell the two apart, and a
    panel that cries partner every time is one people stop reading."""
    _c, db, _dbm, _t = live
    from app.audit import audit
    out = audit(db, "2026-08", "ROI SAM - AudioGo #52029")
    assert out["gone"] == []


def test_a_partner_with_one_real_reason_is_not_called_gone(live):
    """The claim is about the export never having heard of them, not about
    rows the board has perfectly good answers for."""
    import datetime as dt
    _c, db, db_mod, _t = live
    D = dt.date.fromisoformat
    db.add(db_mod.Partner(partner="Adlytics Digital Marketing LLC"))
    db.add(db_mod.OrderLine(
        market="Adlytics Digital Marketing LLC", client="VSCU KC",
        account_ids="52263", product="Online Audio",
        starts_on=D("2026-08-31"), ends_on=D("2026-09-13")))
    db.commit()
    from app.audit import audit
    out = audit(db, "2026-08", "ADM - VSCU KC #52263\nADM - VSCU SC #52265")
    assert out["gone"] == []


def test_the_market_code_survives_parsing():
    """It is the only thing on the row that says which partner it belongs to."""
    from app.audit import parse_list
    rows = parse_list("ROI SAM - AudioGo #52029\nBenton Rodeo #53915")
    assert rows[0]["prefix"] == "ROI SAM"
    assert rows[0]["client"] == "AudioGo"
    assert rows[1]["prefix"] == ""


def test_one_export_run_is_several_files_and_yesterday_is_not_one_of_them():
    """07:32 and 07:34 on the same morning, 227 MB then 830 MB, are one export.
    A file from last week is a picture of a different day, and merging it keeps
    whatever line item today's file did not happen to carry."""
    from app.orders_s3 import _this_mornings_run, _LAST_SKIPPED
    now = 1788000000.0
    run = sorted([(-now, "a_0734.csv"), (-(now - 120), "a_0732.csv"),
                  (-(now - 400), "stephens.csv"), (-(now - 86400), "yesterday.csv"),
                  (-(now - 8 * 86400), "lastweek.csv")])
    assert [k for _w, k in _this_mornings_run(run)] == [
        "a_0734.csv", "a_0732.csv", "stephens.csv"]
    assert _LAST_SKIPPED[0] == 2


def test_a_file_named_outright_is_never_skipped_for_being_old():
    """Somebody asked for that file by name."""
    from app.orders_s3 import _this_mornings_run
    assert [k for _w, k in _this_mornings_run([(0.0, "named.csv")])] == ["named.csv"]


def test_a_paused_line_out_of_window_is_not_called_canceled(live):
    """Order 50236: a paused Mobile Conquesting that ended 31 July, and two
    canceled lines running to December. The paused one is dropped for being out
    of the window, the canceled ones survive, and the board then said every line
    was canceled about an order the IO tool showed as Paused."""
    import datetime as dt
    _c, db, db_mod, _t = live
    D = dt.date.fromisoformat
    db.add(db_mod.Partner(partner="Kaizen Digital Marketing Group"))
    for prod in ("Online Audio", "Social Mirror Ads"):
        db.add(db_mod.OrderLine(
            market="Kaizen Digital Marketing Group", client="Buffalo Wings & Rings",
            account_ids="50236", product=prod, canceled=True,
            starts_on=D("2026-01-01"), ends_on=D("2026-12-31")))
    db.add(db_mod.OrderSync(
        source="s3://bucket/orders/", ok=True, state="done",
        dropped={"Kaizen Digital Marketing Group|Buffalo Wings & Rings":
                 "ended 2026-07-31, before August 2026"}))
    db.commit()
    from app.audit import _why
    why = _why(db, "2026-08", {"client": "Buffalo Wings & Rings",
                               "ids": ["50236"], "kind": "monthly"}, [])
    assert why == ("the only lines on this order that reach this cycle are "
                   "canceled. The other one is not canceled - it ended "
                   "2026-07-31, before August 2026")


def test_with_no_drop_recorded_the_plain_answer_is_still_given(live):
    import datetime as dt
    _c, db, db_mod, _t = live
    D = dt.date.fromisoformat
    db.add(db_mod.OrderLine(
        market="M", client="Gone Client", account_ids="50999",
        product="Online Audio", canceled=True,
        starts_on=D("2026-01-01"), ends_on=D("2026-12-31")))
    db.commit()
    from app.audit import _why
    why = _why(db, "2026-08", {"client": "Gone Client", "ids": ["50999"],
                               "kind": "monthly"}, [])
    assert why == "every line on this order is canceled"


def test_an_order_dropped_on_the_way_in_is_not_blamed_on_the_export(live):
    """"It is not in the export" was said about orders plainly IN the export.

    53437 has three paused line items that all ended on 30 June; 54338 is
    complete. The import drops everything outside the cycle and the audit only
    ever looked at the survivors, so an empty table read as an empty feed - and
    the answer accused somebody else's system and sent whoever was on this page
    to check a file that was perfectly correct.
    """
    _c, db, db_mod, _t = live
    db.add(db_mod.OrderSync(
        source="s3://bucket/orders/", ok=True, state="done",
        dropped={"evolve media|RegistryAZ": "ended 2026-06-30, before August 2026"}))
    db.commit()
    from app.audit import _why
    why = _why(db, "2026-08", {"client": "RegistryAZ", "ids": ["53437"],
                               "kind": "monthly"}, [])
    assert why == ("the export has this order and every line on it ended "
                   "2026-06-30, before August 2026")


def test_an_order_that_really_is_absent_still_says_so_after_that(live):
    """The drop log is checked first, not instead. A client nothing was ever
    recorded about has to keep the honest answer."""
    _c, db, db_mod, _t = live
    db.add(db_mod.OrderSync(
        source="s3://bucket/orders/", ok=True, state="done",
        dropped={"somewhere|Someone Else": "ended 2026-06-30, before August 2026"}))
    db.commit()
    from app.audit import _why
    why = _why(db, "2026-08", {"client": "Nobody At All", "ids": ["99999"],
                               "kind": "monthly"}, [])
    assert "not in the export" in why


def test_two_reasons_are_given_and_a_third_is_not(live):
    """A client can be dropped for more than one thing, and picking whichever
    the export listed first answers a different question each time."""
    _c, db, db_mod, _t = live
    db.add(db_mod.OrderSync(
        source="s3://bucket/orders/", ok=True, state="done",
        dropped={"a|Mixed Co": "is an RFP, not a live order",
                 "b|Mixed Co": "ended 2026-06-30, before August 2026",
                 "c|Mixed Co": "starts 2026-11-01, after August 2026"}))
    db.commit()
    from app.audit import _dropped_reason
    got = _dropped_reason(db, {"mixedco"})
    assert got.count(", and ") == 1
    assert "RFP" in got and "ended 2026-06-30" in got


def test_every_drop_reason_reads_as_a_sentence_about_the_line():
    """They are glued to "every line on it ...", so each one has to be a
    predicate. "the order status is X" reads as gibberish there."""
    import re
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "orders_io.py").read_text()
    reasons = re.findall(r'note_drop\([^,]+,\s*[^,]+,\s*\n?\s*f?"([^"]+)"', src)
    assert reasons, "no drop reasons found - has note_drop been renamed?"
    for r in reasons:
        first = r.split()[0].rstrip(",")
        assert first in {"is", "has", "ended", "starts"}, \
            f"{r!r} does not read after 'every line on it'"


def _seo_world(db, db_mod):
    import datetime as dt
    D = dt.date.fromisoformat
    db.add(db_mod.Partner(partner="Whitley Media", reporting_team="Dana"))
    db.add(db_mod.OrderLine(
        market="Whitley Media", client="Jefferson Hospital", account_ids="54153",
        product="Search Engine Optimization",
        starts_on=D("2026-01-01"), ends_on=D("2026-12-31")))
    # A serving file that was loaded and mentions somebody else entirely.
    db.add(db_mod.ServedDays(
        period="2026-08", market_key="whitleymedia", client_key="someoneelse",
        market="Whitley Media", client="Someone Else", days=25))
    db.commit()


def test_an_seo_row_is_not_judged_by_the_serving_file(live):
    """The serving file is ad delivery. SEO is not served, so it is absent from
    that file for every SEO client every month - and the rule that reads
    absence as "it did not run" took Whitley's whole SEO list off the board,
    with a reason that blamed the file for spelling the client differently.
    """
    _c, db, db_mod, _t = live
    _seo_world(db, db_mod)
    from app import board
    skipped = []
    rows = board.expected_for(db, "2026-08", skipped=skipped)
    assert [e.client for e in rows] == ["Jefferson Hospital"]
    assert not [s for s in skipped if s["client"] == "Jefferson Hospital"]


def test_seo_beside_a_digital_product_also_keeps_its_row(live):
    """ANY SEO, not ALL. Taking the row off loses the SEO report they are still
    owed, and losing a report is the expensive mistake here."""
    import datetime as dt
    _c, db, db_mod, _t = live
    _seo_world(db, db_mod)
    D = dt.date.fromisoformat
    for prod in ("Search Engine Optimization", "Social Mirror Ads"):
        db.add(db_mod.OrderLine(
            market="Whitley Media", client="Mixed Client", account_ids="54154",
            product=prod, starts_on=D("2026-01-01"), ends_on=D("2026-12-31")))
    db.commit()
    from app import board
    rows = board.expected_for(db, "2026-08")
    assert "Mixed Client" in {e.client for e in rows}


def test_a_client_the_serving_file_does_mention_is_still_judged_by_it(live):
    """Absence is the ambiguous signal. Two days served is a fact, and that
    rule stays on for everybody - SEO included."""
    import datetime as dt
    _c, db, db_mod, _t = live
    _seo_world(db, db_mod)
    D = dt.date.fromisoformat
    db.add(db_mod.OrderLine(
        market="Whitley Media", client="Barely Ran", account_ids="54155",
        product="Social Mirror Ads",
        starts_on=D("2026-01-01"), ends_on=D("2026-12-31")))
    db.add(db_mod.ServedDays(
        period="2026-08", market_key="whitleymedia", client_key="barelyran",
        market="Whitley Media", client="Barely Ran", days=2))
    db.commit()
    from app import board
    skipped = []
    rows = board.expected_for(db, "2026-08", skipped=skipped)
    assert "Barely Ran" not in {e.client for e in rows}
    assert any("2 days" in s["why"] for s in skipped if s["client"] == "Barely Ran")


def test_seo_gets_its_own_row_beside_the_digital_one(live):
    """A client running SEO and Social Mirror is owed two files, not one.

    On a single row whichever PDF arrived first satisfied the row and the other
    was never asked for again.
    """
    import datetime as dt
    _c, db, db_mod, _t = live
    D = dt.date.fromisoformat
    db.add(db_mod.Partner(partner="Whitley Media"))
    for prod in ("Search Engine Optimization", "Social Mirror Ads"):
        db.add(db_mod.OrderLine(
            market="Whitley Media", client="Mixed Client", account_ids="54154",
            product=prod, starts_on=D("2026-01-01"), ends_on=D("2026-12-31")))
    db.commit()
    from app import board
    rows = {(e.client, e.kind): e for e in board.expected_for(db, "2026-08")}
    assert ("Mixed Client", "seo") in rows
    assert ("Mixed Client", "monthly") in rows
    # AND THE PRODUCTS GO WITH THE RIGHT ROW. Two rows both saying "SEO,
    # Social Mirror Ads" would tell the reporter to pull each file twice.
    assert rows[("Mixed Client", "seo")].products == ["Search Engine Optimization"]
    assert rows[("Mixed Client", "monthly")].products == ["Social Mirror Ads"]


def test_the_tracker_reads_an_seo_row_as_an_seo_row(live):
    """It writes SEO the way it writes LIFETIME. Read as a monthly it would
    hunt for a digital row that does not exist and call every SEO client
    missing from the board."""
    from app.audit import parse_list
    got = {(r["client"], r["kind"]) for r in parse_list(
        "WHIT - Jefferson Hospital #54153 SEO\nWHIT - Mixed Client #54154")}
    assert got == {("Jefferson Hospital", "seo"), ("Mixed Client", "monthly")}


def test_reports_from_before_the_split_still_find_their_row(live):
    """`is_seo` is new, so every report already in the database is stamped
    False - including the SEO ones uploaded this cycle. Without a fallback they
    would all come off the board as never delivered, on a deploy that was
    supposed to be about tidying."""
    import datetime as dt
    _c, db, db_mod, _t = live
    D = dt.date.fromisoformat
    db.add(db_mod.OrderLine(
        market="Whitley Media", client="Jefferson Hospital", account_ids="54153",
        product="Search Engine Optimization",
        starts_on=D("2026-01-01"), ends_on=D("2026-12-31")))
    b = db_mod.Batch(market="Whitley Media", period="2026-08")
    db.add(b); db.flush()
    db.add(db_mod.Report(batch_id=b.id, client="Jefferson Hospital",
                         account_ids="54153", market="Whitley Media",
                         period="2026-08", filename="jh.pdf", findings=[],
                         acked=[], is_seo=False))
    db.commit()
    from app import board
    rows = {(e.client, e.kind): e for e in board.expected_for(db, "2026-08")}
    assert rows[("Jefferson Hospital", "seo")].report is not None


def test_a_much_smaller_export_than_last_time_says_so(live):
    """The import replaces the order list outright, so a narrower export takes
    clients off the board and nothing anywhere says a number went down."""
    from app.orders_s3 import _sync
    # The warning text is what matters, and it is built from two counts.
    import inspect
    src = inspect.getsource(_sync)
    assert "WORTH A LOOK" in src
    assert "0.75" in src


def test_the_order_gets_its_own_reason_not_the_clients(live):
    """The client map holds the FIRST reason recorded for that client, and a
    client with two orders has two.

    The Logan at Deer Valley was told "every line on it is an RFP" about order
    51554, whose two lines are IO Complete and ended on 15 May. The RFP was a
    different order of theirs. A reason about the client, printed as a reason
    about the order, is worse than none: it is wrong and it reads certain.
    """
    _c, db, db_mod, _t = live
    db.add(db_mod.OrderSync(
        source="s3://b/orders/", ok=True, state="done",
        dropped={"MCM|The Logan at Deer Valley": "is an RFP, not a live order"},
        dropped_orders={"51999": "is an RFP, not a live order",
                        "51554": "ended 2026-05-15, before August 2026"}))
    db.commit()
    from app.audit import _why
    row = {"client": "The Logan at Deer Valley", "kind": "monthly"}
    assert "ended 2026-05-15" in _why(db, "2026-08", {**row, "ids": ["51554"]}, [])
    assert "RFP" in _why(db, "2026-08", {**row, "ids": ["51999"]}, [])


def test_the_client_reason_is_still_there_for_a_row_with_no_id(live):
    """Half the tracker's rows carry no order id at all."""
    _c, db, db_mod, _t = live
    db.add(db_mod.OrderSync(
        source="s3://b/orders/", ok=True, state="done",
        dropped={"MCM|Nameless Co": "ended 2026-05-15, before August 2026"},
        dropped_orders={}))
    db.commit()
    from app.audit import _why
    why = _why(db, "2026-08", {"client": "Nameless Co", "ids": [],
                               "kind": "monthly"}, [])
    assert "ended 2026-05-15" in why


CANCEL_HEADER = ("client_business_unit,orders_status,client,orders_id,product,"
                 "id,status,orders_start_date,start_date,end_date,"
                 "orders_end_date\n")


def _cancel_world(db):
    from app import orders_io
    rows = [
        # One line cancelled under a LIVE order.
        "M,IO Live,Half Cancelled,60001,Social Mirror Ads,1,Cancelled,"
        "2026-01-01,2026-01-01,2026-12-31,2026-12-31\n",
        "M,IO Live,Half Cancelled,60001,Online Audio Ads,2,IO Live,"
        "2026-01-01,2026-01-01,2026-12-31,2026-12-31\n",
        # The WHOLE order cancelled, having run into August.
        "M,Cancelled,All Cancelled,60002,Social Mirror Ads,3,Cancelled,"
        "2026-01-01,2026-01-01,2026-08-20,2026-08-20\n",
    ]
    orders_io.import_io_export(db, (CANCEL_HEADER + "".join(rows)).encode(),
                               period="2026-08")


def test_a_cancelled_line_under_a_live_order_still_owes_a_monthly(live):
    """One product pulled off a campaign that is still running delivered its
    part of the month. The two flags were one flag, so a single cancelled line
    took the whole client's monthly off the board."""
    _c, db, _dbm, _t = live
    _cancel_world(db)
    from app import board
    rows = {(e.client, e.kind): e for e in board.expected_for(db, "2026-08")}
    assert ("Half Cancelled", "monthly") in rows
    # AND THE CANCELLED PRODUCT IS STILL ON IT. It ran for part of the month,
    # so the report covers it - it is not owed going forward, which is a
    # different statement.
    assert set(rows[("Half Cancelled", "monthly")].products) == {
        "Social Mirror", "Online Audio"}


def test_a_cancelled_order_owes_a_lifetime_and_not_a_monthly(live):
    """The whole campaign stopping is the case a lifetime is for."""
    _c, db, _dbm, _t = live
    _cancel_world(db)
    from app import board
    kinds = {(e.client, e.kind) for e in board.expected_for(db, "2026-08")}
    assert ("All Cancelled", "lifetime") in kinds
    assert ("All Cancelled", "monthly") not in kinds


def test_the_order_level_flag_is_actually_selected_by_the_board():
    """A column left out of the board's select reads False on every row and
    never fails - which is how every order pill was grey for a week."""
    import inspect
    from app import board
    src = inspect.getsource(board.expected_for)
    assert "OrderLine.order_canceled" in src


def test_the_rail_has_a_way_in():
    from pathlib import Path
    base = (Path(__file__).resolve().parents[1] / "app" / "templates"
            / "base.html").read_text()
    assert '/why-slow' in base
