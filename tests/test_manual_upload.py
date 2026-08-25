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
    r = dbm.Report(batch_id=1, period="2026-07", client="c", review_state="new")
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
    assert "sign-off pulled" in board.text
    # And the name is not sitting in the reviewer column as though it stood.
    assert ">k</span>" not in board.text


# ------------------------------------------------------- the link, at the top
def test_a_delivered_partner_puts_its_link_at_the_top_of_the_board(client):
    """It sorts in among 145 others otherwise, so the one thing somebody came
    to the page for was found by scrolling."""
    c, (db, dbm, imod) = client
    rep = _feed(imod, db, (FIXTURES / "benton_rodeo.pdf").read_bytes()).reports[0]
    db.add(dbm.Delivery(period=rep.period, group=rep.market, target="drive",
                        reports=1, ok=True,
                        share_url="https://drive.google.com/drive/folders/abc123"))
    db.commit()

    page = c.get(f"/cycle?period={rep.period}")
    assert page.status_code == 200
    head, tail = page.text.split('class="glist"', 1)
    # Above the partner list, not inside it.
    assert "https://drive.google.com/drive/folders/abc123" in head
    assert "Client links" in head
    assert "Partners delivered" in head
    # And copyable, not just openable.
    assert 'data-copy="https://drive.google.com/drive/folders/abc123"' in head


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
