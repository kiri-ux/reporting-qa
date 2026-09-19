"""The buyer's half of a report, kept out of the reporting team's queue.

Nine checks out of forty are the buyer's: the products on the order, the names
given to strategies, fences and conversions, a Social Mirror creative that is
really a display banner, delivery miles off what was sold. None of them is
something whoever reads reports can fix, and none of them was ever going to be
fixed by holding the PDF - but they arrived in the same red list as a blank
widget, counted toward the same severity, and had to be ticked off one at a
time before a report could be signed off.

So three things, and they are one change:

  * `open_findings` is what holds a report up, and the buyer's are not in it.
    The severity, the board state, the ready flag and the "select the ones
    that passed" box all read that list, so they all follow.
  * They are on their own panel on the report and their own tag on the board
    row, with the flags themselves on hover.
  * And on a link per partner that can be sent to the buyer, which opens on
    EVERY report rather than the pending ones - a report this team has already
    signed off still has whatever the order gave it.
"""
import datetime as dt
import importlib

import pytest


# ------------------------------------------------------------- whose desk
def test_every_finding_code_belongs_to_a_check_the_catalog_names():
    """The owner is written against the CHECK, and findings carry a CODE.

    Old findings - hundreds of thousands of them - are not stamped with the
    check that wrote them, so the only way back is the code. That join has to
    cover every code, or a buyer's flag stored last month silently reads as
    the reporting team's.

    It broke exactly once already and this is the guard for it: check_pacing_off
    is one row on the flags page written as two functions, so its codes were
    owned by names the catalog has never heard of.
    """
    from app.checkctl import code_owners
    from app.flag_catalog import CHECK_OWNER

    orphans = {code: sorted(owners) for code, owners in code_owners().items()
               if owners and not (set(owners) & set(CHECK_OWNER))}
    assert not orphans, f"codes with no catalogued check: {orphans}"


def test_a_helper_half_of_a_check_answers_as_the_check():
    """check_pacing_off is one check and two functions, and the switch, the
    catalog and the flags page all know only the one name."""
    from app.checkctl import code_owners

    assert code_owners()["pacing"] == frozenset({"check_pacing_off"})
    assert code_owners()["pacing_off"] == frozenset({"check_pacing_off"})
    assert code_owners()["wrong_client"] == frozenset({"check_client_wrong"})


def test_a_finding_nobody_can_place_stays_with_the_reporting_team():
    """"" is not "buyer".

    The buyer's panel is the one that does not hold a sign-off, so a wrong
    guess in that direction ships a report nobody read. A wrong guess the
    other way costs somebody a glance.
    """
    from app.flag_catalog import is_buyer, owner_of

    assert owner_of({"code": "rule_error"}) == ""
    assert owner_of({"code": "nothing_like_this"}) == ""
    assert not is_buyer({"code": "rule_error"})


def test_the_stamp_on_the_finding_wins_over_the_code():
    from app.flag_catalog import owner_of

    # The code says admin; the check that wrote it says buyer. New findings
    # carry the check, and it is the better answer.
    assert owner_of({"check": "check_products", "code": "device_over"}) == "buyer"


# -------------------------------------------------- what holds a report up
def _rep(dbm, findings, **kw):
    return dbm.Report(batch_id=1, filename="Aug 2026_C 10.pdf", client="C",
                      market="P", period="2026-08", severity="fail",
                      checks=[], findings=findings, **kw)


BUYER_FLAG = {"code": "geofence_no_business_name", "check": "check_geofence_names",
              "severity": "warn", "title": "4 geo-fence rows have no business name",
              "where": "p12 · Geo-Fencing"}
BUYER_FAIL = {"code": "product_missing", "check": "check_products",
              "severity": "fail", "title": "Geo-Framing is ordered and not on the report"}
REPORTER_FLAG = {"code": "blank_widget_page", "check": "check_blank_pages",
                 "severity": "warn", "title": "Page 9 is blank"}


