"""Reports put on the board by hand, and what happens when the feed catches up.

Two rules, and the second only matters because of the first:

  * A row that is still waiting can have a PDF uploaded straight onto it.
  * A copy somebody signed off, or put there by hand, is not overwritten by the
    feed turning up later with its own version. Silently replacing a reviewed
    report means the sign-off now belongs to a file nobody has read.
"""
import datetime as dt
import importlib
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def app_db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTO_RECHECK", "false")
    from app import config as cfg
    importlib.reload(cfg)
    from app import db as dbm
    importlib.reload(dbm)
    from app import ingest as imod
    importlib.reload(imod)
    dbm.init_db()
    db = dbm.SessionLocal()
    D = dt.date.fromisoformat
    db.add(dbm.OrderLine(market="7 Mountains KY", client="Awaken Bakery",
                         account_ids="52746", product="Social Mirror Ads",
                         starts_on=D("2026-01-01"), ends_on=D("2026-12-31")))
    db.commit()
    yield db, dbm, imod
    db.close()
    # Put the modules back. Reloading rebinds the settings object, and every
    # module that did "from .config import settings" keeps the old one - so the
    # tests that run next fail in ways that have nothing to do with them.
    monkeypatch.undo()
    importlib.reload(cfg)
    importlib.reload(dbm)
    importlib.reload(imod)
    import app.main
    importlib.reload(app.main)


NAME = "July 2026_Awaken Bakery_52746.pdf"


def _feed(imod, db, blob, name=NAME):
    return imod.process_batch(db, [(name, blob)], source="zapier", notify=False,
                              coalesce=True, subject="FW: Daily report")


# ------------------------------------------------------------ the guard
def test_a_signed_off_report_is_not_overwritten_by_the_feed(app_db):
    db, dbm, imod = app_db
    first = (FIXTURES / "benton_rodeo.pdf").read_bytes()
    rep_id = _feed(imod, db, first).reports[0].id

    r = db.get(dbm.Report, rep_id)
    r.review_state, r.reviewed_by = "reviewed", "Jacob"
    r.reviewed_at = dt.datetime.utcnow()
    db.commit()

    fixed = (FIXTURES / "salem_rv.pdf").read_bytes()
    _feed(imod, db, fixed)

    r = db.get(dbm.Report, rep_id)
    assert db.query(dbm.Report).count() == 1, "it made a second row"
    assert r.review_state == "reviewed", "the sign-off was thrown away"
    assert Path(r.stored_path).read_bytes() == first, "it overwrote the file"
    assert r.has_pending and r.pending_name == NAME
    assert Path(r.pending_path).read_bytes() == fixed


def test_a_hand_uploaded_report_is_not_overwritten_either(app_db):
    """Nobody signed it off, but somebody chose to put it there."""
    db, dbm, imod = app_db
    first = (FIXTURES / "benton_rodeo.pdf").read_bytes()
    rep_id = _feed(imod, db, first).reports[0].id
    r = db.get(dbm.Report, rep_id)
    r.source = "manual"
    db.commit()

    _feed(imod, db, (FIXTURES / "salem_rv.pdf").read_bytes())
    r = db.get(dbm.Report, rep_id)
    assert r.has_pending and Path(r.stored_path).read_bytes() == first


def test_an_ordinary_report_is_still_superseded_in_place(app_db):
    """The guard is for deliberate work, not for everything."""
    db, dbm, imod = app_db
    rep_id = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0].id
    fixed = (FIXTURES / "salem_rv.pdf").read_bytes()
    _feed(imod, db, fixed)
    r = db.get(dbm.Report, rep_id)
    assert not r.has_pending
    assert Path(r.stored_path).read_bytes() == fixed


def test_only_the_newest_waiting_file_is_kept(app_db):
    """The queue is one deep. Three arrivals do not need three decisions."""
    db, dbm, imod = app_db
    rep_id = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0].id
    r = db.get(dbm.Report, rep_id)
    r.review_state = "reviewed"
    db.commit()

    _feed(imod, db, (FIXTURES / "salem_rv.pdf").read_bytes())
    first_wait = db.get(dbm.Report, rep_id).pending_path
    _feed(imod, db, (FIXTURES / "centre_hills.pdf").read_bytes())
    r = db.get(dbm.Report, rep_id)
    assert Path(r.pending_path).read_bytes() == (FIXTURES / "centre_hills.pdf").read_bytes()
    assert db.query(dbm.Report).count() == 1


def test_protected_says_which_kind_of_deliberate_it_was(app_db):
    db, dbm, _ = app_db
    r = dbm.Report(batch_id=1, filename="x.pdf", period="2026-07", client="c", review_state="new")
    assert r.protected == ""
    r.source = "manual"
    assert r.protected == "uploaded by hand"
    r.source = ""
    r.review_state, r.reviewed_by = "reviewed", "Jacob"
    assert "Jacob" in r.protected
    r.review_state = "waived"
    assert r.protected


# ---------------------------------------------------- accepting / discarding
@pytest.fixture()
def client(app_db):
    from fastapi.testclient import TestClient
    from app import main as mmod
    importlib.reload(mmod)
    return TestClient(mmod.app), app_db


def test_accepting_the_new_file_swaps_it_in_and_resets_the_sign_off(client):
    c, (db, dbm, imod) = client
    first = (FIXTURES / "benton_rodeo.pdf").read_bytes()
    rep_id = _feed(imod, db, first).reports[0].id
    r = db.get(dbm.Report, rep_id)
    r.review_state, r.reviewed_by = "reviewed", "Jacob"
    db.commit()
    fixed = (FIXTURES / "salem_rv.pdf").read_bytes()
    _feed(imod, db, fixed)

    assert c.post(f"/report/{rep_id}/pending/accept",
                  follow_redirects=False).status_code == 303
    db.expire_all()
    r = db.get(dbm.Report, rep_id)
    assert Path(r.stored_path).read_bytes() == fixed
    assert not r.has_pending
    assert r.review_state == "new", "the sign-off described the old file"
    assert r.source == "", "it is the feed's copy now"


