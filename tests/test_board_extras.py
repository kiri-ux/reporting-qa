"""The cycle board's small promises: pinned period, SEO tag, search scope."""
import datetime as dt
import re
from pathlib import Path

import pytest

TPL = Path(__file__).resolve().parents[1] / "app" / "templates"


def test_the_notifications_banner_is_gone():
    """It said the same thing on every load and never changed."""
    assert "Nothing is emailed or posted to Slack" not in (TPL / "cycle.html").read_text()


def test_the_partner_search_only_searches_partner_names():
    """It was searching the whole card, so a reporter's name matched 80 of them."""
    base = (TPL / "base.html").read_text()
    assert "el.dataset.search || el.textContent" in base
    assert 'data-search="{{ g.group }}' in (TPL / "cycle.html").read_text()


def test_the_pinned_period_is_a_setting_not_a_hard_coded_date():
    """Reloading app.config here would re-register app.db's mappers and break
    every test that runs after it, so read the default off the class."""
    from app.config import Settings
    assert Settings.model_fields["default_period"].default == "2026-07"


def test_an_seo_line_gives_the_group_its_own_s_tag():
    from app.board import Expected, GroupRow
    row = GroupRow(group="Curtis Media", target="", seo="Dana",
                   expected=[Expected(market="Curtis Media", group="Curtis Media",
                                      client="A", kind="monthly",
                                      products=["Search Engine Optimization+"])])
    assert row.seo == "Dana"
    assert '<b>S</b>{{ g.seo }}' in (TPL / "cycle.html").read_text()


def test_a_group_with_no_seo_line_gets_no_s_tag():
    """The tag is per cycle, not per partner - it means "there is SEO in here"."""
    from app.board import Expected, GroupRow
    row = GroupRow(group="Adapt Media", target="", expected=[
        Expected(market="Adapt Media", group="Adapt Media", client="A",
                 kind="monthly", products=["Social Mirror Ads"])])
    assert row.seo == ""


def test_is_seo_matches_what_the_order_export_actually_writes():
    from app.partners import is_seo
    assert is_seo("Search Engine Optimization+")
    assert is_seo("SEO")
    assert not is_seo("Social Mirror Ads")


def test_a_filter_never_prints_a_count_of_one():
    """On a Partner filter each partner appears exactly once, so the counts were
    a column of 1s that looked like they meant something.

    Judged per menu first ("show them only if any of them varies") - and one
    partner with two cards was enough to bring the whole column back. Judged
    per row it cannot.
    """
    base = (TPL / "base.html").read_text()
    assert "var informative" not in base
    assert "if (counts[n] > 1) {" in base


def test_site_ctr_findings_are_absent_from_every_real_fixture():
    """Asked directly: does the site/app CTR mismatch happen on live reports?

    It does not. A hundred site rows across five real reports all print a CTR
    that matches their own two columns. Only the assembled everything-sample
    disagrees with itself, which is worth knowing before anyone raises it with
    TapClicks.
    """
    from pathlib import Path as _P
    from app.checks.parser import pdf_text
    from app.checks.quality import site_rows

    bad = {}
    for f in sorted((_P(__file__).parent / "fixtures").glob("*.pdf")):
        for _t, name, imps, clicks, printed, _at in site_rows(pdf_text(f)):
            if printed is None or not imps:
                continue
            if abs(clicks / imps * 100 - printed) > 0.05:
                bad.setdefault(f.stem, []).append(name)
    assert bad == {}, bad


def test_the_recheck_control_is_a_button_not_a_banner():
    """A count beside a refresh arrow needs no sentence, and a banner across
    the top of the board pushed the partners down for something that is not
    news."""
    cycle = (TPL / "cycle.html").read_text()
    assert "Nothing is emailed" not in cycle
    assert 'class="note stale"' not in cycle
    assert "on this board were judged by older checking code" not in cycle
    # it lives with Download CSV, and it says how many
    assert 'class="sync"' in cycle and "{{ stale.total }} checks</button>" in cycle
    assert cycle.index('class="sync"') < cycle.index("Download CSV")
    # And the orders re-read is a button beside it rather than a yellow bar
    # across the width of the board.
    assert 'class="stalebar"' not in cycle
    assert ">orders</button>" in cycle


def test_the_button_becomes_a_progress_readout_while_it_runs():
    cycle = (TPL / "cycle.html").read_text()
    assert 'class="sync busy"' in cycle
    assert "{{ jobs.all.done }} of {{ jobs.all.total or '?' }}" in cycle


