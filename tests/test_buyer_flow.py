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
    assert ">Buyer review</span>" in body
    assert "4 geo-fence rows have no business name" in body
    # ...and NOT as one of the things holding the report up.
    assert "All checks passed" in body


def test_the_board_can_be_cut_to_the_rows_with_buyer_flags():
    """Not a status and not one of the Findings menu entries - it is the
    question that cuts across both, and it is the list that gets sent on."""
    from app.main import cycle_view
    import inspect

    assert "buyer_review" in inspect.signature(cycle_view).parameters


def test_the_buyer_filter_survives_a_page_turn(client):
    """The chips are links and the pagers are built from one macro - anything
    not carried by it is a filter that silently drops on page 2. Added by hand
    was on that list for weeks and No new file never was."""
    from pathlib import Path

    keep = Path("app/templates/cycle.html").read_text().split("{% endmacro %}")[0]
    assert "buyerreview" in keep
    assert "waiting=1" in keep
    body = c_get(client, "/cycle?period=2026-08&buyerreview=1&done=all")
    assert "Amazing Results LLC Client" in body


def c_get(client, url):
    c, _app = client
    r = c.get(url)
    assert r.status_code == 200, url
    return r.text


# ------------------------------------------------- what the buyer can press
def test_the_buyer_can_mark_a_flag_off(client):
    """The same accepted list the report page writes, so a flag dealt with
    here stops showing on the board row and on the report."""
    from sqlalchemy import select

    c, app = client
    db = app.db.SessionLocal()
    rid = db.scalar(select(app.db.Report)
                    .where(app.db.Report.market == "Amazing Results LLC")).id
    db.close()
    url = app.buyer_link.url_for("", "Amazing Results LLC")
    r = c.post(f"{url}/report/{rid}/ack?period=2026-08",
               data={"index": "0", "on": "1"}, follow_redirects=False)
    assert r.status_code == 303
    db = app.db.SessionLocal()
    rep = db.get(app.db.Report, rid)
    assert rep.acked == [0]
    assert rep.buyer_findings == []
    # AND IT IS NOT A SIGN-OFF. Ticking the last thing holding a report up is
    # a review; nothing on this page was ever holding one up.
    assert rep.review_state == "reviewed"    # it already was, from the fixture
    db.close()


def test_a_note_belongs_to_the_flag_it_is_typed_under(client):
    """The report already carries a note and it is one note for the whole
    report, which is the wrong shape for four flags on four different
    orders."""
    from sqlalchemy import select

    c, app = client
    db = app.db.SessionLocal()
    rep = db.scalar(select(app.db.Report)
                    .where(app.db.Report.market == "Amazing Results LLC"))
    rep.findings = [BUYER_FLAG, BUYER_FAIL]
    db.commit()
    rid = rep.id
    db.close()
    url = app.buyer_link.url_for("", "Amazing Results LLC")

    r = c.post(f"{url}/report/{rid}/note?period=2026-08",
               data={"index": "1", "note": "Station is renaming it"},
               follow_redirects=False)
    assert r.status_code == 303
    db = app.db.SessionLocal()
    rep = db.get(app.db.Report, rid)
    # KEYED BY STRING. JSON object keys are strings whatever they went in as,
    # so the note came back out under "1" and read as no note at all.
    assert rep.note_on(1) == "Station is renaming it"
    assert rep.note_on(0) == ""
    db.close()
    assert "Station is renaming it" in c.get(f"{url}?period=2026-08").text


def test_an_emptied_note_is_gone_rather_than_blank(client):
    from sqlalchemy import select

    c, app = client
    db = app.db.SessionLocal()
    rep = db.scalar(select(app.db.Report)
                    .where(app.db.Report.market == "Amazing Results LLC"))
    rep.findings = [BUYER_FLAG]
    rep.flag_notes = {"0": "was something"}
    db.commit()
    rid = rep.id
    db.close()
    url = app.buyer_link.url_for("", "Amazing Results LLC")
    c.post(f"{url}/report/{rid}/note?period=2026-08",
           data={"index": "0", "note": "   "}, follow_redirects=False)
    db = app.db.SessionLocal()
    assert db.get(app.db.Report, rid).flag_notes == {}
    db.close()