def test_keeping_this_one_throws_the_new_file_away(client):
    c, (db, dbm, imod) = client
    first = (FIXTURES / "benton_rodeo.pdf").read_bytes()
    rep_id = _feed(imod, db, first).reports[0].id
    r = db.get(dbm.Report, rep_id)
    r.review_state = "reviewed"
    db.commit()
    _feed(imod, db, (FIXTURES / "salem_rv.pdf").read_bytes())
    db.expire_all()
    waiting = Path(db.get(dbm.Report, rep_id).pending_path)

    c.post(f"/report/{rep_id}/pending/discard", follow_redirects=False)
    db.expire_all()
    r = db.get(dbm.Report, rep_id)
    assert not r.has_pending
    assert not waiting.exists(), "the discarded file was left on disk"
    assert r.review_state == "reviewed", "the sign-off should survive"
    assert Path(r.stored_path).read_bytes() == first


def test_the_waiting_file_can_be_opened_before_deciding(client):
    c, (db, dbm, imod) = client
    rep_id = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0].id
    r = db.get(dbm.Report, rep_id)
    r.review_state = "reviewed"
    db.commit()
    fixed = (FIXTURES / "salem_rv.pdf").read_bytes()
    _feed(imod, db, fixed)
    got = c.get(f"/report/{rep_id}/pending/file")
    assert got.status_code == 200 and got.content == fixed


def test_uploading_by_hand_creates_the_report_for_a_waiting_row(client):
    c, (db, dbm, imod) = client
    blob = (FIXTURES / "benton_rodeo.pdf").read_bytes()
    r = c.post("/cycle/upload",
               data={"period": "2026-07", "market": "7 Mountains KY",
                     "client": "Awaken Bakery", "account_ids": "52746",
                     "kind": "monthly"},
               files={"file": ("anything at all.pdf", blob, "application/pdf")},
               follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    rep = db.query(dbm.Report).one()
    assert rep.source == "manual"
    assert rep.client == "Awaken Bakery" and rep.account_ids == "52746"
    assert rep.market == "7 Mountains KY" and rep.period == "2026-07"
    assert Path(rep.stored_path).read_bytes() == blob
    assert rep.checks, "the checks did not run on it"


def test_uploading_by_hand_where_one_exists_goes_to_the_replace_flow(client):
    """Two rows for one client is the thing to avoid; the replace route knows
    how to handle a second copy and this one does not."""
    c, (db, dbm, imod) = client
    rep_id = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0].id
    r = c.post("/cycle/upload",
               data={"period": "2026-07", "market": "7 Mountains KY",
                     "client": "Awaken Bakery", "account_ids": "52746",
                     "kind": "monthly"},
               files={"file": ("x.pdf", (FIXTURES / "salem_rv.pdf").read_bytes(),
                               "application/pdf")},
               follow_redirects=False)
    assert r.headers["location"] == f"/report/{rep_id}/view"
    db.expire_all()
    assert db.query(dbm.Report).count() == 1


def test_something_that_is_not_a_pdf_is_refused(client):
    c, (db, dbm, imod) = client
    r = c.post("/cycle/upload",
               data={"period": "2026-07", "market": "7 Mountains KY",
                     "client": "Awaken Bakery", "account_ids": "52746",
                     "kind": "monthly"},
               files={"file": ("notes.txt", b"not a pdf at all", "text/plain")})
    assert r.status_code == 400


# ------------------------------------------------------ the pages still render
def test_the_cycle_board_and_a_report_page_render_after_a_pulled_signoff(client):
    """A pulled sign-off touches both templates. A NameError in one of them is
    a 500 on the page somebody works from all day."""
    import datetime as _dt
    c, (db, dbm, imod) = client
    rep_id = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0].id
    r = db.get(dbm.Report, rep_id)
    r.review_state, r.reviewed_by = "new", "k"
    r.signoff_cleared_at = _dt.datetime(2026, 8, 25)
    db.commit()

    page = c.get(f"/report/{rep_id}/view")
    assert page.status_code == 200
    assert "k signed this off" in page.text

    board = c.get("/cycle")
    assert board.status_code == 200
    assert "needs signing off again" in board.text
    # And the name is not sitting in the reviewer column as though it stood.
    assert ">k</span>" not in board.text


# ------------------------------------------------------ the links, on their own page
def test_the_board_points_at_the_links_page_rather_than_listing_them(client):
    """A list of finished partners growing above 146 cards pushes the work down
    the screen. The moment you want a link is when you are sending it."""
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    db.add(dbm.Delivery(period=rep.period, group=rep.market, target="drive",
                        reports=1, ok=True,
                        share_url="https://drive.google.com/drive/folders/abc123"))
    db.commit()

    board = c.get(f"/cycle?period={rep.period}").text
    assert "Partners delivered" in board
    assert f"/cycle/links?period={rep.period}" in board
    # The stacked panel is gone. The partner's own card keeps its link, which
    # sits in context rather than pushing the board down the screen.
    assert '<div class="links">' not in board
    assert "Client link" in board


def test_the_links_page_lists_them_with_copy(client):
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    db.add(dbm.Delivery(period=rep.period, group=rep.market, target="drive",
                        reports=1, ok=True,
                        share_url="https://drive.google.com/drive/folders/abc123"))
    db.commit()

    page = c.get(f"/cycle/links?period={rep.period}")
    assert page.status_code == 200
    assert 'data-copy="https://drive.google.com/drive/folders/abc123"' in page.text
    assert 'class="fresh"' not in page.text, "nothing was packaged on this visit"


