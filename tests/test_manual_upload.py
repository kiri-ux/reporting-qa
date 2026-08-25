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