def test_a_note_cannot_be_written_on_the_reporting_teams_flag(client):
    from sqlalchemy import select

    c, app = client
    db = app.db.SessionLocal()
    rep = db.scalar(select(app.db.Report)
                    .where(app.db.Report.market == "Amazing Results LLC"))
    rep.findings = [BUYER_FLAG, REPORTER_FLAG]
    db.commit()
    rid = rep.id
    db.close()
    url = app.buyer_link.url_for("", "Amazing Results LLC")
    r = c.post(f"{url}/report/{rid}/note?period=2026-08",
               data={"index": "1", "note": "nope"}, follow_redirects=False)
    assert r.status_code == 403


def test_the_buyer_cannot_tick_off_the_reporting_teams_work(client):
    """One page, one list, one thing it can write to. A hand-posted index must
    not reach into what the report is actually being held for."""
    import datetime as dt
    from sqlalchemy import select

    c, app = client
    db = app.db.SessionLocal()
    rep = db.scalar(select(app.db.Report)
                    .where(app.db.Report.market == "Amazing Results LLC"))
    rep.findings = [BUYER_FLAG, REPORTER_FLAG]
    rep.review_state = "new"
    db.commit()
    rid = rep.id
    db.close()
    url = app.buyer_link.url_for("", "Amazing Results LLC")
    r = c.post(f"{url}/report/{rid}/ack?period=2026-08",
               data={"index": "1", "on": "1"}, follow_redirects=False)
    assert r.status_code == 403
    db = app.db.SessionLocal()
    assert db.get(app.db.Report, rid).acked == []
    db.close()


def test_one_partners_link_does_not_tick_another_partners_report(client):
    c, app = client
    from sqlalchemy import select
    db = app.db.SessionLocal()
    theirs = db.scalar(select(app.db.Report)
                       .where(app.db.Report.market == "Other Partner")).id
    db.close()
    url = app.buyer_link.url_for("", "Amazing Results LLC")
    r = c.post(f"{url}/report/{theirs}/ack?period=2026-08",
               data={"index": "0", "on": "1"}, follow_redirects=False)
    assert r.status_code == 404


def test_the_buyer_can_see_what_the_client_is_paying_for(client):
    """Every flag on this page is about the order, and the answer to all of
    them is on the order lines."""
    from sqlalchemy import select

    c, app = client
    db = app.db.SessionLocal()
    rid = db.scalar(select(app.db.Report)
                    .where(app.db.Report.market == "Amazing Results LLC")).id
    db.close()
    url = app.buyer_link.url_for("", "Amazing Results LLC")
    body = c.get(f"{url}/report/{rid}/orders?period=2026-08&frag=1").text
    assert "Mobile Conquesting" in body
    # The sync button is behind the site password, so it is not on their copy.
    assert "/orders/sync" not in body


def test_the_report_opens_beside_the_list_not_over_it(client):
    """A panel that appears is a panel that has to be dismissed, and
    dismissing it is what put the list - and the ticks on it - back out of
    reach. It is on the page from load and the file goes into it."""
    c, app = client
    url = app.buyer_link.url_for("", "Amazing Results LLC")
    body = c.get(f"{url}?period=2026-08").text
    assert 'class="bview"' in body          # there before anything is clicked
    assert "data-open=" in body
    assert "/orders?period=2026-08&amp;frag=1" in body
    assert "/pacing?period=2026-08" in body
    assert "/report/" in body               # the QA page the reporter works off