@pytest.fixture()
def dbm(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'b.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import app.config
    import app.db
    for m in (app.config, app.db):
        importlib.reload(m)
    app.db.init_db()
    yield app.db
    monkeypatch.undo()
    import app.main
    for m in (app.config, app.db, app.main):
        importlib.reload(m)


def test_a_buyer_flag_does_not_hold_the_report(dbm):
    """The one that started this. A missing product is a fail, and it is a
    fail about the ORDER - the report is a true picture of a buy that is wrong.
    Holding the PDF does not put the product on it."""
    rep = _rep(dbm, [BUYER_FAIL, BUYER_FLAG])
    assert rep.open_findings == []
    assert rep.effective_severity == "pass"
    rep.review_state = "reviewed"
    assert rep.ready
    assert rep.board_state == "ready"


def test_a_buyer_flag_is_still_on_the_report(dbm):
    rep = _rep(dbm, [BUYER_FAIL, BUYER_FLAG])
    assert [f["code"] for f in rep.buyer_findings] == ["product_missing",
                                                       "geofence_no_business_name"]


def test_the_reporting_team_keeps_everything_else(dbm):
    rep = _rep(dbm, [BUYER_FLAG, REPORTER_FLAG])
    assert [f["code"] for f in rep.open_findings] == ["blank_widget_page"]
    assert rep.effective_severity == "warn"
    assert [f["code"] for f in rep.buyer_findings] == ["geofence_no_business_name"]


def test_a_report_with_nothing_but_buyer_flags_reads_as_passed(dbm):
    """Which is what the tag on the board row is for - the findings column
    says "All checks passed" and the tag beside it says who still has
    something to look at."""
    rep = _rep(dbm, [BUYER_FLAG])
    assert rep.open_findings == []
    assert rep.buyer_findings


def test_accepting_a_buyer_flag_takes_it_off_the_list(dbm):
    """The tick is not needed to sign a report off any more, and it still
    works: it is how the tag comes off the row once somebody has dealt with
    it."""
    rep = _rep(dbm, [BUYER_FLAG], acked=[0])
    assert rep.buyer_findings == []
    assert rep.open_findings == []


def test_a_report_that_could_not_be_parsed_still_fails(dbm):
    """No findings itemized at all: the stored verdict is the only answer
    there is, and computing from an empty list would call it clean."""
    rep = _rep(dbm, [])
    assert rep.effective_severity == "fail"


# ------------------------------------------------------------- the link
def test_a_buyer_link_names_the_partner_it_was_made_for():
    from app.buyer_link import group_of, token_for

    for name in ("Amazing Results LLC", "7 Mountains KY", "Woof Boom / Lima"):
        assert group_of(token_for(name)) == name


def test_a_buyer_link_with_the_partner_swapped_is_refused():
    """The name travels in the link so nothing has to be stored. The signature
    is what stops somebody editing it to another partner's list."""
    import base64

    from app.buyer_link import group_of, token_for

    _name, _, sig = token_for("Amazing Results LLC").partition(".")
    other = base64.urlsafe_b64encode(b"Woof Boom").decode().rstrip("=")
    assert group_of(f"{other}.{sig}") == ""
    assert group_of("") == ""
    assert group_of("nonsense") == ""
    assert group_of("bm90YmFzZTY0.xxxx") == ""


def test_changing_the_secret_revokes_every_link_already_sent(monkeypatch):
    from app import buyer_link

    monkeypatch.setattr(buyer_link.settings, "buyer_link_secret", "one")
    was = buyer_link.token_for("P")
    monkeypatch.setattr(buyer_link.settings, "buyer_link_secret", "two")
    assert buyer_link.group_of(was) == ""


# -------------------------------------------------------------- the page
@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'p.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTO_RECHECK", "false")
    monkeypatch.setenv("BUYER_LINK_SECRET", "test-secret")
    import app.config
    import app.db
    import app.main
    for m in (app.config, app.db, app.main):
        importlib.reload(m)
    from fastapi.testclient import TestClient

    app.db.Base.metadata.create_all(app.db.engine)
    db = app.db.SessionLocal()
    D = dt.date.fromisoformat
    db.add(app.db.Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    for market in ("Amazing Results LLC", "Other Partner"):
        db.add(app.db.Partner(partner=market, group=market))
        db.add(app.db.OrderLine(
            market=market, client=f"{market} Client", account_ids="10",
            line_ids="1", product="Mobile Conquesting", campaign="Mobile",
            live=True, status="IO Live",
            starts_on=D("2026-01-01"), ends_on=D("2026-12-31"),
            order_starts_on=D("2026-01-01"), order_ends_on=D("2026-12-31")))
    db.commit()
    pdf = tmp_path / "one.pdf"
    pdf.write_bytes(b"%PDF-1.4 not really\n")
    # SIGNED OFF, AND WITH A BUYER FLAG ON IT. This is the row the buyer's
    # page exists for: the reporting team is finished with it and the order
    # still has a fence with no business name on it.
    db.add(app.db.Report(
        batch_id=1, filename="Aug 2026_Amazing Results LLC Client 10.pdf",
        stored_path=str(pdf), client="Amazing Results LLC Client",
        market="Amazing Results LLC", period="2026-08", severity="warn",
        checks=[], review_state="reviewed", reviewed_by="Paulina",
        findings=[BUYER_FLAG]))
    db.add(app.db.Report(
        batch_id=1, filename="Aug 2026_Other Partner Client 10.pdf",
        stored_path=str(pdf), client="Other Partner Client",
        market="Other Partner", period="2026-08", severity="pass",
        checks=[], findings=[]))
    db.commit()
    db.close()
    yield TestClient(app.main.app), app
    monkeypatch.undo()
    for m in (app.config, app.db, app.main):
        importlib.reload(m)


def test_the_buyer_page_opens_on_every_report_not_the_pending_ones(client):
    """The board's default is Pending, which is a fact about this team's queue.
    A report that has been signed off still has whatever the order gave it,
    and it is the one the buyer is most likely to be asked about."""
    c, app = client
    url = app.buyer_link.url_for("", "Amazing Results LLC")
    page = c.get(f"{url}?period=2026-08")
    assert page.status_code == 200
    body = page.text
    assert "Amazing Results LLC Client" in body          # signed off, still listed
    assert "4 geo-fence rows have no business name" in body
    assert "Other Partner Client" not in body


def test_the_buyer_page_can_be_cut_to_what_is_flagged(client):
    c, app = client
    url = app.buyer_link.url_for("", "Amazing Results LLC")
    assert "Amazing Results LLC Client" in c.get(
        f"{url}?period=2026-08&only=flagged").text


def test_a_token_that_was_not_signed_here_opens_nothing(client):
    c, _app = client
    assert c.get("/buyer/bm90aGluZw.abcdefghijklmnopqrst").status_code == 404


def test_one_partners_link_does_not_serve_another_partners_pdf(client):
    """The permission IS the row: the report has to be on this cycle's board
    for this partner. Nothing is taken from the URL but the id."""
    from sqlalchemy import select

    c, app = client
    db = app.db.SessionLocal()
    mine, theirs = (db.scalar(select(app.db.Report)
                              .where(app.db.Report.market == m))
                    for m in ("Amazing Results LLC", "Other Partner"))
    mine_id, theirs_id = mine.id, theirs.id
    db.close()
    url = app.buyer_link.url_for("", "Amazing Results LLC")
    assert c.get(f"{url}/report/{mine_id}/file?period=2026-08").status_code == 200
    assert c.get(f"{url}/report/{theirs_id}/file?period=2026-08").status_code == 404


def test_the_board_row_carries_the_tag_and_the_flags_on_it(client):
    """On the board it is a tag and a hover, not four lines in the findings
    column - that column is what the reporting team acts on."""
    c, _app = client
    body = c.get("/cycle?period=2026-08&group=Amazing+Results+LLC&done=all").text
    assert "Buyer review needed" in body
    assert "4 geo-fence rows have no business name" in body
    # ...and NOT as one of the things holding the report up.
    assert "All checks passed" in body


def test_the_board_card_carries_the_partners_link(client):
    c, app = client
    body = c.get("/cycle?period=2026-08&group=Amazing+Results+LLC").text
    assert app.buyer_link.token_for("Amazing Results LLC") in body


def test_the_report_page_puts_them_under_their_own_heading(client):
    from sqlalchemy import select

    c, app = client
    db = app.db.SessionLocal()
    rid = db.scalar(select(app.db.Report)
                    .where(app.db.Report.market == "Amazing Results LLC")).id
    db.close()
    body = c.get(f"/report/{rid}/view").text
    assert "Buyer review" in body
    assert body.index("Needs a look") < body.index("Buyer review")
    assert "4 geo-fence rows have no business name" in body


def test_a_buyer_link_is_not_behind_the_site_password(client, monkeypatch):
    """It is sent to somebody who does not have it, and the signature on the
    link is what stands in its place."""
    from app.main import settings

    monkeypatch.setattr(settings, "site_password", "hunter2")
    c, app = client
    url = app.buyer_link.url_for("", "Amazing Results LLC")
    assert c.get("/cycle?period=2026-08").status_code == 401
    assert c.get(f"{url}?period=2026-08").status_code == 200