def test_a_partner_card_shows_its_own_job_not_just_the_board_wide_one():
    """The work happens in the background and the redirect comes straight back,
    so the button looked untouched after being pressed."""
    cycle = (TPL / "cycle.html").read_text()
    assert "jobs.by_group.get(g.group)" in cycle
    assert 'class="mini busy" data-job="{{ g.group }}"' in cycle


def test_the_progress_updates_without_a_reload():
    """The counter only moved when somebody reloaded, so a job that had stopped
    and a job that was working looked exactly the same."""
    cycle = (TPL / "cycle.html").read_text()
    assert "/cycle/recheck/status" in cycle
    assert "setInterval(tick" in cycle
    assert "stalled" in cycle


def test_the_partner_button_carries_no_count():
    """"Re-check 2" under a heading that says "14 reports" reads as a bug, even
    when 2 is the true number of stale ones. The numbers go in the hover and
    the button does the whole partner."""
    cycle = (TPL / "cycle.html").read_text()
    assert ">\n                Re-check</button>" in cycle
    assert "Re-check {{ stale.by_group[g.group] }}" not in cycle
    assert 'name="scope" value="all"' in cycle


def test_the_stale_counts_are_two_queries_not_two_per_partner():
    """290 COUNT queries on a 145-partner board is why it had started taking a
    moment to come up."""
    import inspect
    from app import main
    src = inspect.getsource(main._stale_here)
    assert "group_by(Report.market)" in src
    assert "market_names_for_group" not in src        # that one rebuilt the index


def test_markets_by_group_builds_the_index_once():
    import inspect
    from app import board
    assert "def markets_by_group" in inspect.getsource(board)


# --- checking a row off with no report -------------------------------------
#
# SEO is pulled outside TapClicks, so those rows sat at "Not received" all
# month and held their partner off ready for a report that was never coming.


def test_a_row_checked_off_with_no_report_reads_as_ready():
    from app.board import Expected
    e = Expected(market="Curtis Media", group="Curtis Media", client="A",
                 kind="monthly", products=["Search Engine Optimization+"])
    assert e.state == "missing" and not e.ready
    e.done_by = "kiri"
    assert e.state == "ready" and e.ready and e.done_only


def test_a_report_beats_the_check_off():
    """The mark says nothing is coming. If something came anyway, it is the
    thing to judge - not a row hidden behind somebody's tick."""
    from app.board import Expected

    class R:
        board_state, ready = "errors", False
    e = Expected(market="Curtis Media", group="Curtis Media", client="A",
                 kind="monthly", done_by="kiri", report=R())
    assert e.state == "errors" and not e.ready and not e.done_only


def test_the_check_off_is_for_one_cycle_only():
    """Next month the row comes back asking for a report, because SEO reports
    are going to start being uploaded."""
    from app.db import CycleDone
    cols = {c.name for c in CycleDone.__table__.columns}
    assert "period" in cols and "ident" in cols
    uq = [c for c in CycleDone.__table__.constraints
          if getattr(c, "name", "") == "uq_cycle_done"]
    assert uq and {c.name for c in uq[0].columns} == {"period", "ident"}


def test_the_board_row_offers_the_button_and_a_way_back():
    html = (TPL / "cycle.html").read_text()
    assert 'action="/cycle/done"' in html
    assert "Done, no report" in html
    assert 'name="action" value="clear"' in html


def test_the_mark_is_stamped_by_the_board_not_by_the_page():
    """The board, the CSV and the partner counts have to agree on whether a
    partner is finished."""
    src = (Path(__file__).resolve().parents[1] / "app" / "board.py").read_text()
    assert "_stamp_done(db, period, out)" in src


def test_every_view_of_the_stored_file_carries_a_version_token():
    """The URL is identical before and after a replacement, so a browser that
    cached the old one showed the wrong logo beside the right report."""
    html = (TPL / "viewer.html").read_text()
    for m in re.finditer(r'"(/report/\{\{ rep\.id \}\}/(?:file|logo\.png))([^"]*)"', html):
        assert "v={{ file_v }}" in m.group(2), m.group(0)


def test_the_report_page_says_which_kind_of_report_it_is():
    """Only the lifetime pill was printed, so a monthly was "a report with no
    lifetime pill" - readable only if you knew the pill existed."""
    html = (TPL / "viewer.html").read_text()
    assert '<span class="pill p-info">lifetime</span>' in html
    assert '<span class="pill p-month">monthly</span>' in html
    assert ".p-month{" in (TPL / "base.html").read_text()


def test_upload_and_done_sit_on_one_line():
    html = (TPL / "cycle.html").read_text()
    assert '<div class="rowacts">' in html
    assert ".rowacts{display:flex" in html