def test_pacing_and_order_lines_open_in_the_row(client):
    """A sheet covers the report you opened to read them against, which is the
    one thing this layout exists to keep on screen."""
    c, app = client
    url = app.buyer_link.url_for("", "Amazing Results LLC")
    body = c.get(f"{url}?period=2026-08").text
    assert "data-drawer=" in body
    assert 'class="rowdrawer"' in body
    # ...and NOT as the sheet that covers the panel. The rules link in the top
    # bar is the tool's own and keeps its sheet.
    assert "data-sheet=\"/buyer/" not in body


def test_the_reporting_teams_flags_are_there_to_look_at(client):
    """None of them is the buyer's to fix, and they are the one being asked
    about the report - "why is page nine blank" reaches them. Behind a button
    with the count on it, so none of it reads as something for them to do."""
    from sqlalchemy import select

    c, app = client
    db = app.db.SessionLocal()
    rep = db.scalar(select(app.db.Report)
                    .where(app.db.Report.market == "Amazing Results LLC"))
    rep.findings = [BUYER_FLAG, REPORTER_FLAG]
    db.commit()
    rid = rep.id
    db.close()
    url = app.buyer_link.url_for("", "Amazing Results LLC")

    body = c.get(f"{url}?period=2026-08").text
    assert "Reporting flags · 1" in body

    drawer = c.get(f"{url}/report/{rid}/flags?period=2026-08").text
    assert "Page 9 is blank" in drawer
    # ...and not the buyer's own, which are already on the row.
    assert "4 geo-fence rows have no business name" not in drawer


def test_a_report_with_nothing_open_says_so_quietly(client):
    c, app = client
    url = app.buyer_link.url_for("", "Amazing Results LLC")
    body = c.get(f"{url}?period=2026-08").text
    # No count, and no amber.
    assert ">Reporting flags</a>" in body


def test_the_buyer_page_does_not_print_page_tags(client):
    """The report is open beside the list, and "p1 · cover page" against a
    campaign that finished under its goal points at nothing to go and look
    at - the finding is about the order, not a place on the page."""
    c, app = client
    url = app.buyer_link.url_for("", "Amazing Results LLC")
    body = c.get(f"{url}?period=2026-08").text
    assert "4 geo-fence rows have no business name" in body
    assert "p12 · Geo-Fencing" not in body


def test_the_board_does_not_tag_a_report_as_renamed(client):
    """Filing a report under the right name is not something to do about it,
    and the tag sat on a third of the board above the findings - in the one
    column somebody is scanning for work. The fact stays on the report page."""
    from pathlib import Path

    c, _app = client
    body = c.get("/cycle?period=2026-08&done=all").text
    assert ">renamed</span>" not in body
    assert "renamed_from" in Path("app/templates/viewer.html").read_text()


def test_the_buyer_can_read_the_pacing(client):
    """Collapsed on the report page, because whoever reads reports does not
    act on it. Here it is the whole question."""
    from sqlalchemy import select

    c, app = client
    db = app.db.SessionLocal()
    rid = db.scalar(select(app.db.Report)
                    .where(app.db.Report.market == "Amazing Results LLC")).id
    db.close()
    url = app.buyer_link.url_for("", "Amazing Results LLC")
    r = c.get(f"{url}/report/{rid}/pacing?period=2026-08")
    assert r.status_code == 200


def test_the_pacing_panel_is_drawn_once(client):
    """It is on the report page and on the buyer's, and two copies of that
    markup drift the week either one is touched."""
    from pathlib import Path

    assert Path("app/templates/pacing_body.html").exists()
    for page in ("viewer.html", "buyer_pacing.html"):
        assert 'include "pacing_body.html"' in Path(f"app/templates/{page}").read_text()


def test_the_buyer_keeps_the_way_back_home(client):
    """It was drawn with the rail and the top bar left out, on the theory that
    every link in them is behind the site password. There is no password, so
    that was a page with no way off it."""
    c, app = client
    url = app.buyer_link.url_for("", "Amazing Results LLC")
    body = c.get(f"{url}?period=2026-08").text
    assert 'class="rail"' in body
    assert "Vici sign-in" not in body


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