def test_the_one_just_packaged_is_first_and_marked(client):
    """Packaging is the last step before sending the link, and landing back on
    a board with it somewhere in there is a find-it-yourself puzzle."""
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    for name in ("Aardvark Media", rep.market, "Zebra Radio"):
        db.add(dbm.Delivery(period=rep.period, group=name, target="drive",
                            reports=1, ok=True,
                            share_url=f"https://drive.google.com/{name}"))
    db.commit()

    from urllib.parse import quote
    page = c.get(f"/cycle/links?period={rep.period}&new={quote(rep.market)}").text
    body = page[page.find("<ul>"):]
    assert body.find(rep.market) < body.find("Aardvark Media"), "not first"
    assert 'class="fresh"' in body and "just packaged" in body
    # Alphabetical otherwise, so the rest of the page does not reshuffle.
    assert body.find("Aardvark Media") < body.find("Zebra Radio")


def test_an_empty_links_page_says_so_rather_than_looking_broken(client):
    c, (db, dbm, imod) = client
    page = c.get("/cycle/links?period=2026-07")
    assert page.status_code == 200
    assert "No partner has been packaged" in page.text


def test_see_reports_does_not_jump_past_the_card(client):
    """The anchor scrolled straight past the partner card that says what you
    are looking at, so the table arrived with its heading off screen."""
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    page = c.get(f"/cycle?period={rep.period}")
    assert "See reports" in page.text
    assert "#reports\">See reports" not in page.text


def test_the_reports_that_arrived_sort_above_the_ones_that_have_not(client):
    """Two thirds of a cycle has not been sent yet. In market order the rows
    there is something to do about sit below a screenful of "Not received" -
    and the 150-row cap can cut them off the page altogether."""
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    # A pile of orders with nothing against them, named to sort first.
    for i in range(5):
        db.add(dbm.OrderLine(market=rep.market, client=f"AAA Client {i}",
                             account_ids=f"9000{i}", product="Display",
                             starts_on=None, ends_on=None))
    db.commit()

    text = c.get(f"/cycle?period={rep.period}").text
    body = text.split('id="reports"', 1)[-1] if 'id="reports"' in text else text
    arrived = body.find("Awaken Bakery")
    first_missing = body.find("AAA Client")
    assert arrived != -1 and first_missing != -1
    assert arrived < first_missing, "the report you can open is below the ones you cannot"


# ------------------------------------------------------------- the light sign-in
def test_saving_a_name_puts_it_in_the_topbar_and_takes_the_boxes_away(client):
    """A name, not a login. Typing it into every row is what made people stop
    signing off at all."""
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]

    page = c.get(f"/cycle?period={rep.period}").text
    assert 'name="who" placeholder="Your name"' in page

    c.post("/me", data={"who": "Kiri"}, follow_redirects=False)
    page = c.get(f"/cycle?period={rep.period}").text
    assert ">Kiri</b>" in page and "not you?" in page
    assert 'placeholder="Your name" size="10"' not in page, "still asking on every row"

    view = c.get(f"/report/{rep.id}/view").text
    assert 'name="who" placeholder="Your name"' not in view


def test_signing_off_with_no_typed_name_uses_the_remembered_one(client):
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    c.post("/me", data={"who": "Kiri"})
    c.post(f"/report/{rep.id}/review", data={"state": "reviewed", "who": ""},
           follow_redirects=False)
    db.expire_all()
    assert db.get(dbm.Report, rep.id).reviewed_by == "Kiri"


def test_a_typed_name_beats_the_remembered_one(client):
    """Otherwise a signed-in person could never sign for whoever is sitting
    next to them."""
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    c.post("/me", data={"who": "Kiri"})
    c.post(f"/report/{rep.id}/review", data={"state": "reviewed", "who": "Paulina"})
    db.expire_all()
    assert db.get(dbm.Report, rep.id).reviewed_by == "Paulina"


def test_the_first_signoff_remembers_you(client):
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    c.post(f"/report/{rep.id}/review", data={"state": "reviewed", "who": "Jacob"})
    assert c.cookies.get("qa_user") == "Jacob"


def test_accepting_the_last_finding_is_a_review(client):
    """Going through every finding and ticking it off IS reading the report.
    Asking for a signature afterwards asks the same question twice."""
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    open_n = len(rep.open_findings)
    assert open_n, "fixture carries no findings to tick off"
    c.post("/me", data={"who": "Kiri"})

    for i, f in enumerate(rep.findings):
        if f.get("severity") in ("fail", "warn"):
            c.post(f"/report/{rep.id}/ack", data={"index": i, "on": "1"},
                   follow_redirects=False)
    db.expire_all()
    r = db.get(dbm.Report, rep.id)
    assert not r.open_findings
    assert r.review_state == "reviewed" and r.reviewed_by == "Kiri"


def test_it_does_not_sign_for_somebody_it_cannot_name(client):
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    for i, f in enumerate(rep.findings):
        if f.get("severity") in ("fail", "warn"):
            c.post(f"/report/{rep.id}/ack", data={"index": i, "on": "1"})
    db.expire_all()
    assert db.get(dbm.Report, rep.id).review_state == "new"


def test_un_ticking_does_not_tear_up_a_signoff(client):
    """A sign-off is a person's decision. Un-accepting is them working on it."""
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    c.post("/me", data={"who": "Kiri"})
    first = next(i for i, f in enumerate(rep.findings)
                 if f.get("severity") in ("fail", "warn"))
    c.post(f"/report/{rep.id}/review", data={"state": "reviewed", "who": "Kiri"})
    c.post(f"/report/{rep.id}/ack", data={"index": first, "on": ""})
    db.expire_all()
    assert db.get(dbm.Report, rep.id).review_state == "reviewed"


# ------------------------------------------------------------- saved views
def test_saving_a_view_names_the_filters_and_drops_the_period(client):
    """A view saved while looking at July should open on whatever cycle you are
    on, or it is wrong the moment the month turns."""
    c, (db, dbm, imod) = client
    c.post("/me", data={"who": "Kiri"})
    c.post("/views", data={"name": "PA with errors",
                           "query": "?period=2026-07&buyer=Lauren&status=Open&q=pa"},
           follow_redirects=False)
    row = db.query(dbm.SavedView).one()
    assert row.name == "PA with errors"
    assert "period" not in row.query
    assert "buyer=Lauren" in row.query and "status=Open" in row.query
    assert row.created_by == "Kiri"