def test_packaging_runs_in_the_background():
    """It uploads every PDF in the partner one after another - minutes on a big
    one - and that was happening inside the browser request."""
    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    i = src.index("def deliver_group(")
    body = src[i:i + 1400]
    assert "start_delivery(" in body
    assert "rec = deliver(" not in body


def test_a_packaging_run_shows_its_progress_on_the_card():
    html = (TPL / "cycle.html").read_text()
    assert "packing.get(g.group)" in html
    assert "packaged" in html


def test_a_packaging_job_that_died_with_a_deploy_is_closed_out():
    """A card cannot sit on "12 of 30" forever looking like the tool is stuck."""
    import datetime as dt
    from app.db import DeliveryJob
    j = DeliveryJob(key="deliver:2026-07:X", state="running", done=12, total=30,
                    started_at=dt.datetime.utcnow() - dt.timedelta(minutes=30),
                    updated_at=dt.datetime.utcnow() - dt.timedelta(minutes=30))
    assert j.stalled
    j.updated_at = dt.datetime.utcnow()
    assert not j.stalled


def test_drive_reads_each_folder_once_not_once_per_file():
    """Two round trips to Google before a byte moved, per PDF."""
    src = (Path(__file__).resolve().parents[1] / "app" / "delivery.py").read_text()
    assert "if dest not in dest_files:" in src
    assert "if parent_folders is None:" in src


def test_the_roster_table_has_a_cell_for_every_heading():
    """Nine headings, eight cells: the delivery dropdown sat under "Trainer"
    and every column from there rightwards showed the one beside it."""
    import re as _re
    html = (TPL / "partners.html").read_text()
    head = html[html.index("<thead>"):html.index("</thead>")]
    body = html[html.index("<tbody>"):html.index("</tbody>")]
    row = body[body.index("<tr>"):body.index("</tr>")]
    assert len(_re.findall(r"<th\b", head)) == len(_re.findall(r"<td\b", row))
    assert "{{ p.trainer }}" in html


def test_where_reports_go_sits_above_the_roster():
    """It was below 280 partner rows, which is where things go to not be
    found."""
    html = (TPL / "partners.html").read_text()
    assert html.index("Where reports go") < html.index("Roster ({{ partners|length }})")


def test_the_package_button_says_something_the_moment_it_is_pressed():
    html = (TPL / "cycle.html").read_text()
    assert 'form[action*="/deliver"]' in html
    assert "Packaging..." in html


def test_the_way_back_from_a_report_survives_the_page_reloading_itself():
    """Accepting a finding, saving a note and re-checking all redirect back to
    the report, and from then on the referer IS the report - so Reviewed left
    you sitting on the one you had just signed off."""
    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert 'BACK_COOKIE = "qa_back"' in src
    assert "target or _back_cookie(request)" in src
    assert 'resp.set_cookie(BACK_COOKIE' in src


def test_the_logo_panel_shows_even_when_no_fingerprint_was_taken():
    """It was hidden on a report whose hash came back empty - which is exactly
    the report somebody is trying to mark, because a corner the tool could not
    read is usually the one carrying the tool's own mark."""
    html = (TPL / "viewer.html").read_text()
    assert "{% if has_file %}" in html
    assert "{% if has_file and not logo_hash %}" in html
    assert "Read the logo again" in html
    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert '@app.post("/report/{report_id}/logo/refresh")' in src


def test_the_findings_state_the_fact_and_stop():
    """"More than 50% either way is worth a look before this goes out - either
    the order attached is the wrong one, or the campaign needs a conversation"
    is commentary on a number the reader can already see."""
    src = (Path(__file__).resolve().parents[1] / "app" / "checks" / "rules.py").read_text()
    for phrase in ("worth a look before this goes out",
                   "needs a conversation",
                   "usually a make-good conversation",
                   "Check which client was picked in TapClicks",
                   "it is not nothing"):
        assert phrase not in src, phrase


def test_every_pacing_caller_passes_the_flight_window():
    """A lifetime paces against the campaign it reports on. Miss the window on
    one caller and that page silently goes back to counting every order the
    client has."""
    import re as _re
    root = Path(__file__).resolve().parents[1] / "app"
    for f in ("main.py", "recheck.py", "ingest.py"):
        src = (root / f).read_text()
        for m in _re.finditer(r"ordered_for\(([^;]*?)\)\n", src):
            assert "window=" in m.group(1), f"{f}: {m.group(0)[:80]}"


# Findings that are about the ORDER versus the whole report, not about a spot
# on a page. A page number on these would be made up.
NO_PLACE = {"product_missing", "product_rogue", "pacing", "pacing_off",
            "rule_error", "completion_missing"}