def test_a_saved_view_appears_on_the_board_as_a_link(client):
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    c.post("/views", data={"name": "Mine", "query": "?buyer=Lauren"})
    page = c.get(f"/cycle?period={rep.period}").text
    assert ">Mine</a>" in page
    assert f"/cycle?period={rep.period}&buyer=Lauren" in page


def test_saving_the_same_name_twice_replaces_it(client):
    """Otherwise the list fills with three views all called "mine"."""
    c, (db, dbm, imod) = client
    c.post("/views", data={"name": "Mine", "query": "?buyer=Lauren"})
    c.post("/views", data={"name": "Mine", "query": "?buyer=Paulina"})
    row = db.query(dbm.SavedView).one()
    assert "Paulina" in row.query


def test_junk_in_the_query_is_not_saved(client):
    """The query comes from the browser, so it is not trusted to be a filter."""
    c, (db, dbm, imod) = client
    c.post("/views", data={"name": "Mine",
                           "query": "?buyer=Lauren&rows=all&evil=1"})
    row = db.query(dbm.SavedView).one()
    assert "evil" not in row.query and "rows" not in row.query


def test_a_view_can_be_forgotten(client):
    c, (db, dbm, imod) = client
    c.post("/views", data={"name": "Mine", "query": "?buyer=Lauren"})
    vid = db.query(dbm.SavedView).one().id
    c.post(f"/views/{vid}/delete", follow_redirects=False)
    assert db.query(dbm.SavedView).count() == 0


def test_an_unnamed_view_is_not_saved(client):
    c, (db, dbm, imod) = client
    c.post("/views", data={"name": "  ", "query": "?buyer=Lauren"})
    assert db.query(dbm.SavedView).count() == 0


# ------------------------------------------------------------- bulk sign-off
def _second(imod, db, name="July 2026_Salem RV_52747.pdf"):
    return _feed(imod, db, (FIXTURES / "salem_rv.pdf").read_bytes(), name).reports[0]