def test_every_finding_says_which_page_to_look_at():
    """"Rate printed above 100%" on a forty-five page report is a scavenger
    hunt. Made this note a lot."""
    import ast
    root = Path(__file__).resolve().parents[1] / "app" / "checks"
    missing = []
    for f in ("rules.py", "quality.py"):
        for node in ast.walk(ast.parse((root / f).read_text())):
            if not (isinstance(node, ast.Call)
                    and getattr(node.func, "id", "") == "_f"):
                continue
            if "where" in {k.arg for k in node.keywords}:
                continue
            code = node.args[0].value if node.args else "?"
            if code in NO_PLACE:
                continue
            missing.append(f"{f}:{node.lineno} {code}")
    assert not missing, "findings with no page: " + "; ".join(missing)


def test_the_rate_ceiling_finding_carries_its_page_and_widget():
    from app.checks.rules import check_rate_ceiling
    text = ("OVERVIEW - PAGE 1\nSocial Mirror Creative Performance\n"
            " row   1   2\n"
            "Completion Performance\n  Strategy   101.16%\n")
    out = check_rate_ceiling({"text": text, "page_of": lambda _o: 12})
    assert out[0]["where"] == "p12 · Completion Performance"


def test_seo_is_never_owed_a_lifetime():
    """It is bought by the month and reported on by the month. There is no
    campaign that finishes and no delivery-to-date to sum up."""
    src = (Path(__file__).resolve().parents[1] / "app" / "board.py").read_text()
    assert "life = (not is_seo(l.product)) and cyc.needs_lifetime(" in src


def test_the_order_panel_shows_what_the_month_was_bought_to_do():
    html = (TPL / "report_orders_body.html").read_text()
    for col in ("Impressions", "Budget", "Ad spend"):
        assert col in html
    for field in ("r.impressions", "r.budget", "r.spend",
                  "r.total_impressions", "r.total_budget"):
        assert field in html


# --------------------------------------- checking the board against a list
def test_a_pasted_row_gives_up_its_client_kind_and_orders():
    from app.audit import parse_list
    assert parse_list("7MOU SG - Benton Rodeo #53915 LIFETIME") == [
        {"raw": "7MOU SG - Benton Rodeo #53915 LIFETIME",
         "client": "Benton Rodeo", "ids": ["53915"], "kind": "lifetime"}]


def test_several_order_ids_on_one_campaign_are_all_read():
    from app.audit import parse_list
    got = parse_list("7MOU SG - Roto Rooter #29818/#42452/#42808")
    assert got[0]["ids"] == ["29818", "42452", "42808"]


def test_a_comma_inside_the_client_name_does_not_split_it_in_half():
    """"Altiery Gingerich Insurance Agency, LLC #53106 SEO" comes out of a CSV
    as two cells, and the half carrying the id is "LLC #53106 SEO"."""
    from app.audit import parse_list
    got = parse_list("7MOU SC - Altiery Gingerich Insurance Agency, LLC #53106 SEO")
    assert got[0]["client"] == "Altiery Gingerich Insurance Agency, LLC"
    assert got[0]["ids"] == ["53106"]


def test_a_tab_pasted_sheet_reads_the_column_with_the_ids_in_it():
    from app.audit import parse_list
    line = "7 Mountains PA Selinsgrove\tLive Campaigns\t7MOU SG - Salem RV #52793\t8/31/2026"
    got = parse_list(line)
    assert got == [{"raw": "7MOU SG - Salem RV #52793", "client": "Salem RV",
                    "ids": ["52793"], "kind": "monthly"}]


def test_the_audit_matches_on_order_id_and_reports_both_directions():
    from app.audit import audit
    from app.board import Expected

    rows = [Expected(market="7 Mountains PA Selinsgrove", group="7 Mountains",
                     client="Salem RV", kind="monthly", account_ids="52793"),
            Expected(market="7 Mountains PA Selinsgrove", group="7 Mountains",
                     client="SVEC", kind="monthly", account_ids="52277")]

    import app.board as B
    real = B.expected_for
    B.expected_for = lambda *a, **k: rows
    try:
        got = audit(None, "2026-07",
                    "7MOU SG - Salem RV #52793\n7MOU SG - Benton Rodeo #53915")
    finally:
        B.expected_for = real
    assert [m["client"] for m in got["matched"]] == ["Salem RV"]
    assert [m["client"] for m in got["missing"]] == ["Benton Rodeo"]
    assert [e.client for e in got["extra"]] == ["SVEC"]