def test_several_reports_can_be_signed_off_at_once(client):
    """Most of a cycle is reports where everything passed, and ticking those
    one at a time is the longest single job on the board."""
    c, (db, dbm, imod) = client
    a = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    b = _second(imod, db)
    c.post("/me", data={"who": "Kiri"})

    r = c.post("/reports/review", data={"ids": [a.id, b.id], "state": "reviewed"},
               follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    for rid in (a.id, b.id):
        rep = db.get(dbm.Report, rid)
        assert rep.review_state == "reviewed" and rep.reviewed_by == "Kiri"
        assert rep.reviewed_at is not None


def test_it_will_not_sign_for_somebody_it_cannot_name(client):
    c, (db, dbm, imod) = client
    a = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    c.post("/reports/review", data={"ids": [a.id], "state": "reviewed"})
    db.expire_all()
    assert db.get(dbm.Report, a.id).review_state == "new"


def test_a_typed_name_is_remembered_from_the_bulk_bar_too(client):
    c, (db, dbm, imod) = client
    a = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    c.post("/reports/review", data={"ids": [a.id], "state": "reviewed",
                                    "who": "Paulina"})
    assert c.cookies.get("qa_user") == "Paulina"
    db.expire_all()
    assert db.get(dbm.Report, a.id).reviewed_by == "Paulina"


def test_an_unknown_state_is_refused(client):
    c, (db, dbm, imod) = client
    a = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    c.post("/me", data={"who": "Kiri"})
    r = c.post("/reports/review", data={"ids": [a.id], "state": "shipped"})
    assert r.status_code == 400


def test_an_empty_selection_changes_nothing(client):
    c, (db, dbm, imod) = client
    a = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    c.post("/me", data={"who": "Kiri"})
    c.post("/reports/review", data={"state": "reviewed"}, follow_redirects=False)
    db.expire_all()
    assert db.get(dbm.Report, a.id).review_state == "new"


def test_the_rows_carry_whether_they_are_clear(client):
    """"Select the ones that passed" is driven off this, not off the status
    pill - a report can be amber for a warning somebody has already accepted."""
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    page = c.get(f"/cycle?period={rep.period}").text
    assert 'class="rowpick"' in page
    assert 'data-clear="' in page
    assert "Select the ones that passed" in page


# ------------------------------------------------------- the generic logo
def test_the_header_logo_is_fingerprinted_and_stored(client):
    """A partner's logo is on that partner's reports, a client's on that
    client's, and the reporting tool's default is on everybody's - which is
    what makes the last one findable without a list of 146 partner logos."""
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    db.expire_all()
    assert db.get(dbm.Report, rep.id).logo_hash


def test_the_same_report_twice_gives_the_same_fingerprint():
    from app.checks.logo import header_logo_hash
    a = header_logo_hash(FIXTURES / "benton_rodeo.pdf")
    assert a and a == header_logo_hash(FIXTURES / "benton_rodeo.pdf")


def test_two_partners_have_two_different_logos():
    from app.checks.logo import header_logo_hash
    assert (header_logo_hash(FIXTURES / "benton_rodeo.pdf")
            != header_logo_hash(FIXTURES / "central_penn.pdf"))


def test_an_unreadable_file_is_silent_rather_than_broken(tmp_path):
    from app.checks.logo import header_logo_hash
    junk = tmp_path / "not.pdf"
    junk.write_bytes(b"this is not a pdf")
    assert header_logo_hash(junk) == ""
    assert header_logo_hash(tmp_path / "missing.pdf") == ""


def test_the_check_fails_only_a_logo_somebody_has_marked():
    """It does not guess. Guessing was tried - a logo on three or more markets
    could not be any one partner's, so it must be the tool's - and Seven
    Mountains disproved it in a day, running 7 Mountains PA, PA Altoona and KY
    as separate markets with one perfectly correct logo across all of them."""
    from app.checks.rules import check_market_logo
    assert check_market_logo({"logo_hash": "abc", "logo_generic": False}) == []
    out = check_market_logo({"logo_hash": "abc", "logo_generic": True})
    assert len(out) == 1 and out[0]["severity"] == "fail"
    assert out[0]["where"] == "p1"
    assert out[0]["detail"] == "", "the title says it; the paragraph did not"


def test_the_check_abstains_until_it_has_been_told_anything():
    """Two ways to have nothing to say: the corner could not be read, or nobody
    has marked the tool's default yet so there is nothing to compare against."""
    from app.checks.rules import _rule_applies, check_market_logo
    assert _rule_applies(check_market_logo, {"logo_hash": "", "logo_known": True}) is False
    assert _rule_applies(check_market_logo, {"logo_hash": "a", "logo_known": False}) is False
    assert _rule_applies(check_market_logo, {"logo_hash": "a", "logo_known": True}) is True


def test_marking_a_logo_makes_every_report_carrying_it_fail(client):
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    db.expire_all()
    logo = db.get(dbm.Report, rep.id).logo_hash
    assert logo

    c.post("/me", data={"who": "Kiri"})
    assert c.post(f"/logo/{logo}/mark", data={"kind": "generic"},
                  follow_redirects=False).status_code == 303

    from app.checks.logo import is_generic
    assert is_generic(db, logo) is True
    # And the reports carrying it are queued for a re-check, since the answer
    # they are showing was worked out before anybody had said this.
    db.expire_all()
    assert db.get(dbm.Report, rep.id).rules_version == ""


def test_a_marking_can_be_taken_back(client):
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    db.expire_all()
    logo = db.get(dbm.Report, rep.id).logo_hash
    c.post(f"/logo/{logo}/mark", data={"kind": "generic"})
    c.post(f"/logo/{logo}/mark", data={"kind": "clear"})
    from app.checks.logo import is_generic
    assert is_generic(db, logo) is False


def test_the_report_page_shows_the_actual_crop(client):
    """Marking a logo is a decision about a picture, so the page shows the
    picture rather than asking anybody to take a fingerprint on trust."""
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    page = c.get(f"/report/{rep.id}/view").text
    assert f"/report/{rep.id}/logo.png" in page
    assert "This is the tool's default logo" in page

    png = c.get(f"/report/{rep.id}/logo.png")
    assert png.status_code == 200
    assert png.content[:8] == b"\x89PNG\r\n\x1a\n"


def test_a_junk_fingerprint_is_refused(client):
    c, (db, dbm, imod) = client
    assert c.post("/logo/..%2Fetc/mark", data={"kind": "generic"}).status_code in (400, 404)
    assert c.post("/logo/NOTAHASH/mark", data={"kind": "generic"}).status_code == 400


def test_the_sync_button_comes_back_where_it_was_pressed(client):
    c, (db, dbm, imod) = client
    r = c.post("/orders/sync", data={"back": "/cycle?period=2026-07"},
               follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].startswith("/cycle?period=2026-07&sync=")


def test_the_sync_button_cannot_be_pointed_off_this_app(client):
    """It takes a path from a form, so it is not trusted to be one."""
    c, (db, dbm, imod) = client
    for evil in ("https://example.com/", "//example.com/", "javascript:alert(1)"):
        r = c.post("/orders/sync", data={"back": evil}, follow_redirects=False)
        assert r.headers["location"].startswith("/orders?sync=")


# ------------------------------------------- marking a logo reaches the rest
def test_marking_a_logo_re_checks_every_report_that_carries_it(client, monkeypatch):
    """Clearing the stamp only queued them, and the sweep skips signed-off
    reports and stops running once its queue drains - so marking a logo could
    sit there doing nothing visible until the next deploy."""
    c, (db, dbm, imod) = client
    from app import main as mmod

    started = {}
    monkeypatch.setattr("app.recheck.start_job",
                        lambda db, key, **kw: started.update(kw, key=key) or {})
    rep = dbm.Report(batch_id=1, filename="x.pdf", period="2026-07", client="Awaken Bakery",
                     market="7 Mountains KY", review_state="reviewed",
                     reviewed_by="k", rules_version="abc",
                     logo_hash="deadbeef")
    other = dbm.Report(batch_id=1, filename="x.pdf", period="2026-07", client="Other Client",
                       market="7 Mountains PA", review_state="new",
                       rules_version="abc", logo_hash="deadbeef")
    db.add_all([rep, other]); db.commit()

    r = c.post("/logo/deadbeef/mark", data={"kind": "generic"},
               follow_redirects=False)
    assert r.status_code == 303
    assert "logo_queued=2" in r.headers["location"]
    db.expire_all()
    assert db.get(dbm.Report, rep.id).rules_version == ""
    assert db.get(dbm.Report, other.id).rules_version == ""
    # Signed off ones included: a newly marked generic logo is exactly the case
    # where a sign-off deserves another look.
    assert started["logo"] == "deadbeef" and not started.get("skip_signed")


def test_the_report_names_who_else_carries_the_same_logo(client):
    c, (db, dbm, imod) = client
    rep = dbm.Report(batch_id=1, filename="x.pdf", period="2026-07", client="Awaken Bakery",
                     market="7 Mountains KY", review_state="new",
                     logo_hash="deadbeef")
    other = dbm.Report(batch_id=1, filename="x.pdf", period="2026-07", client="Second Client",
                       market="7 Mountains PA", review_state="reviewed",
                       logo_hash="deadbeef")
    db.add_all([rep, other]); db.commit()
    html = c.get(f"/report/{rep.id}/view").text
    assert "Second Client" in html
    assert "carry this same logo" in html or "carries this same logo" in html


def test_order_lines_come_back_as_a_fragment_for_the_sheet(client):
    c, (db, dbm, imod) = client
    rep = dbm.Report(batch_id=1, filename="x.pdf", period="2026-07", client="Awaken Bakery",
                     market="7 Mountains KY", review_state="new",
                     account_ids="52746")
    db.add(rep); db.commit()
    full = c.get(f"/report/{rep.id}/orders").text
    frag = c.get(f"/report/{rep.id}/orders?frag=1").text
    assert "<html" in full.lower()
    assert "<html" not in frag.lower()
    assert "Social Mirror" in frag


def test_a_line_item_that_missed_the_month_sits_below_the_fold(client):
    """The orders are read for the month being worked on, so a July report gets
    looked at against an August import - and a buy that does not start until
    August was sitting on its order list looking like evidence."""
    from tests.test_order_remap import _HEAD, TWO_FLIGHTS
    from app.orders_io import import_io_export
    c, (db, dbm, imod) = client
    import_io_export(db, (_HEAD + TWO_FLIGHTS).encode(), period="2026-08")
    rep = dbm.Report(batch_id=1, filename="a.pdf", period="2026-07",
                     client="Acme", market="BU", review_state="new",
                     account_ids="101, 102")
    db.add(rep); db.commit()

    html = c.get(f"/report/{rep.id}/orders?frag=1").text
    head, _, tail = html.partition("did not run in 2026-07")
    assert tail, "the off-month section is missing"
    assert "7001" in head and "7002" not in head, "July's line item, and only it"
    assert "7002" in tail


# --------------------------------------------------------------- delivery
def test_a_delivered_report_keeps_its_own_name(app_db):
    """The partner's copy used to be named after the client - no month, no
    order id - so what they opened was not what the report page said it was
    called, and nothing downstream could be filed by it."""
    from app.delivery import report_filename
    from app import db as dbm

    rep = dbm.Report(batch_id=1, filename="July 2026_Elmira Downtown Development 53700.pdf",
                     period="2026-07", client="Elmira Downtown Development",
                     account_ids="53700")
    e = type("E", (), {"report": rep, "client": rep.client, "kind": "monthly"})()
    assert report_filename(e) == "July 2026_Elmira Downtown Development 53700.pdf"

    rep.filename = "July 2026_Elmira Downtown Development 53700 (1).pdf"
    assert report_filename(e) == "July 2026_Elmira Downtown Development 53700.pdf"

    life = type("E", (), {"report": rep, "client": rep.client, "kind": "lifetime"})()
    assert report_filename(life).endswith(" - Lifetime.pdf")

    rep.filename = ""
    assert report_filename(e) == "July 2026_Elmira Downtown Development 53700.pdf"


def test_a_blank_delivery_target_is_filled_in_from_the_bundled_roster(app_db):
    """A roster exported without the Delivery column loads every other field
    and leaves this blank - and blank means Drive, which is how a Dropbox
    partner's client was handed a Google Drive link."""
    from app import db as dbm
    from app.partners import backfill_targets

    db, _, _ = app_db
    db.add(dbm.Partner(partner="7 Mountains NY Elmira/Mansfield",
                       group="7 Mountains NY Elmira/Mansfield", delivery_target=""))
    db.add(dbm.Partner(partner="Someone Else", group="Someone Else",
                       delivery_target="drive"))
    db.commit()
    assert backfill_targets(db) == 1
    rows = {p.partner: p.delivery_target for p in db.query(dbm.Partner).all()}
    assert rows["7 Mountains NY Elmira/Mansfield"] == "dropbox"
    assert rows["Someone Else"] == "drive"       # an answer already there stands


def test_a_group_takes_the_exception_not_the_first_answer(app_db):
    """Markets ship as a group. The first partner in it used to decide where
    the group delivers, so a group whose first market said nothing (Drive)
    shipped to Drive even with a Dropbox market sitting beside it."""
    from app import db as dbm
    from app.board import by_group

    db, _, _ = app_db
    db.add(dbm.Partner(partner="7 Mountains NY Elmira", group="7 Mountains NY",
                       delivery_target="drive"))
    db.add(dbm.Partner(partner="7 Mountains NY Mansfield", group="7 Mountains NY",
                       delivery_target="dropbox"))
    db.commit()
    from app.board import Expected
    rows = by_group(db, "2026-07", expected=[
        Expected(group="7 Mountains NY", market="7 Mountains NY Elmira",
                 client="c", account_ids="1", products=["Video"], kind="monthly",
                 buyer="", report=None)])
    assert rows[0].target == "dropbox"


# ------------------------------------------------- what a report is called
def test_a_hand_pulled_report_is_named_from_what_it_is(app_db):
    """TapClicks calls every file you download by hand "Digital Marketing
    Report.pdf". That name followed the report onto the board, into the zip and
    into the partner's folder, where nothing can be filed by it."""
    import datetime as dt
    from app import db as dbm
    from app.ingest import process_batch

    db, dbm_, imod = app_db
    D = dt.date.fromisoformat
    db.add(dbm_.OrderLine(market="7 Mountains PA Selinsgrove", client="Benton Rodeo",
                          account_ids="52746", line_ids="1", product="Mobile Conquesting",
                          campaign="Mobile Conquesting Display & Video Ads",
                          starts_on=D("2025-01-01"), ends_on=D("2026-12-31"), live=True))
    db.commit()
    blob = (FIXTURES / "benton_rodeo.pdf").read_bytes()
    batch = process_batch(db, [("Digital Marketing Report.pdf", blob)],
                          source="manual", notify=False)
    rep = batch.reports[0]
    assert rep.filename == "July 2026_Benton Rodeo 52746.pdf"
    # The client comes off page one, not off the useless filename - and the
    # market and order id come off the order line it matches.
    assert rep.client == "Benton Rodeo"
    assert rep.account_ids == "52746"
    assert rep.market == "7 Mountains PA Selinsgrove"


def test_a_lifetime_is_recognized_by_the_range_it_prints():
    """A lifetime pulled with no month in its name was read as a monthly and
    checked against one month of a two-year campaign."""
    import datetime as dt
    from app.checks.rules import looks_like_lifetime
    D = dt.date.fromisoformat
    assert looks_like_lifetime((D("2026-07-01"), D("2026-07-31"))) is False
    assert looks_like_lifetime((D("2026-06-28"), D("2026-08-02"))) is False
    assert looks_like_lifetime((D("2025-01-03"), D("2026-07-31"))) is True
    assert looks_like_lifetime(None) is False


def test_the_name_is_built_from_the_report_not_inherited():
    from app.naming import canonical_name

    class R:
        id = 1
        client = "All Seasons Powersports"
        account_ids = "53908"
        period = "2026-07"
        is_lifetime = False
        filename = "Digital Marketing Report.pdf"

    r = R()
    assert canonical_name(r) == "July 2026_All Seasons Powersports 53908.pdf"
    r.is_lifetime = True
    assert canonical_name(r) == "Lifetime_All Seasons Powersports 53908.pdf"
    r.account_ids = "53908, 53909"
    assert canonical_name(r) == "Lifetime_All Seasons Powersports 53908 53909.pdf"
    # Nothing known yet: keep its own name, minus a browser's "(1)".
    r.client, r.period, r.is_lifetime = "", "", False
    r.filename = "download (1).pdf"
    assert canonical_name(r) == "download.pdf"


def test_a_report_with_no_market_gets_one_on_the_next_check(app_db):
    """"no market" means the report belongs to no partner, sits under no card
    and is in nobody's cycle. The stamp used to happen only at ingest, so
    anything that arrived before its client was on the order list stayed that
    way for good."""
    import datetime as dt
    from pathlib import Path
    from app import db as dbm_
    from app.recheck import recheck

    db, dbm2, _ = app_db
    D = dt.date.fromisoformat
    db.add(dbm2.OrderLine(market="7 Mountains PA Selinsgrove", client="Benton Rodeo",
                          account_ids="52999", line_ids="1", product="Mobile Conquesting",
                          campaign="Mobile Conquesting Display & Video Ads",
                          starts_on=D("2025-01-01"), ends_on=D("2026-12-31"), live=True))
    src = FIXTURES / "benton_rodeo.pdf"
    rep = dbm2.Report(batch_id=1, filename="July 2026_Benton Rodeo 52999.pdf",
                      stored_path=str(src), client="Benton Rodeo",
                      account_ids="52999", market="", period="2026-07")
    db.add(rep); db.commit()
    recheck(db, rep)
    assert rep.market == "7 Mountains PA Selinsgrove"


def test_setting_a_whole_group_of_partners_at_once(client):
    """Setting a group of markets one dropdown at a time is how one of them
    gets missed, and the one that gets missed is the one whose client is handed
    the wrong link."""
    c, (db, dbm, imod) = client
    db.query(dbm.Partner).delete()
    for name in ("7 Mountains PA", "7 Mountains PA Stroudsburg",
                 "7 Mountains KY", "Somebody Else"):
        db.add(dbm.Partner(partner=name, group=name, delivery_target="drive"))
    db.commit()

    r = c.post("/partners/target-bulk",
               data={"contains": "7 mountains", "target": "dropbox"},
               follow_redirects=False)
    assert r.status_code == 303 and "set=3" in r.headers["location"]
    db.expire_all()
    rows = {p.partner: p.delivery_target for p in db.query(dbm.Partner).all()}
    assert rows["7 Mountains PA Stroudsburg"] == "dropbox"
    assert rows["7 Mountains KY"] == "dropbox"
    assert rows["Somebody Else"] == "drive"


def test_uploading_a_lifetime_by_hand(client):
    """A lifetime scopes its flight to the cycle's lifetime window, and the
    function that works that out was not imported in this route - so every
    hand-uploaded lifetime came back as a 500."""
    c, (db, dbm, imod) = client
    blob = (FIXTURES / "benton_rodeo.pdf").read_bytes()
    for kind in ("lifetime", "monthly"):
        r = c.post("/cycle/upload",
                   data={"period": "2026-07", "market": "7 Mountains KY",
                         "client": "Awaken Bakery", "account_ids": "52746",
                         "kind": kind},
                   files={"file": (f"{kind}.pdf", blob, "application/pdf")},
                   follow_redirects=False)
        assert r.status_code == 303, f"{kind}: {r.status_code}"


def test_every_route_that_scopes_a_flight_imports_what_it_needs():
    """Three routes work out a lifetime's window and each imports its own
    helpers. One of them did not, and only a lifetime upload reached that line."""
    import ast
    from pathlib import Path as _P
    src = _P("app/main.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        body = ast.get_source_segment(src, node) or ""
        if "cycle_for(" not in body:
            continue
        assert "import" in body and "cycle_for" in body.split("cycle_for(")[0], \
            f"{node.name} calls cycle_for without importing it"


def test_an_auto_review_lands_back_on_the_board(client):
    """Ticking the last finding IS the review, so it goes where pressing
    Reviewed goes - the board you came from, not the report you just signed."""
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    c.post("/me", data={"who": "Kiri"})
    # Arriving from the board is what teaches the page the way back.
    c.get(f"/report/{rep.id}/view",
          headers={"referer": "http://x/cycle?period=2026-07&group=Benton"})

    bad = [i for i, f in enumerate(rep.findings)
           if f.get("severity") in ("fail", "warn")]
    assert len(bad) >= 1
    last = None
    for i in bad:
        last = c.post(f"/report/{rep.id}/ack", data={"index": i, "on": "1"},
                      headers={"referer": f"http://x/report/{rep.id}/view"},
                      follow_redirects=False)
    assert last.headers["location"].startswith("/cycle?period=2026-07")


def test_accepting_one_of_several_stays_on_the_report(client):
    """Only the LAST one is a review. Ticking one of four is still working."""
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    c.post("/me", data={"who": "Kiri"})
    c.get(f"/report/{rep.id}/view", headers={"referer": "http://x/cycle?period=2026-07"})
    bad = [i for i, f in enumerate(rep.findings)
           if f.get("severity") in ("fail", "warn")]
    if len(bad) < 2:
        return                     # nothing to prove on a one-finding fixture
    r = c.post(f"/report/{rep.id}/ack", data={"index": bad[0], "on": "1"},
               headers={"referer": f"http://x/report/{rep.id}/view"},
               follow_redirects=False)
    assert f"/report/{rep.id}/view" in r.headers["location"]


def test_a_hand_uploaded_report_is_named_the_way_the_feed_names_them(client):
    """TapClicks calls every file you download by hand "Digital Marketing
    Report.pdf". Two of those reached a partner's Dropbox folder under that
    name, because renaming only ever happened on the feed and on a
    replacement."""
    c, (db, dbm, imod) = client
    pdf = (FIXTURES / "benton_rodeo.pdf").read_bytes()
    r = c.post("/cycle/upload", files={"file": ("Digital Marketing Report.pdf", pdf, "application/pdf")},
               data={"period": "2026-07", "market": "7 Mountains PA Selinsgrove",
                     "client": "All Seasons Powersports", "account_ids": "47329",
                     "kind": "monthly"}, follow_redirects=False)
    rid = int(r.headers["location"].split("/")[2])
    db.expire_all()
    rep = db.get(dbm.Report, rid)
    assert rep.filename == "July 2026_All Seasons Powersports 47329.pdf"
    assert rep.renamed_from == "Digital Marketing Report.pdf"


def test_a_hand_uploaded_lifetime_is_named_as_one(client):
    c, (db, dbm, imod) = client
    pdf = (FIXTURES / "benton_rodeo.pdf").read_bytes()
    r = c.post("/cycle/upload", files={"file": ("Digital Marketing Report.pdf", pdf, "application/pdf")},
               data={"period": "2026-07", "market": "7 Mountains PA Selinsgrove",
                     "client": "All Seasons Powersports", "account_ids": "47329",
                     "kind": "lifetime"}, follow_redirects=False)
    rid = int(r.headers["location"].split("/")[2])
    db.expire_all()
    assert db.get(dbm.Report, rid).filename \
        == "Lifetime_All Seasons Powersports 47329.pdf"


def test_the_delivered_name_is_built_whatever_the_report_is_called():
    """The last mile says so too, so one missed path cannot put an unfilable
    name in front of a partner again."""
    from app.delivery import report_filename

    class R:
        filename = "Digital Marketing Report.pdf"
        client = "All Seasons Powersports"
        account_ids = "47329"
        period = "2026-07"
        is_lifetime = False

    class E:
        report = R()
        client = "All Seasons Powersports"
        kind = "monthly"

    assert report_filename(E()) == "July 2026_All Seasons Powersports 47329.pdf"
    R.is_lifetime, E.kind = True, "lifetime"
    assert report_filename(E()) == "Lifetime_All Seasons Powersports 47329.pdf"


def test_a_re_check_puts_an_old_name_right(client):
    """The reports that already exist are fixed the next time they are read."""
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    rep.filename = "Digital Marketing Report.pdf"
    rep.rules_version = "stale"
    db.commit()
    from app.recheck import recheck
    recheck(db, rep)
    db.expire_all()
    assert db.get(dbm.Report, rep.id).filename != "Digital Marketing Report.pdf"


def test_the_name_carries_every_order_the_report_covers(monkeypatch):
    """Congressman Mike Kelly's July report covers CTV on order 53130 and
    Online Audio on 50589 and 53130, and was named "...Mike Kelly 53130.pdf" -
    because the ids were only filled in when the file arrived with none at all.
    A name that names one of three orders looks complete and is not."""
    import datetime as dt

    from app import naming as N
    from app import roster as R

    class L:
        def __init__(self, ids, s, e):
            self.account_ids, self.canceled = ids, False
            self.starts_on, self.ends_on, self.flights = s, e, None

    lines = [L("53130", dt.date(2026, 6, 28), dt.date(2026, 7, 4)),
             L("50589 53130", dt.date(2026, 1, 1), dt.date(2026, 12, 31))]
    monkeypatch.setattr(R, "client_lines", lambda *a, **k: lines)

    class Rep:
        client, account_ids, period, is_lifetime = "Congressman Mike Kelly", "53130", "2026-07", False

    assert N.ids_for_report(None, Rep()) == "50589 53130"
    Rep.account_ids = N.ids_for_report(None, Rep())
    assert N.canonical_name(Rep()) == "July 2026_Congressman Mike Kelly 50589 53130.pdf"


def test_another_campaign_of_the_same_client_stays_out_of_the_name(monkeypatch):
    """Scoped to the lines this report is judged against."""
    import datetime as dt

    from app import naming as N
    from app import roster as R

    class L:
        def __init__(self, ids, s, e):
            self.account_ids, self.canceled = ids, False
            self.starts_on, self.ends_on, self.flights = s, e, None

    monkeypatch.setattr(R, "client_lines", lambda *a, **k: [
        L("53130", dt.date(2026, 6, 28), dt.date(2026, 7, 4)),
        L("99999", dt.date(2026, 9, 1), dt.date(2026, 12, 31))])

    class Rep:
        client, account_ids, period, is_lifetime = "Congressman Mike Kelly", "53130", "2026-07", False

    assert N.ids_for_report(None, Rep()) == "53130"


def test_the_pacing_panel_is_actually_built(client):
    """It imported client_flight from roster, which does not have it, so every
    report raised ImportError and the bare except turned that into an empty
    panel. Pacing did not go quiet on some reports - it was dead on all of
    them, and nothing said so because failing looked exactly like having
    nothing to say."""
    c, (db, dbm, imod) = client
    rep_id = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0].id
    html = c.get(f"/report/{rep_id}/view").text
    assert "could not be built" not in html
    assert "Pacing" in html and "Social Mirror" in html


def test_a_report_with_nothing_to_pace_says_why(client):
    """The panel simply vanished when there was nothing to compare against,
    which looks exactly like the panel being broken - "where did pacing go" is
    not a question a page should leave you holding."""
    c, (db, dbm, imod) = client
    db.query(dbm.OrderLine).delete()
    db.commit()
    rep_id = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0].id
    html = c.get(f"/report/{rep_id}/view").text
    assert "Nothing to pace against" in html
    assert "no order line matches this client" in html
