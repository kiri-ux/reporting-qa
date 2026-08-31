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
    import re as _re
    from app.config import Settings
    got = Settings.model_fields["default_period"].default
    assert _re.fullmatch(r"\d{4}-\d{2}", got or ""), \
        "a pinned period, in the form the rest of the tool stores"


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
    assert "life = (not is_seo(l.product)) and ran and (" in src


def test_the_order_panel_shows_what_the_month_was_bought_to_do():
    html = (TPL / "report_orders_body.html").read_text()
    # The eleven-column version had a header per number and needed dragging
    # sideways to reach the dates. Two stacked money columns instead.
    for col in ("This month", "Campaign total"):
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


def test_the_cycle_search_covers_the_whole_cycle_not_just_the_page():
    """The box filtered the rows the browser had, and the table is capped at
    150 of 763 - so "paul" said "28 of 150 rows" and looked like it had
    searched everything."""
    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert "q: str = Query(\"\")" in src
    assert "rows = [e for e in rows if _matches(e, q)]" in src
    html = (TPL / "cycle.html").read_text()
    assert 'name="q" value="{{ q }}"' in html
    # The hint moved into the box itself when the table went from a 150-row cap
    # to real pagination. The promise is the same: Enter searches the cycle.
    assert "Enter to search all" in html


def test_a_search_matches_every_word_anywhere_on_the_row():
    from app.board import Expected
    from app.main import _matches
    e = Expected(market="ADX Communications", group="ADX", client="Armada Advisors",
                 kind="monthly", account_ids="53174", reporter="Paulina",
                 products=["CTV"])
    assert _matches(e, "paul")
    assert _matches(e, "armada ctv")          # both words, either order
    assert _matches(e, "53174")
    assert not _matches(e, "armada meta")


def test_the_audit_says_why_a_row_is_not_on_the_board():
    """"Not on the board" is where the question starts. The useful answer is
    which of the dozen reasons this cycle has for not owing a report applies."""
    import datetime as dt

    import app.board as B
    from app.audit import audit
    from app.db import OrderLine

    class FakeDb:
        def __init__(self, lines):
            self._lines = lines

        def scalars(self, *a, **k):
            outer = self

            class R:
                def all(self):
                    return outer._lines
            return R()

    real = B.expected_for
    B.expected_for = lambda db, period, skipped=None: (
        skipped.append({"market": "7 Mountains PA Altoona",
                        "client": "Sorge Funeral Home", "kind": "lifetime",
                        "why": "lifetime already delivered in 2026-06"})
        or [])
    try:
        got = audit(FakeDb([]), "2026-07",
                    "7MOU ALT - Sorge Funeral Home #45911 LIFETIME\n"
                    "7MOU ALT - Nobody At All #99999")
    finally:
        B.expected_for = real

    why = {m["client"]: m["why"] for m in got["missing"]}
    assert why["Sorge Funeral Home"] == "lifetime already delivered in 2026-06"
    assert "not in the export" in why["Nobody At All"]


def test_the_audit_reads_the_order_line_when_the_board_has_no_reason():
    import datetime as dt

    import app.board as B
    from app.audit import audit

    class L:
        client, product = "Carl Feather Homes", "Display"
        account_ids, line_ids = "49822", "1"
        starts_on = dt.date(2024, 1, 1)
        ends_on = order_ends_on = dt.date(2026, 5, 31)
        order_starts_on, flights, live, canceled = starts_on, None, True, False

    class FakeDb:
        def scalars(self, *a, **k):
            class R:
                def all(self):
                    return [L()]
            return R()

    real = B.expected_for
    B.expected_for = lambda db, period, skipped=None: []
    try:
        got = audit(FakeDb(), "2026-07", "7MOU PA - Carl Feather Homes #49822")
    finally:
        B.expected_for = real
    assert "ended by 2026-05-31" in got["missing"][0]["why"]


def test_the_comparison_limits_itself_to_the_partners_the_list_covers():
    """A list covering one partner compared against the whole board reported
    the other 145 partners as "missing from your list" - 1,050 rows of noise
    around the handful that matter."""
    import app.board as B
    from app.audit import audit
    from app.board import Expected

    rows = [
        Expected(market="7 Mountains PA Selinsgrove", group="7 Mountains",
                 client="Salem RV", kind="monthly", account_ids="52793"),
        Expected(market="7 Mountains PA Altoona", group="7 Mountains",
                 client="Reliance Bank", kind="monthly", account_ids="43850"),
        Expected(market="ADX Communications", group="ADX",
                 client="Armada Advisors", kind="monthly", account_ids="53174"),
    ]
    real = B.expected_for
    B.expected_for = lambda db, period, skipped=None: rows
    try:
        got = audit(None, "2026-07", "7MOU SG - Salem RV #52793")
    finally:
        B.expected_for = real

    assert got["covered"] == ["7 Mountains"]
    # Reliance Bank is a 7 Mountains row the list has not got - worth saying.
    # Armada Advisors is another partner entirely - not this list's business.
    assert [e.client for e in got["extra"]] == ["Reliance Bank"]


def test_with_nothing_matched_the_whole_cycle_is_still_shown():
    import app.board as B
    from app.audit import audit
    from app.board import Expected

    rows = [Expected(market="ADX Communications", group="ADX",
                     client="Armada Advisors", kind="monthly", account_ids="53174")]
    real = B.expected_for
    B.expected_for = lambda db, period, skipped=None: rows
    try:
        got = audit(None, "2026-07", "7MOU SG - Nobody #11111")
    finally:
        B.expected_for = real
    assert got["covered"] == [] and len(got["extra"]) == 1


def test_a_note_after_the_lifetime_marker_is_not_part_of_the_client_name():
    from app.audit import parse_list
    got = parse_list("7MOU ALT - Sorge Funeral Home & Crematory #45911 "
                     "LIFETIME -End Date 2026-12-31")
    assert got[0]["client"] == "Sorge Funeral Home & Crematory"
    assert got[0]["kind"] == "lifetime"


def test_signed_off_reports_are_a_filter_not_a_second_table():
    """A report used to change which table it was in the moment somebody signed
    it, so finding it again meant knowing that it had moved. One grid, with the
    finished work filtered out by default."""
    html = (TPL / "cycle.html").read_text()
    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert 'id="tbl-done"' not in html, "the second table is back"
    # Three buckets, and Pending is the default because that is the job.
    assert 'class="buckets"' in html
    assert "'pending', 'Pending'" in html
    assert "'completed', 'Completed'" in html
    assert 'bucket = done_ if done_ in {"pending", "completed", "all"}' in src
    assert 'if bucket == "pending":' in src


def test_the_reports_table_pages_fifty_at_a_time():
    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert "PAGE_SIZE = 50" in src
    assert "rows[(page - 1) * PAGE_SIZE:page * PAGE_SIZE]" in src
    html = (TPL / "cycle.html").read_text()
    assert "{% macro pager() %}" in html


def test_an_order_id_on_the_board_links_to_the_order():
    """The board says a campaign is owed a report. The next question is always
    what the order says, and that meant copying the number out by hand."""
    from app.config import settings
    assert settings.io_order_url.endswith("viewOrder/")
    html = (TPL / "cycle.html").read_text()
    assert "{{ io_order_url }}{{ oid }}" in html
    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert '"io_order_url": settings.io_order_url' in src


def test_a_broken_pipe_mid_upload_is_retried():
    """It killed a whole partner's run, and the same button pressed a third
    time worked - nothing was wrong with the report, the connection went away."""
    import inspect
    from app import delivery
    src = inspect.getsource(delivery.upload_drive_folder)
    assert "num_retries=4" in src
    assert "RETRY_UPLOAD" in src
    assert BrokenPipeError in delivery.RETRY_UPLOAD


def test_a_packaged_partner_says_when_its_folder_is_behind(tmp_path):
    """Reports get corrected all cycle and the folder only changes when
    somebody presses sync, so a partner can be handing out a perfectly good
    link to last Tuesday's work with nothing saying so."""
    from app.board import Expected, GroupRow
    from app.db import Report
    from app.delivery import file_stamp, out_of_sync

    pdfs = {}
    for who in ("acme", "beta", "gamma", "delta"):
        f = tmp_path / f"{who}.pdf"
        f.write_bytes(b"%PDF-1.4\n")
        pdfs[who] = str(f)

    # Filed under the name it still has, from the file it was filed from.
    filed = Report(filename="July 2026_Acme 123.pdf", stored_path=pdfs["acme"],
                   delivered_as="July 2026_Acme 123.pdf",
                   delivered_stamp=file_stamp(pdfs["acme"]), client="Acme")
    # Renamed since it was filed.
    moved = Report(filename="July 2026_Beta 999.pdf", stored_path=pdfs["beta"],
                   delivered_as="July 2026_Beta 111.pdf",
                   delivered_stamp=file_stamp(pdfs["beta"]), client="Beta")
    # Never filed at all.
    never = Report(filename="July 2026_Gamma 7.pdf", stored_path=pdfs["gamma"],
                   delivered_as="", client="Gamma",
                   severity="pass", findings=[], review_state="reviewed")
    # Same name, different file - somebody replaced the PDF after it went up.
    swapped = Report(filename="July 2026_Delta 5.pdf", stored_path=pdfs["delta"],
                     delivered_as="July 2026_Delta 5.pdf",
                     delivered_stamp="1:1", client="Delta")
    g = GroupRow("P", "drive", [
        Expected(market="M", group="P", client="Acme", kind="monthly", report=filed),
        Expected(market="M", group="P", client="Beta", kind="monthly", report=moved),
        Expected(market="M", group="P", client="Gamma", kind="monthly", report=never),
        Expected(market="M", group="P", client="Delta", kind="monthly", report=swapped),
        # No PDF behind it: nothing was ever owed to the folder for this one.
        Expected(market="M", group="P", client="Echo", kind="monthly"),
    ])
    assert out_of_sync(g) == ["Beta", "Gamma", "Delta"]


def test_a_sync_only_sends_what_actually_moved():
    """Re-uploading every report every time takes several minutes on a big
    partner to change nothing, and nine megabytes a file is all of that time."""
    import inspect
    from app import delivery
    for fn in (delivery.upload_drive_folder, delivery.upload_dropbox_folder):
        src = inspect.getsource(fn)
        assert "needs_send(e)" in src, fn.__name__
        assert "skipped += 1" in src, fn.__name__
        # And it still hands back the link when nothing needed sending.
        assert "Already up to date" in src, fn.__name__


def test_the_tooltips_are_the_page_s_own_not_the_operating_system_s():
    """The browser's title= takes about a second and a half and is drawn by the
    OS, so it is both slow and the only thing on the page that does not look
    like the page."""
    base = (TPL / "base.html").read_text()
    # One floating element, positioned in script. Drawn on the element itself
    # the report table clipped it - that table scrolls sideways, and anything
    # that scrolls sideways cuts off whatever leaves it.
    assert 'id="tip"' in base
    assert "getAttribute('data-tip')" in base
    assert "[data-tip]::after" not in base, "back to a clippable tooltip"
    html = (TPL / "cycle.html").read_text()
    # The sign-off row is the one that gets pointed at all day.
    assert 'data-tip="Done, no report' in html
    assert 'data-tip="Not needed' in html
    assert 'data-tip="Reviewed' in html


def test_the_board_says_when_the_automatic_pull_has_stopped():
    """A cycle arrives in a flood and then it stops, and what is left is the
    lifetimes, pulled by hand one at a time. Projecting "about 25 hours to go"
    off a trickle of four an hour reads as a schedule when it is really a note
    that nothing is coming on its own any more."""
    import datetime as dt
    from app.pace import STALLED_RATE, pace

    now = dt.datetime(2026, 8, 26, 12, 0)

    class FakeDb:
        pass

    import app.pace as P
    real = P.arrivals
    # A flood two days ago, a trickle since.
    flood = [now - dt.timedelta(hours=48) + dt.timedelta(minutes=i)
             for i in range(0, 600, 2)]
    trickle = [now - dt.timedelta(hours=h) for h in (2.5, 1.5, 0.5)]
    try:
        P.arrivals = lambda db, period: sorted(flood + trickle)
        out = pace(FakeDb(), "2026-07", 105, now=now)
        assert out["stalled"] is True
        # And a cycle in full flood is not called stalled.
        P.arrivals = lambda db, period: sorted(
            [now - dt.timedelta(minutes=i) for i in range(0, 180, 1)])
        assert pace(FakeDb(), "2026-07", 105, now=now)["stalled"] is False
    finally:
        P.arrivals = real
    assert STALLED_RATE == 20.0


def test_the_partner_cards_page_and_the_search_reaches_past_the_page():
    """A hundred and fifty cards is four screens of scrolling before the
    reports table, which is where the work happens. A search that only looked
    at the twenty on screen would be worse than no search at all."""
    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert "CARD_PAGE = 20" in src
    assert "shown_groups = groups[(cards - 1) * CARD_PAGE:cards * CARD_PAGE]" in src
    # The query narrows the partners FIRST, then the page applies to what is left.
    i, j = src.index("hit = [g for g in groups"), src.index("card_total = len(groups)")
    assert i < j, "the page is applied before the search"
    html = (TPL / "cycle.html").read_text()
    assert "{% macro cardpager() %}" in html


def test_a_partner_can_be_packaged_with_only_what_is_signed_off():
    """A partner is not all or nothing. Two thirds of it can be signed off
    while somebody works through the last dozen, and holding the first thirty
    back is a week of nobody having anything."""
    import inspect
    from app import delivery
    src = inspect.getsource(delivery.deliver)
    assert "ready_only" in src
    assert "keep = [e for e in group.expected if e.ready]" in src
    html = (TPL / "links.html").read_text()
    assert 'name="ready_only" value="1"' in html


def test_the_site_can_be_put_behind_one_shared_password(tmp_path, monkeypatch):
    """Blank leaves it open, which is what it has been. This is an internal
    tool behind a link nobody outside has, so what is worth stopping is a
    forwarded link, not a colleague."""
    import importlib, os
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'g.db'}")
    monkeypatch.setenv("SITE_PASSWORD", "hunter2")
    import app.config, app.db, app.main
    for m in (app.config, app.db, app.main):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    app.db.Base.metadata.create_all(app.db.engine)
    c = TestClient(app.main.app)

    assert c.get("/cycle").status_code == 401
    # Whatever the health checker needs stays open, or Render marks it down.
    assert c.get("/healthz").status_code == 200
    assert c.post("/unlock", data={"password": "wrong"}).status_code == 401
    r = c.post("/unlock", data={"password": "hunter2", "next": "/cycle"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/cycle"
    assert c.get("/cycle").status_code == 200

    # The cookie carries a hash, not the password - a cookie is readable by
    # anybody with the laptop.
    assert "hunter2" not in str(c.cookies.get("qa_pass") or "")

    # And an open redirect is not a way out of it.
    r = c.post("/unlock", data={"password": "hunter2", "next": "//evil.example"},
               follow_redirects=False)
    assert r.headers["location"] == "/"

    monkeypatch.delenv("SITE_PASSWORD")
    for m in (app.config, app.db, app.main):
        importlib.reload(m)


def _render_every_page(tmp_path, monkeypatch):
    import datetime as dt
    import importlib
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'r.db'}")
    import app.config, app.db, app.main
    for m in (app.config, app.db, app.main):
        importlib.reload(m)
    from fastapi.testclient import TestClient
    app.db.Base.metadata.create_all(app.db.engine)
    db = app.db.SessionLocal()
    D = dt.date.fromisoformat
    db.add(app.db.Batch(email_subject="x", received_at=dt.datetime(2026, 8, 1)))
    db.add(app.db.Partner(partner="P", group="P", delivery_target="dropbox"))
    db.add(app.db.OrderLine(
        market="P", client="C", account_ids="10", line_ids="1", product="CTV",
        campaign="Connected TV Ads", live=True, status="IO Live",
        starts_on=D("2026-01-01"), ends_on=D("2026-12-31"),
        order_starts_on=D("2026-01-01"), order_ends_on=D("2026-12-31")))
    db.commit()
    db.add(app.db.Report(batch_id=1, filename="July 2026_C 10.pdf", client="C",
                         market="P", period="2026-07", severity="fail",
                         review_state="new", checks=[],
                         findings=[{"code": "x", "severity": "fail",
                                    "title": "t", "detail": "d"}]))
    db.add(app.db.Delivery(period="2026-07", group="P", target="dropbox",
                           ok=True, reports=1, share_url="https://d/x"))
    db.commit()
    db.close()
    c = TestClient(app.main.app)
    pages = ["/", "/cycle?period=2026-07", "/cycle?period=2026-07&done=all",
             "/cycle/links?period=2026-07", "/orders", "/partners", "/people",
             "/rules", "/lifetimes", "/cycle/audit?period=2026-07"]
    return c, pages


def test_every_page_closes_the_tags_it_opens(tmp_path, monkeypatch):
    """AN OPEN BRACE FOLLOWED STRAIGHT BY A HASH STARTS A TEMPLATE COMMENT.

    A media query wrapping a hash-id selector - `){#tip{` with no space - ate
    the rest of the stylesheet, the tag that closes it, and every tag after
    that. The whole site rendered as one line of footer text on a blank page,
    and every test passed, because they all check status codes and substrings
    and none of them had ever looked at the shape of the document.
    """
    import re
    c, pages = _render_every_page(tmp_path, monkeypatch)
    for url in pages:
        r = c.get(url)
        assert r.status_code in (200, 303), f"{url} -> {r.status_code}"
        t = r.text
        if "text/html" not in r.headers.get("content-type", ""):
            continue
        # And the page actually got as far as the end. Checked before the
        # scripts are stripped, since that is the whole document.
        assert t.rstrip().endswith("</html>"), f"{url}: truncated"
        opened = len(re.findall(r"<script[\s>]", t))
        assert opened == t.count("</script>"), f"{url}: <script> unbalanced"
        # Prose inside a script talking ABOUT a tag is not a tag.
        body = re.sub(r"<script\b.*?</script>", "", t, flags=re.S)
        for tag in ("style", "head", "body", "html", "main",
                    "table", "form", "select", "textarea"):
            n = len(re.findall(rf"<{tag}[\s>]", body))
            closed = body.count(f"</{tag}>")
            assert n == closed, f"{url}: <{tag}> {n} open, {closed} closed"


def test_no_template_has_an_accidental_comment_opener():
    """The same bug, caught at the source rather than in the output."""
    import re
    for f in sorted(TPL.glob("*.html")):
        src = f.read_text()
        # A brace-hash that is not a real comment, i.e. one with no closer.
        for m in re.finditer(r"\{#", src):
            rest = src[m.start():]
            assert "#}" in rest, f"{f.name}: unterminated template comment"
        # The CSS shape that caused it: an open brace immediately before an id.
        assert not re.search(r"\)\{#[A-Za-z]", src), (
            f"{f.name}: `){{#` in CSS - put a space after the brace")


def _grp(*specs):
    """(client, ready, delivered_as) -> a GroupRow of reports."""
    from app.board import Expected, GroupRow
    from app.db import Report
    rows = []
    for client, ready, filed in specs:
        rep = Report(filename=f"July 2026_{client}.pdf", stored_path="/x.pdf",
                     client=client, delivered_as=filed,
                     severity="pass", findings=[],
                     review_state="reviewed" if ready else "new")
        rows.append(Expected(market="M", group="P", client=client,
                             kind="monthly", report=rep))
    return GroupRow("P", "drive", rows)


def test_a_partner_part_way_through_can_send_what_is_finished():
    """Some July reports are still being fixed and some partners only need one
    or two. The finished ones should be able to go without waiting for - or
    deleting - the rest."""
    import inspect
    from app import delivery, main
    src = inspect.getsource(main.cycle_links)
    # It used to be "ready AND not packaged", so a partner two reports into
    # twenty was on neither list.
    assert "if g.group in packaged:" in src
    assert "ready_n = sum(1 for e in g.expected if e.ready)" in src
    html = (TPL / "links.html").read_text()
    assert "Send the {{ w.ready }} good to go" in html
    # And the delivery itself only takes the signed-off ones.
    assert "keep = [e for e in group.expected if e.ready]" in \
        inspect.getsource(delivery.deliver)


def test_an_unfinished_report_is_not_out_of_sync():
    """Sending only the finished ones is deliberate. Counting the rest as
    "changed since this was packaged" turns the flag on and leaves it on for
    the whole cycle, which is the fastest way to teach somebody to ignore it."""
    from app.delivery import out_of_sync
    # signed off, never filed  -> the folder is missing it
    # still open, never filed  -> it was never going to be in there
    # filed, since renamed     -> the folder has the wrong one
    g = _grp(("Done", True, ""), ("Open", False, ""),
             ("Renamed", True, "July 2026_Renamed 111.pdf"))
    assert out_of_sync(g) == ["Done", "Renamed"]


def test_the_partner_search_reaches_past_the_page_it_is_on():
    """With twenty cards a page, a box that only filtered what was on screen
    said "0 of 20 partners" for a partner on page five - which reads as the
    partner not existing."""
    html = (TPL / "cycle.html").read_text()
    # It is a form that goes to the server, not only a client-side filter.
    i = html.index('data-cards="glist"')
    before = html[:i]
    assert before.rindex('<form method="get" action="/cycle"') > before.rindex('</form>')
    assert 'name="q"' in html[i - 400:i + 200]
    assert "Enter to search all {{ card_total }}" in html


def test_the_filter_dropdowns_offer_the_whole_cycle():
    """Built from the cards on screen, the Partner filter offered the twenty
    this page happens to show and called it "All (20)"."""
    src = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert "def _card_options(groups)" in src
    # Built from EVERY group, not from the ones that survived the filter -
    # offering only what is still showing means one pick and the menu can
    # never take you anywhere else.
    assert '"opts": _card_options(every_group)' in src
    html = (TPL / "cycle.html").read_text()
    for key in ("partner", "buyer", "reporter", "trainer", "status"):
        assert f'data-opts-{key}="{{{{ opts.{key} }}}}"' in html
    base = (TPL / "base.html").read_text()
    assert "grid.dataset[uk]" in base
    # And a list of 145 needs a way to find one.
    assert "multifind" in base


def test_card_options_are_the_whole_cycles_values():
    from app.board import Expected, GroupRow
    from app.main import _card_options
    groups = [
        GroupRow("Alpha", "drive",
                 [Expected(market="M", group="Alpha", client="a", kind="monthly")],
                 buyer="Bella", reporter="Paulina", trainer="Katie"),
        GroupRow("Lockwood Media", "dropbox",
                 [Expected(market="M", group="Lockwood Media", client="b",
                           kind="monthly")],
                 buyer="Stacy", reporter="Taylor", trainer="Katie"),
    ]
    opts = _card_options(groups)
    assert opts["partner"] == "Alpha|Lockwood Media"
    assert opts["buyer"] == "Bella|Stacy"
    assert opts["trainer"] == "Katie"          # deduplicated
    # THE TWO VALUES A CARD ACTUALLY CARRIES. This offered every report state -
    # Not received, Errors, In review - and a card is labelled "Good to go" or
    # "Open", so picking any of them matched no card and the board went empty.
    assert opts["status"] == "Open"


def test_the_not_owed_list_sits_with_the_reports():
    """It is a statement about which reports this cycle wants and which it does
    not, so it belongs under the reports rather than wedged between the tiles
    and the partner cards."""
    html = (TPL / "cycle.html").read_text()
    assert html.index('<section id="reports">') < html.index('class="notowed"')
    assert html.index('class="glist" id="glist"') < html.index('class="notowed"')


def test_the_order_status_sits_with_the_order_it_is_about():
    """It sat under the review pill two columns over, where it read as part of
    the verdict rather than as the fact behind it - and as words, on a row
    that has none to spare. Green live, orange paused, red cancelled, blue
    complete, and the words themselves on hover."""
    from app.main import _io_kind
    assert _io_kind("IO Live") == "live"
    assert _io_kind("IO Paused") == "paused"
    assert _io_kind("Cancelled") == "cancelled"
    assert _io_kind("IO Cancelled") == "cancelled"
    assert _io_kind("IO Complete") == "complete"
    assert _io_kind("") == "other"

    html = (TPL / "cycle.html").read_text()
    # EACH ORDER IS ITS OWN PILL. A row of dots beside a grey block of ids
    # said two orders and two statuses and nothing about which was which.
    assert 'class="oidp io-{{ st|iokind }}"' in html
    assert "e.order_status.get(oid, '')" in html
    assert 'class="acct iostatus"' not in html
    assert "iodot" not in html
    for kind in ("live", "paused", "cancelled", "complete"):
        assert f".oidp.io-{kind}{{background:" in html


def test_one_order_with_two_statuses_takes_the_living_one():
    """An order with a live line item and a cancelled one is a live order, and
    marking it dead is the kind of wrong that gets a report pulled to the
    wrong end date."""
    from app.board import _status_rank
    assert _status_rank("IO Live") < _status_rank("Cancelled")
    assert _status_rank("IO Live") < _status_rank("IO Complete")
    assert _status_rank("IO Paused") < _status_rank("IO Complete")
    assert _status_rank("") > _status_rank("Cancelled")


def test_a_cancelled_order_dated_to_the_future_is_off_the_pull_list():
    """Nothing on the export says when somebody hit cancel, so a campaign
    called off in 2021 can still be dated to 2027 - and "ends after the cutoff"
    kept it on the list forever. Whitefield Media has no live order at all and
    was asking for a pull back to 23 March 2020 on the strength of one."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, OrderLine
    from app.main import pull_range_rows, pull_range_why

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    D = dt.date.fromisoformat
    db.add(OrderLine(market="Whitefield Media", client="Old Client",
                     account_ids="1", line_ids="10", product="Display",
                     starts_on=D("2020-03-23"), ends_on=D("2021-06-30"),
                     order_starts_on=D("2020-03-23"), order_ends_on=D("2027-12-31"),
                     canceled=True, live=False, status="Cancelled"))
    db.commit()

    assert [m for m, _e, _n in pull_range_rows(db, today=D("2026-08-26"))] == []
    # And the list says why, so the whole thing can be audited.
    why = pull_range_why(db, "Whitefield Media", today=D("2026-08-26"))
    assert len(why) == 1 and why[0]["kept"] is False
    assert "has been running since" in why[0]["why"]
    db.close(); eng.dispose()


def test_a_cancelled_order_that_stopped_last_month_still_needs_its_pull():
    """It finished recently enough to still owe a lifetime, and that report
    covers the whole campaign."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, OrderLine
    from app.main import pull_range_rows

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    D = dt.date.fromisoformat
    db.add(OrderLine(market="Recently Stopped", client="C", account_ids="2",
                     product="Display", starts_on=D("2019-01-01"),
                     ends_on=D("2026-07-20"), order_starts_on=D("2019-01-01"),
                     order_ends_on=D("2027-12-31"), canceled=True, live=False))
    db.commit()
    got = pull_range_rows(db, today=D("2026-08-26"))
    assert got and got[0][0] == "Recently Stopped"
    assert got[0][1] == D("2019-01-01")
    db.close(); eng.dispose()


def test_a_client_that_served_with_no_order_line_is_named(client_orders_db):
    """"A partner with no orders" was the first attempt and it cried wolf at
    125 of 158, because most of them simply have nothing running.

    The serving file is the evidence. A client that DELIVERED this month and
    has no order line at all cannot be a campaign that went dark or a spelling
    the two tools disagree on - something was running and there is nothing here
    to judge it against. That is what a partner's export failing to land looks
    like.
    """
    c, db, dbm = client_orders_db
    db.add(dbm.OrderLine(market="Has Orders", client="Alpha", account_ids="1",
                         product="Display", starts_on=dt.date(2026, 1, 1),
                         ends_on=dt.date(2026, 12, 31)))
    # Delivered, and the order list knows about it.
    db.add(dbm.ServedDays(period="2026-07", market_key="hasorders",
                          client_key="alpha", market="Has Orders",
                          client="Alpha", days=31))
    # Delivered, and the order list has never heard of it.
    db.add(dbm.ServedDays(period="2026-07", market_key="nofilelanded",
                          client_key="beta", market="No File Landed",
                          client="Beta", days=28))
    # Delivered once, which is as likely to be a stray row as a campaign.
    db.add(dbm.ServedDays(period="2026-07", market_key="oneday",
                          client_key="gamma", market="One Day",
                          client="Gamma", days=1))
    db.commit()

    html = c.get("/orders").text
    assert "delivered impressions in July 2026" in html
    # The panel names only the one with nothing to judge it against. Alpha and
    # Gamma appear elsewhere on the page, so the panel itself is what is read.
    panel = html[html.index("delivered impressions in July 2026"):]
    panel = panel[:panel.index("</div>")]
    assert "No File Landed" in panel and "Beta" in panel
    assert "Alpha" not in panel, "it has an order line"
    assert "Gamma" not in panel, "one day is not a campaign"
    assert "no orders loaded for this partner at all" in panel


def test_the_order_tool_puts_the_product_in_the_client_name(client_orders_db):
    """The serving file says "A-1 Appliance"; the IO tool has that same client
    as "A-1 Appliance - Display", because one client running two products is
    two client records there. Keyed strictly they are two different clients,
    and A-1 came back as a client that served for thirty days with no order
    behind it - on a partner whose file had landed perfectly well."""
    c, db, dbm = client_orders_db
    db.add(dbm.OrderLine(market="Results Media Solutions Yuba-Marysville",
                         client="A-1 Appliance - Display", account_ids="52201",
                         product="Display", starts_on=dt.date(2026, 2, 10),
                         ends_on=dt.date(2026, 12, 31)))
    db.add(dbm.ServedDays(period="2026-07",
                          market_key="resultsmediasolutionsyubamarysville",
                          client_key="a1appliance",
                          market="Results Media Solutions Yuba-Marysville",
                          client="A-1 Appliance", days=30))
    db.commit()
    html = c.get("/orders").text
    assert "delivered impressions in July 2026" not in html


@pytest.fixture()
def client_orders_db(tmp_path, monkeypatch):
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'o.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTO_RECHECK", "false")
    # The board these tests build is July's, whatever cycle the tool is pinned
    # to today - otherwise every one of them has to be rewritten each month.
    monkeypatch.setenv("DEFAULT_PERIOD", "2026-07")
    from app import config as cfg
    importlib.reload(cfg)
    from app import db as dbm
    importlib.reload(dbm)
    dbm.init_db()
    from app import main as mmod
    importlib.reload(mmod)
    db = dbm.SessionLocal()
    yield TestClient(mmod.app), db, dbm
    db.close()
    monkeypatch.undo()
    importlib.reload(cfg); importlib.reload(dbm); importlib.reload(mmod)


def test_each_straggler_carries_the_line_item_that_set_its_date():
    """"Manning Media, 2018-03-21" and "Whitfield Media, 2020-03-23" are claims
    about particular orders, and the only way to judge one is to see it."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, OrderLine
    from app.main import _strategy_with_reasons

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    D = dt.date.fromisoformat
    db.add(OrderLine(market="Manning Media", client="An Old Client",
                     account_ids="900", line_ids="9000", product="Display",
                     status="IO Live", starts_on=D("2018-03-21"),
                     ends_on=D("2027-12-31"), order_starts_on=D("2018-03-21"),
                     order_ends_on=D("2027-12-31"), live=True))
    # And one on the same partner that is NOT what put it there.
    db.add(OrderLine(market="Manning Media", client="Gone", account_ids="901",
                     product="Display", status="Cancelled", canceled=True,
                     live=False, starts_on=D("2019-01-01"), ends_on=D("2019-06-30"),
                     order_starts_on=D("2019-01-01"), order_ends_on=D("2027-12-31")))
    db.commit()

    st = _strategy_with_reasons(db)
    mine = [s for s in st["stragglers"] if s["market"] == "Manning Media"]
    assert mine, "the old order should still need its own pull"
    why = mine[0]["why"]
    assert [w["orders"] for w in why] == ["900"]
    assert why[0]["why"] == "still running"
    assert mine[0]["dropped"] == 1, "the cancelled one is named as not counted"
    db.close(); eng.dispose()


def test_the_tiles_are_not_added_up_from_one_page_of_cards():
    """Twenty cards on screen out of a hundred and forty-five made "Expected"
    read 137 against a cycle of over a thousand - the sum of the page rather
    than the number the server sent. The server's figures already cover the
    whole cycle and whatever filter is on it."""
    cycle = (TPL / "cycle.html").read_text()
    base = (TPL / "base.html").read_text()
    assert '{% if card_pages > 1 %}data-paged="1"{% endif %}' in cycle
    assert "if ('paged' in tiles.dataset) return;" in base
    # And it is the first thing retotal does, before any summing.
    i = base.index("var retotal = function")
    j = base.index("if ('paged' in tiles.dataset) return;")
    k = base.index("var sum = {", i)
    assert i < j < k


def test_the_pull_state_is_an_icon_with_the_words_on_hover():
    """"auto-pull has stopped" and "about 25 hours to go" are a lot of words
    for one bit, in the one place on the page with no room for words."""
    cycle = (TPL / "cycle.html").read_text()
    assert 'class="pulse stopped"' in cycle and 'class="pulse running"' in cycle
    assert "The automatic pull has stopped." in cycle
    assert "The pull is running - about" in cycle
    # It still says so to a screen reader, and it does not blink at somebody
    # who asked things not to.
    assert 'aria-label="The automatic pull has stopped"' in cycle
    assert "@media (prefers-reduced-motion:reduce){ .pulse.running{animation:none} }" in cycle


def test_the_two_tools_spelling_a_client_differently_is_an_alert(client_orders_db):
    """This started as a mild note saying "worth ruling out". The first one it
    found - A-1 Appliance - was exactly the thing it said to rule out: the
    delivery data was attached to the plain client record and the order to the
    "- Display" one. So it warns, and it says what to go and check."""
    c, db, dbm = client_orders_db
    db.add(dbm.OrderLine(market="Results Media Solutions Yuba-Marysville",
                         client="A-1 Appliance - Display", account_ids="52201",
                         product="Display", starts_on=dt.date(2026, 2, 10),
                         ends_on=dt.date(2026, 12, 31)))
    db.add(dbm.ServedDays(period="2026-07",
                          market_key="resultsmediasolutionsyubamarysville",
                          client_key="a1appliance",
                          market="Results Media Solutions Yuba-Marysville",
                          client="A-1 Appliance", days=30))
    db.commit()
    html = c.get("/orders").text
    assert "linked to the wrong client record" in html
    assert "A-1 Appliance - Display" in html
    # It reads as a warning, not as a note in passing.
    assert "border-left-color:var(--gold)" in html
    # And it is NOT in the missing panel, because it is not missing.
    assert "delivered impressions in July 2026" not in html


def test_a_client_the_two_tools_agree_on_is_not_flagged():
    """The alert has to stay quiet on the ordinary case, or it is the "125
    partners have no orders" panel again."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, OrderLine, ServedDays
    from app.serving import matched_on_base_name

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(OrderLine(market="Partner", client="A-1 Appliance", account_ids="1",
                     product="Display", starts_on=dt.date(2026, 1, 1),
                     ends_on=dt.date(2026, 12, 31)))
    db.add(ServedDays(period="2026-07", market_key="partner",
                      client_key="a1appliance", market="Partner",
                      client="A-1 Appliance", days=30))
    db.commit()
    assert matched_on_base_name(db, "2026-07") == []
    db.close(); eng.dispose()


def test_a_month_is_written_the_way_people_say_it():
    """"2026-07" is how the period is stored, because it sorts. It is not how
    anybody says it, and it was on the screen beside a board heading reading
    "July 2026 reports" - the same month under two names, one page."""
    from app.cycle import month_label
    assert month_label("2026-07") == "July 2026"
    assert month_label("2026-01") == "January 2026"
    assert month_label("2026-12") == "December 2026"
    # Anything that is not a period comes back untouched, so it is safe on a
    # column that might be blank or hold something else.
    assert month_label("") == ""
    assert month_label(None) == ""
    assert month_label("Lifetime") == "Lifetime"
    assert month_label("2026-13") == "2026-13"
    assert month_label("2026-7") == "2026-7"


def test_the_hyphenated_period_is_not_printed_at_a_person():
    """The places it leaked: the cycle dropdown, the viewer head, the order
    panel, the report's own order list. A URL is not a person."""
    import re as _re
    for name in ("cycle.html", "links.html", "viewer.html", "orders.html",
                 "report_orders_body.html", "batch.html", "dashboard.html",
                 "lifetimes.html"):
        html = (TPL / name).read_text()
        for m in _re.finditer(r"\{\{[^}]*?\bperiod\b[^}]*?\}\}", html):
            frag = m.group(0)
            if "|month" in frag or "urlencode" in frag:
                continue
            # A period inside a URL or a form value is machinery, not
            # writing. Nor is the placeholder in the box you type one INTO -
            # that is showing the format, and "e.g. August 2026" would be
            # showing the wrong one.
            before = html[max(0, m.start() - 40):m.start()]
            assert ("period=" in before or "/cycle/" in before
                    or 'value="' in before or "placeholder=" in before), \
                f"{name}: {frag} is shown raw"


def test_needs_fix_does_not_promise_to_re_pull_anything():
    """"Needs fix - send it back to be re-pulled" read as the tool doing the
    re-pulling. It does not send anything anywhere. All it does is mark the
    row, which is what keeps the partner from being packaged with it."""
    for name in ("cycle.html", "viewer.html"):
        html = (TPL / name).read_text()
        assert "send it back to be re-pulled" not in html
    cycle = (TPL / "cycle.html").read_text()
    assert "It needs to be re-pulled and uploaded." in cycle
    assert "It needs to be re-pulled and uploaded." in (TPL / "viewer.html").read_text()


def test_a_blank_order_status_says_the_export_left_it_blank():
    """"Order 51903 - the order does not say" reads as the tool being coy
    about something it knows. The order tool is not what is silent - the
    export's status column is empty for that order."""
    cycle = (TPL / "cycle.html").read_text()
    assert "the order does not say" not in cycle
    assert "no status on it in the order export" in cycle


def test_the_order_lines_link_is_a_button():
    """It sat under a panel of small grey print as a sentence with a link in
    it, so the one control that answers "what is this finding looking at" read
    as a footnote. It still opens in the side sheet, and the href still works
    for a middle-click."""
    v = (TPL / "viewer.html").read_text()
    assert ">View order lines</a>" in v
    assert "Order lines as stored</a> - what a" not in v
    i = v.index(">View order lines</a>")
    tag = v[v.rindex("<a ", 0, i):i]
    assert 'class="mini"' in tag
    assert "data-sheet=" in tag and 'href="/report/{{ rep.id }}/orders"' in tag


def test_a_finished_order_on_a_live_row_still_gets_its_colour():
    """Order 51903 came up grey, tooltipped "no status on it in the order
    export" - on an export that carries a status on every row it ships.

    The status was read inside the loop that builds the expected rows, and
    that loop skips a line which is neither live nor owed a lifetime. But a
    finished order overlapping the campaign still gets its id onto the row,
    because the row is about the whole campaign. Pill present, status absent.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, OrderLine
    from app.board import expected_for

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    D = dt.date.fromisoformat

    def line(order, product, start, end, status, live, canceled=False,
             complete=False):
        return OrderLine(
            market="Digital Marketing LLC", client="ASI Insulation & Abatement",
            account_ids=order, line_ids=order + "1", product=product,
            status=status, live=live, canceled=canceled, complete=complete,
            starts_on=D(start), ends_on=D(end),
            order_starts_on=D(start), order_ends_on=D(end),
            detail=[{"order": order, "line": order + "1", "status": status,
                     "starts": start, "ends": end, "live": live,
                     "canceled": canceled, "complete": complete}])

    # Still running, so this is the line that earns the row.
    db.add(line("51999", "Display", "2026-01-01", "2026-12-31", "IO Live", True))
    # Finished in the spring. Overlaps the campaign, so its id lands on the
    # row - and it is exactly the line the expected-row loop skips.
    db.add(line("51903", "Video", "2025-06-01", "2026-04-30", "IO Complete",
                False, complete=True))
    db.commit()

    rows = [e for e in expected_for(db, "2026-07")
            if e.client == "ASI Insulation & Abatement"]
    assert rows, "the live order should still put this client on the board"
    for e in rows:
        # Every pill the row prints has a colour behind it.
        for oid in e.account_ids.split():
            assert e.order_status.get(oid), f"{oid} came back grey"
        assert e.order_status["51999"] == "IO Live"
        if "51903" in e.account_ids:
            assert e.order_status["51903"] == "IO Complete"
    life = [e for e in rows if e.kind == "lifetime"]
    if life:
        assert "51903" in life[0].account_ids, \
            "the finished order belongs on the campaign's row"
    db.close(); eng.dispose()


def test_the_logo_buttons_say_what_they_do_to_the_logo():
    """"This is the tool's default logo" is a statement about the picture, and
    the line above it was an instruction to whoever was reading. Both hid the
    part that matters: the mark reaches every other report carrying it."""
    v = (TPL / "viewer.html").read_text()
    assert ">Flag as default logo</button>" in v
    assert ">Flag as real logo</button>" in v
    assert "This is the tool's default logo" not in v
    assert "It is a real logo</button>" not in v
    assert "Logo being used on the report." in v
    assert ("Flagging as a different status will force all other reports using"
            in v)


def test_the_sync_flag_says_why_not_just_how_many():
    """"26 reports changed since this was packaged", on a partner whose 26
    reports were all signed off days ago, reads as the tool having lost track -
    and a list of 26 client names does not settle it. There are only three
    reasons a report is not what the folder has, and which one it is decides
    whether you press sync or go and look."""
    from app.delivery import send_reason, out_of_sync_why

    class R:
        stored_path = __file__          # a real file, so it has a stamp
        delivered_as = ""
        delivered_stamp = ""
        filename = "July 2026_Benton Rodeo 53915.pdf"
        period = None                   # keeps canonical_name off the filename
        client = "Benton Rodeo"

    class E:
        client = "Benton Rodeo"
        ready = True
        def __init__(self, r): self.report = r

    r = R(); e = E(r)
    assert send_reason(e) == "never sent"

    r.delivered_as = "July 2026_Benton Rodeo 53915.pdf"
    from app.delivery import file_stamp
    r.delivered_stamp = file_stamp(r.stored_path)
    assert send_reason(e) == "", "it is what the partner has"

    r.delivered_as = "July 2026_Benton Rodeo 53915 50589.pdf"
    assert send_reason(e) == "renamed", "the order ids moved under it"

    r.delivered_as = "July 2026_Benton Rodeo 53915.pdf"
    r.delivered_stamp = "1:1"
    assert send_reason(e) == "new file", "the PDF itself was replaced"

    class G:
        expected = [e]
    assert out_of_sync_why(G()) == {"new file": 1}
    # A report nobody has touched is not counted at all.
    r.delivered_stamp = file_stamp(r.stored_path)
    assert out_of_sync_why(G()) == {}


def test_the_sync_tooltip_carries_the_reasons():
    cycle = (TPL / "cycle.html").read_text()
    assert "changed since this was packaged" not in cycle
    assert "not in the folder as" in cycle
    assert "stale_why.get(g.group)" in cycle


def test_a_report_with_a_newer_file_waiting_is_still_pending():
    """Parking a newer file leaves the sign-off alone on purpose - the copy
    that was signed off is still the copy the partner gets. But that put the
    row in Completed, which is the bucket nobody reads, and took the amber
    "newer file waiting" tag with it. A decision nobody can see is a decision
    nobody makes."""
    from app.board import Expected

    class Rep:
        def __init__(self, waiting):
            self.pending_path = "/tmp/newer.pdf" if waiting else ""
            self.pending_at = dt.datetime(2026, 8, 4, 12) if waiting else None
            self.review_state = "reviewed"
            self.severity = "pass"

        @property
        def has_pending(self):
            return bool(self.pending_path and self.pending_at)

        ready = True

    signed = Expected(market="M", group="M", client="C", kind="monthly")
    signed.report = Rep(False)
    assert signed.ready and not signed.waiting
    assert not signed.open_row, "nothing to do, so it is completed"

    waiting = Expected(market="M", group="M", client="D", kind="monthly")
    waiting.report = Rep(True)
    assert waiting.ready, "the sign-off is not torn up"
    assert waiting.waiting
    assert waiting.open_row, "but the row is open, because there is a decision"


def test_the_waiting_tag_goes_to_the_report():
    cycle = (TPL / "cycle.html").read_text()
    i = cycle.index("newer file waiting")
    tag = cycle[cycle.rindex("<a ", 0, i):i]
    assert 'href="/report/{{ e.report.id }}/view"' in tag
    assert "pending_name" in tag, "it names the file that is waiting"
    assert "stays open until somebody" in tag


def test_every_check_is_in_the_catalogue():
    """The rules sheet said what makes a report owed and nothing about what is
    done to it once it arrives, so "what does this thing look for" could only
    be answered by opening enough reports to have seen every finding fire.

    This fails on a check that gets added without a line describing it, which
    is the only way the list stays the whole list."""
    from app.flag_catalog import described
    from app.checks.rules import CHECKS

    have = {fn.__name__ for fn, _label in CHECKS}
    assert not (have - described()), "checks with nothing written about them"
    assert not (described() - have), "described checks that no longer exist"


def test_the_flags_tab_needs_no_javascript():
    """The sheet injects this page as innerHTML, and a script tag put in that
    way never runs. Tabs built on a click handler would work on the full page
    and do nothing in the sheet, which is where it is actually read."""
    body = (TPL / "rules_body.html").read_text()
    assert "<script" not in body
    assert 'type="radio" name="rulestab"' in body
    assert '#rtab-flags:checked ~ .rp-flags' in (TPL / "base.html").read_text()


def test_the_flags_tab_lists_them_all():
    from app.flag_catalog import flags
    from app.checks.rules import CHECKS

    got = flags()
    assert sum(len(g["checks"]) for g in got) == len(CHECKS)
    # Every row says what going wrong looks like AND what passing claims, and
    # they are not the same sentence.
    for g in got:
        assert g["group"]
        for c in g["checks"]:
            assert c["what"] and c["label"] and c["what"] != c["label"]


def test_the_monthly_rule_does_not_open_with_a_headline():
    body = (TPL / "rules_body.html").read_text()
    assert "One product is enough." not in body
    assert "The report covers the whole client" in body


def test_a_500_says_what_broke(tmp_path, monkeypatch):
    """"Internal Server Error" in three words is the same three words for every
    fault there is, so a screenshot of one says nothing and the only way to
    find out was to go and read Render's logs. This is an internal tool behind
    a password: the people who see this page are the people who need to know
    what it says."""
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'o.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTO_RECHECK", "false")
    from app import config as cfg; importlib.reload(cfg)
    from app import db as dbm; importlib.reload(dbm); dbm.init_db()
    from app import main as mmod; importlib.reload(mmod)

    @mmod.app.get("/boom-for-the-test")
    def _boom():
        raise ValueError("a distinctive thing went wrong")

    c = TestClient(mmod.app, raise_server_exceptions=False)
    r = c.get("/boom-for-the-test")
    assert r.status_code == 500
    # What broke, and on which build - the two things a screenshot has to carry.
    assert "a distinctive thing went wrong" in r.text
    assert mmod.version.BUILD in r.text
    assert "/boom-for-the-test" in r.text
    # And it is a page, not a wall of middleware.
    assert "Something broke on this page" in r.text
    assert "Back to the board" in r.text

    monkeypatch.undo()
    importlib.reload(cfg); importlib.reload(dbm); importlib.reload(mmod)


def test_the_flag_catalogue_does_not_force_a_re_check_of_the_board():
    """The rules fingerprint is a hash of every file in app/checks, and any
    change to it queues every report on the board to be judged again. The
    catalogue is prose ABOUT the rules - a wording fix in it should not cost
    seven hundred reports a re-read, so it lives outside that folder."""
    from pathlib import Path
    here = Path(__file__).resolve().parents[1] / "app"
    assert (here / "flag_catalog.py").exists()
    assert not (here / "checks" / "catalog.py").exists()


def test_the_tree_is_one_build():
    """The check on the repo itself. If this fails here, a file was edited and
    something that imports from it was not."""
    from app.selfcheck import stale_imports
    assert stale_imports() == []


def test_a_half_deployed_box_says_so_instead_of_500ing(tmp_path, monkeypatch):
    """A deploy is a zip of the files that changed, copied over the tree, and
    the day one is missed the box runs half of one build and half of another.
    No test catches that - a test always has the whole tree - and the symptom
    was ImportError on the board, hours later, as three words in Times New
    Roman.

    This is the check the failure suggests: read every `from .x import a` in
    the package and confirm x really has a. Most of them are inside functions,
    so nothing runs them until somebody opens the page they are on.
    """
    import importlib
    from pathlib import Path
    from fastapi.testclient import TestClient

    src = Path(__file__).resolve().parents[1] / "app" / "delivery.py"
    original = src.read_text()
    cut = original.index("def out_of_sync_why(group)")
    to = original.index("def latest_deliveries")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'o.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTO_RECHECK", "false")
    try:
        # Put yesterday's delivery.py on the box.
        src.write_text(original[:cut] + original[to:])
        from app import config as cfg; importlib.reload(cfg)
        from app import db as dbm; importlib.reload(dbm); dbm.init_db()
        # DROPPED, not reloaded. reload() runs the new source in the old
        # module's namespace, so a function the new file no longer has is
        # still sitting there - which is the one thing this test must not
        # have.
        import sys
        sys.modules.pop("app.delivery", None)
        from app import selfcheck as scmod; importlib.reload(scmod)
        from app import main as mmod; importlib.reload(mmod)

        found = scmod.check(force=True)
        assert found, "a missing function has to be noticed"
        assert "out_of_sync_why" in found[0]
        assert "delivery.py" in found[0], "it names the file to go and copy"

        c = TestClient(mmod.app)
        html = c.get("/orders").text
        assert "This deploy is incomplete" in html
        assert "out_of_sync_why" in html
        assert c.get("/healthz/deep").json()["ok"] is False
    finally:
        src.write_text(original)
        monkeypatch.undo()
        import sys
        sys.modules.pop("app.delivery", None)
        from app import config as cfg; importlib.reload(cfg)
        from app import db as dbm; importlib.reload(dbm)
        from app import selfcheck as scmod; importlib.reload(scmod)
        scmod.check(force=True)
        from app import main as mmod; importlib.reload(mmod)


def test_an_html5_creative_is_not_a_missing_preview_link():
    """HTML5 ads ship as a zip of markup and assets. There is no still frame,
    so the preview link is missing on every one of them - correctly. Flagging
    that is flagging the format, and the reporter can do nothing about it but
    tick the same box again next month."""
    from app.checks.quality import missing_preview_links, is_html5

    assert is_html5("HomeServices_300x250.zip")
    assert is_html5("Spring Sale.ZIP ")
    assert not is_html5("Zipline Tours")          # not an extension
    assert not is_html5("banner_300x250.jpg")

    head = "Creative Name          Preview Link          Impressions   Clicks"
    def grid(third):
        return "\n".join([
            "PPC Creative Performance", head,
            "summer_300x250.jpg     View                  12,000        140",
            "html5_banner.zip".ljust(45) + "9,000         88",
            third,
        ])

    text = grid("autumn_728x90.jpg      View                  7,500         61")
    assert missing_preview_links(text) == [], "the zip row is the format"

    # And a real one still fires.
    got = missing_preview_links(grid("autumn_728x90.jpg".ljust(45) + "7,500         61"))
    assert got and got[0][2] == 1, "one row with no link"
    assert got[0][3] == 2, "out of the two that could have had one"


def test_a_ticked_box_survives_a_new_file():
    """Ticking six known false alarms and then uploading the corrected pull
    used to clear all six, so the same six got ticked again every month. A
    sign-off is about a copy; an acceptance is about a FINDING, and the finding
    is as true of the new file as it was of the old one."""
    from app.recheck import remap_acks

    old = [{"code": "site_ctr", "title": "1 site clicking above 5%", "where": "p9"},
           {"code": "blank_preview", "title": "2 previews did not render", "where": "p4"},
           {"code": "ctr_high", "title": "CTR over 5%", "where": "p2"}]
    # The middle one was accepted.
    acked = [1]

    # The new file fixed the CTR one and moved the preview finding up the list.
    new = [{"code": "blank_preview", "title": "2 previews did not render", "where": "p4"},
           {"code": "site_ctr", "title": "1 site clicking above 5%", "where": "p9"}]
    assert remap_acks(old, acked, new) == [0], "carried, at its new position"

    # And a finding the new file fixed does not drag its tick onto something
    # else.
    assert remap_acks(old, [0], [{"code": "ctr_high", "title": "CTR over 5%",
                                  "where": "p2"}]) == []


def test_every_path_that_replaces_a_file_keeps_the_ticks():
    """Three places take a new PDF for a report that already exists - the feed
    superseding an unprotected copy, "Use the new file" on a parked arrival,
    and "Replace the file" by hand. All three cleared the ticks."""
    main = (Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    ingest = (Path(__file__).resolve().parents[1] / "app" / "ingest.py").read_text()
    assert main.count("rep.acked = remap_acks(") == 2
    assert ingest.count("rep.acked = remap_acks(") == 1
    # And none of them still wipes them.
    assert "rep.acked = []" not in ingest
    # main.py keeps one: a brand new report has nothing to carry.
    assert main.count("rep.acked = []") == 0


DAILY_SERVE_HEADER = [
    "Business Unit", "Client", "Impressions", "Clicks", "CTR", "Internal CPM",
    "Internal Cost", "Goal CPM %", "Goal CPM $", "Goal Internal CPM",
    "Campaign Name", "Campaign ID", "Avg Daily Serve", "Campaign Start Date",
    "Data Source Name", "Date", "Line Item Name", "Line Item ID",
    "Number of Days Served", "Number of Products", "Order ID",
    "Order Level Name", "Product", "Product Level Name", "Product Line Item ID",
    "Restricted", "Strategy ID", "Strategy Name", "Strategy Type",
    "Total Conversions", "View-throughs", "Click Conversions",
]


def test_the_daily_serve_export_is_read_as_a_serving_file():
    """The new file's own header, as it comes out of the reporting tool. It has
    a Campaign Start Date as well as a Date, which is the column that would
    otherwise be picked up as the day."""
    from app.serving import looks_like_serving, map_columns

    assert looks_like_serving(DAILY_SERVE_HEADER)
    cols = map_columns(DAILY_SERVE_HEADER)
    assert DAILY_SERVE_HEADER[cols["day"]] == "Date", "not Campaign Start Date"
    assert DAILY_SERVE_HEADER[cols["client"]] == "Client"
    assert DAILY_SERVE_HEADER[cols["market"]] == "Business Unit"
    assert DAILY_SERVE_HEADER[cols["impressions"]] == "Impressions"


def test_the_daily_file_is_told_apart_from_the_order_export():
    """Both land in the same folder. Merged into the orders it would be rows of
    nothing recognisable; read as orders it would empty the board."""
    from app import config as cfg
    from app.orders_s3 import is_serving_file, _name_matches

    assert is_serving_file("exports/client-serve_20260828.csv")
    assert is_serving_file("client_serve.csv"), "punctuation is not the point"
    assert not is_serving_file("exports/ordersdb7moupa_20260826.csv")
    assert not _name_matches("exports/client-serve_20260828.csv")
    assert _name_matches("exports/orders-db-anne.csv")


def test_a_daily_serve_file_adds_days_instead_of_replacing_them():
    """The hand-uploaded file covers a whole month and replaces it. The daily
    one carries whatever range it carries, so replacing on it would throw away
    every day it does not happen to mention - a client on twenty days would
    read as one, and drop off the board."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, ServedDays
    from app.serving import import_serving

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()

    def rows(days):
        out = [["Business Unit", "Client", "Impressions", "Date"]]
        for d in days:
            out.append(["Acme Media", "Bloom Heating", "12000", d])
        return out

    # Monday's file: three days.
    import_serving(db, rows(["2026-08-03", "2026-08-04", "2026-08-05"]),
                   merge=True)
    got = db.query(ServedDays).one()
    assert got.days == 3

    # Tuesday's: one new day, and two it has seen before.
    import_serving(db, rows(["2026-08-05", "2026-08-06"]), merge=True)
    got = db.query(ServedDays).one()
    assert got.days == 4, "the union, not the newest file's count"
    assert got.first_day == dt.date(2026, 8, 3)
    assert got.last_day == dt.date(2026, 8, 6)

    # A row with no impressions on it is not a day of delivery.
    zero = [["Business Unit", "Client", "Impressions", "Date"],
            ["Acme Media", "Bloom Heating", "0", "2026-08-07"]]
    import_serving(db, zero + [["Acme Media", "Bloom Heating", "5", "2026-08-08"]],
                   merge=True)
    assert db.query(ServedDays).one().days == 5

    # And a hand-uploaded file still replaces the month outright.
    import_serving(db, rows(["2026-08-20"]))
    got = db.query(ServedDays).one()
    assert got.days == 1, "an upload by hand is the whole month"
    db.close(); eng.dispose()


def test_a_count_loaded_before_the_days_were_kept_is_a_floor():
    """Rows already in the database have a count and no dates. Merging must not
    replace a real 20 with the 2 days this morning's file mentions."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, ServedDays
    from app.serving import import_serving, _key

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(ServedDays(period="2026-08", market_key=_key("Acme Media"),
                      client_key=_key("Bloom Heating"), market="Acme Media",
                      client="Bloom Heating", days=20, day_list=[]))
    db.commit()

    import_serving(db, [["Business Unit", "Client", "Impressions", "Date"],
                        ["Acme Media", "Bloom Heating", "900", "2026-08-27"],
                        ["Acme Media", "Bloom Heating", "900", "2026-08-28"]],
                   merge=True)
    assert db.query(ServedDays).one().days == 20, "the old count still stands"
    db.close(); eng.dispose()


def test_the_cycle_is_august():
    from app.config import Settings
    assert Settings.model_fields["default_period"].default == "2026-08"


ROSTER_CSV = ("Partner,Buyer,Email,SEO,Email,Manager,Reporting Team,To:,"
              "Trainer,Reporting Notes,Buyer Notes,Group,Delivery\n")


def _roster(n, target=""):
    out = ROSTER_CSV
    for i in range(n):
        out += (f"Partner {i},Bella,bella@vicimediainc.com,Matt,"
                f"matt@vicimediainc.com,Amin,Paulina,client{i}@x.com,Jennaya,"
                f",,Partner {i},{target}\n")
    return out


def test_the_sheet_link_is_read_for_its_id_and_tab():
    """A pasted browser URL is what somebody has to hand, so that is what it
    takes - the id and the tab come out of it."""
    from app import roster_sheet as rs

    url = ("https://docs.google.com/spreadsheets/d/"
           "1_WfyDOEN4oOdPdEoYtv5yMZZ6_hk8vu5o6pElIJ-8d4/edit?gid=0#gid=0")
    # THE MODULE'S OWN SETTINGS OBJECT. Another fixture in this file reloads
    # app.config, so app.config.settings and roster_sheet.settings are two
    # different objects by the time this runs - and setting the one nobody is
    # reading is a test that passes alone and fails in company.
    cfg = rs
    old = cfg.settings.roster_sheet
    try:
        cfg.settings.roster_sheet = url
        assert rs.sheet_id() == "1_WfyDOEN4oOdPdEoYtv5yMZZ6_hk8vu5o6pElIJ-8d4"
        assert rs.sheet_gid() == "0"
        assert rs.configured()
        # A bare id works too.
        cfg.settings.roster_sheet = "1_WfyDOEN4oOdPdEoYtv5yMZZ6_hk8vu5o6pElIJ-8d4"
        assert rs.sheet_id() == "1_WfyDOEN4oOdPdEoYtv5yMZZ6_hk8vu5o6pElIJ-8d4"
        cfg.settings.roster_sheet = ""
        assert not rs.configured()
    finally:
        cfg.settings.roster_sheet = old


def test_a_sign_in_page_does_not_delete_the_roster():
    """THE FAILURE THAT MATTERS IS THE SILENT ONE. A read that comes back as a
    Google sign-in page, or empty because a tab was renamed, parses perfectly
    well as "a roster with no partners in it" - and importing that takes 206
    partners and every owner on the board with them."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, Partner
    from app.partners import NotARoster, import_partners

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    import_partners(db, _roster(200))
    assert db.query(Partner).count() == 200

    for junk in ("<html><body>Sign in</body></html>", ROSTER_CSV,
                 _roster(3)):
        with pytest.raises((NotARoster, ValueError)):
            import_partners(db, junk, keep_targets=True, min_rows=132)
        assert db.query(Partner).count() == 200, "the roster is untouched"

    # A real edit still loads, including a genuine batch of leavers.
    assert import_partners(db, _roster(150), keep_targets=True, min_rows=132) == 150
    assert db.query(Partner).count() == 150
    db.close(); eng.dispose()


def test_a_delivery_target_set_here_is_not_blanked_by_the_sheet():
    """Where a partner takes delivery is set in the tool and not always in the
    sheet. Overwriting it with a blank hands a Dropbox partner's client a
    Google Drive link, and nothing on screen looks wrong."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, Partner
    from app.partners import import_partners

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    import_partners(db, _roster(3))
    p = db.query(Partner).filter_by(partner="Partner 1").one()
    p.delivery_target = "dropbox"
    db.commit()

    # The sheet says nothing about delivery.
    import_partners(db, _roster(3), keep_targets=True)
    assert db.query(Partner).filter_by(partner="Partner 1").one().delivery_target \
        == "dropbox", "kept, because the sheet did not say otherwise"

    # And when the sheet DOES say, the sheet wins.
    import_partners(db, _roster(3, target="drive"), keep_targets=True)
    assert db.query(Partner).filter_by(partner="Partner 1").one().delivery_target \
        == "drive"

    # An upload by hand still replaces outright, as it always did.
    import_partners(db, _roster(3))
    assert db.query(Partner).filter_by(partner="Partner 1").one().delivery_target == ""
    db.close(); eng.dispose()


def test_the_sheet_is_read_only():
    """A tool that edits the sheet it reads is a tool nobody trusts."""
    src = (Path(__file__).resolve().parents[1] / "app" / "roster_sheet.py").read_text()
    for writer in ("update(", "append_row", "batchUpdate", "values().update",
                   "spreadsheets().values"):
        assert writer not in src, f"{writer} writes to the sheet"


def test_an_order_that_starts_this_month_is_not_dropped_on_the_way_in():
    """River Valley Builders Facebook, order 55476, IO Live, 1 August to 31
    December, came back on the board as a client that delivered 31 days with no
    order behind it. So did 117 others.

    The import keeps line items that touch "the period", and the period
    defaulted to the calendar month BEFORE today - so on 31 August every line
    item starting 1 August was skipped as "starts after the period". A whole
    month of new orders, dropped by a window a month behind the board.
    """
    from app.orders_io import import_io_export
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, OrderLine

    header = ("client_business_unit,orders_status,client,orders_id,product,id,"
              "status,orders_start_date,start_date,end_date,orders_end_date\n")
    row = ("7 Mountains PA Selinsgrove,IO Live,River Valley Builders Facebook,"
           "55476,Meta,551,IO Live,2026-08-01,2026-08-01,2026-12-31,2026-12-31\n")
    # And one that starts the month after, which the rollover would otherwise
    # leave behind on the 1st.
    soon = row.replace("55476", "55999").replace("2026-08-01", "2026-09-01")

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    res = import_io_export(db, (header + row + soon).encode(), period="2026-08")
    # One row per client and product, so both orders roll into it.
    ids = " ".join(l.account_ids or "" for l in db.query(OrderLine).all())
    assert "55476" in ids, f"kept, {res.get('skipped')}"
    assert "55999" in ids, "next month's orders are here before the rollover"

    # A year out is still not this cycle's problem.
    far = row.replace("55476", "56999").replace("2026-08-01", "2027-06-01")
    db.query(OrderLine).delete(); db.commit()
    import_io_export(db, (header + far).encode(), period="2026-08")
    assert db.query(OrderLine).count() == 0
    db.close(); eng.dispose()


def test_the_import_window_follows_the_cycle_not_the_calendar():
    from app.orders_io import import_io_export
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, OrderLine
    from app import config as cfg

    header = ("client_business_unit,orders_status,client,orders_id,product,id,"
              "status,orders_start_date,start_date,end_date,orders_end_date\n")
    row = ("Partner,IO Live,A Client,1,Meta,11,IO Live,"
           "2026-08-01,2026-08-01,2026-12-31,2026-12-31\n")
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    was = cfg.settings.default_period
    try:
        cfg.settings.default_period = "2026-08"
        import_io_export(db, (header + row).encode())   # no period passed
        assert db.query(OrderLine).count() == 1, "the board's cycle is the window"
    finally:
        cfg.settings.default_period = was
    db.close(); eng.dispose()


def test_moving_the_cycle_re_reads_the_export():
    """Nothing re-reads the export when the cycle rolls over - the file has not
    changed, so the ETag says there is nothing to do. That is how August's
    orders stayed dropped after the board moved to August."""
    src = (Path(__file__).resolve().parents[1] / "app" / "orders_s3.py").read_text()
    assert 'mapv = f"{product_map_version()}:' in src
    assert "default_period or current_period()" in src


def test_a_beta_client_is_not_a_missing_order():
    """The reporting tool carries trial records named "... - GT - beta-2026".
    They deliver impressions like anything else and will never have an order
    behind them, so they sat on the missing-orders panel forever - and a flag
    that is permanently on is a flag nobody reads."""
    from app.serving import is_beta

    assert is_beta("Plumb Creek Pet Lodge - GT - beta-2026")
    assert is_beta("North Texas Fair & Rodeo - GT - beta-2026")
    assert is_beta("Stanley's Greenhouse - GT - beta-2027")
    # The hyphen is required. Without it this catches a real client.
    assert not is_beta("Zeta Beta 1999")
    assert not is_beta("Betamax 2026 Ltd")
    assert not is_beta("Beta Alpha Co")


def test_a_beta_client_is_dropped_on_the_way_in():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, ServedDays
    from app.serving import import_serving

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    import_serving(db, [["Business Unit", "Client", "Impressions", "Date"],
                        ["Acme", "Real Client", "900", "2026-08-04"],
                        ["Acme", "Plumb Creek - GT - beta-2026", "900", "2026-08-04"]])
    assert [r.client for r in db.query(ServedDays).all()] == ["Real Client"]
    db.close(); eng.dispose()


def test_a_client_whose_orders_are_under_another_partner_says_so():
    """Shasta Farm & Equipment delivers under "Results Media Solutions Redding"
    in the serving file, and order 52146 sits under "Results Media Solutions
    Chico". One client, two business units - and the row read as a partner
    export that had not landed, which sends somebody looking for a file that is
    already there."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, OrderLine, ServedDays
    from app.serving import served_but_no_order, _key

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(OrderLine(market="Results Media Solutions Chico",
                     client="Shasta Farm & Equipment", account_ids="52146",
                     product="Display", starts_on=dt.date(2026, 4, 10),
                     ends_on=dt.date(2027, 3, 31)))
    # Redding has orders of its own, so "no file landed" is not the answer.
    db.add(OrderLine(market="Results Media Solutions Redding",
                     client="Someone Else", account_ids="1", product="Display",
                     starts_on=dt.date(2026, 1, 1), ends_on=dt.date(2026, 12, 31)))
    db.add(ServedDays(period="2026-08",
                      market_key=_key("Results Media Solutions Redding"),
                      client_key=_key("Shasta Farm & Equipment"),
                      market="Results Media Solutions Redding",
                      client="Shasta Farm & Equipment", days=31))
    db.commit()

    got = served_but_no_order(db, "2026-08")
    assert len(got) == 1
    assert got[0][3] == "its orders are under Results Media Solutions Chico"
    db.close(); eng.dispose()


def test_the_lookup_answers_the_three_questions():
    """"I see this order, why is it not on the board" was a screenshot and a
    round trip every time, and everything needed to answer it was already
    loaded with no way to ask."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, OrderLine, ServedDays
    from app.lookup import find
    from app.serving import _key

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(OrderLine(market="Results Media Solutions Chico",
                     client="Shasta Farm & Equipment", account_ids="52146",
                     product="Display", status="IO Live",
                     starts_on=dt.date(2026, 4, 10), ends_on=dt.date(2027, 3, 31)))
    db.add(ServedDays(period="2026-08",
                      market_key=_key("Results Media Solutions Redding"),
                      client_key=_key("Shasta Farm & Equipment"),
                      market="Results Media Solutions Redding",
                      client="Shasta Farm & Equipment", days=31))
    db.commit()

    # By order id: loaded, and delivering under a different partner.
    got = find(db, "52146", "2026-08")
    assert got["lines"] and got["lines"][0].account_ids == "52146"
    joined = " ".join(got["notes"])
    assert "Loaded." in joined
    assert "DELIVERS UNDER A DIFFERENT PARTNER" in joined
    assert "Redding" in joined and "Chico" in joined

    # An order nobody has heard of.
    got = find(db, "54568", "2026-08")
    assert not got["lines"]
    assert "Not in the order list loaded here." in " ".join(got["notes"])

    # By name, where the partner has no orders at all.
    db.add(ServedDays(period="2026-08", market_key=_key("Growth by Design"),
                      client_key=_key("Credit Union Audit Group"),
                      market="Growth by Design",
                      client="Credit Union Audit Group", days=31))
    db.commit()
    got = find(db, "Credit Union Audit", "2026-08")
    joined = " ".join(got["notes"])
    assert "NO orders loaded at all" in joined, joined
    db.close(); eng.dispose()


def test_linkedin_is_a_product():
    """It is a product the tool knows about everywhere else - the export
    carries monthly_linkedin_ad_spend, the device check excludes it by name -
    and the product map had no entry at all. Every LinkedIn line item was
    thrown out of the order list, and Credit Union Audit Group, which sells
    nothing else and delivers 31 days a month, was not on the board."""
    from app.checks.products import map_order_products

    assert map_order_products("LinkedIn Ads") == ["LinkedIn"]
    assert map_order_products("LinkedIn") == ["LinkedIn"]
    assert map_order_products("LinkedIn Display & Video Ads") == ["LinkedIn"]


def test_an_unmapped_product_does_not_delete_the_client():
    """A product name the map has never seen used to throw the line away, so a
    client whose ONLY product was unknown vanished from the board completely -
    no row, no report expected, and nothing saying why."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, OrderLine
    from app.orders_io import import_io_export

    header = ("client_business_unit,orders_status,client,orders_id,product,id,"
              "status,orders_start_date,start_date,end_date,orders_end_date\n")
    row = ("Growth by Design,IO Live,A Client,54568,Skywriting Ads,132082,"
           "IO Live,2026-07-06,2026-07-06,2026-09-30,2026-09-30\n")

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    res = import_io_export(db, (header + row).encode(), period="2026-08")
    line = db.query(OrderLine).one()
    assert line.client == "A Client", "the client is on the board"
    assert line.product == "Skywriting Ads", "under the name the export gave"
    assert res["unmapped_products"] == {"Skywriting Ads": 1}

    # AND IT IS NOT EXPECTED ON A REPORT. Nothing knows what it looks like when
    # it is there, so failing a report for its absence would be the tool
    # blaming somebody for a gap in its own dictionary.
    from app.roster import expected_products, is_mapped
    assert not is_mapped("Skywriting Ads")
    assert expected_products(db, "A Client", "54568", period="2026-08") == set()
    db.close(); eng.dispose()


def test_the_unmapped_products_are_named_on_the_page(client_orders_db):
    """Kept quietly is how a product stays unmapped forever."""
    c, db, dbm = client_orders_db
    db.add(dbm.OrderLine(market="Growth by Design", client="A Client",
                         account_ids="54568", product="Skywriting Ads",
                         starts_on=dt.date(2026, 1, 1),
                         ends_on=dt.date(2026, 12, 31)))
    db.add(dbm.OrderLine(market="Growth by Design", client="B Client",
                         account_ids="1", product="LinkedIn",
                         starts_on=dt.date(2026, 1, 1),
                         ends_on=dt.date(2026, 12, 31)))
    db.commit()
    html = c.get("/orders").text
    assert "not in the product map" in html
    assert "Skywriting Ads" in html
    # A product that IS mapped is not in the panel.
    panel = html[html.index("not in the product map"):]
    panel = panel[:panel.index("</section>")]
    assert "LinkedIn" not in panel


def test_no_sentence_the_tests_assert_on_is_split_across_lines():
    """This has now cost two rounds. A phrase wrapped across two lines in a
    template is not in the rendered HTML as one string, so an assertion on it
    fails while the page is perfectly correct - and the hunt goes looking for a
    bug in the code underneath it.

    These are the phrases other tests match on. They have to survive wrapping.
    """
    checked = {
        "orders.html": ["not in the product map",
                        "delivered impressions in",
                        "linked to the wrong client record"],
        "cycle.html": ["not in the folder as"],
        "viewer.html": ["View order lines"],
    }
    for name, phrases in checked.items():
        html = (TPL / name).read_text()
        for phrase in phrases:
            assert phrase in html, f"{name}: {phrase!r} is wrapped across lines"


def _line(dbm, client, product, market="Partner", ids="1"):
    return dbm.OrderLine(market=market, client=client, account_ids=ids,
                         product=product, status="IO Live", live=True,
                         starts_on=dt.date(2026, 1, 1),
                         ends_on=dt.date(2026, 12, 31),
                         order_starts_on=dt.date(2026, 1, 1),
                         order_ends_on=dt.date(2026, 12, 31))


def test_a_product_that_never_shows_on_a_report_is_not_owed_one():
    """Website Visitor ID and Additional Billing are invoiced line items with
    no widget, and there never will be one. Live Chat DOES belong on a report -
    it is only ever sold alongside another digital product, so it never brings
    one with it."""
    from app.checks.products import earns_a_report, on_a_report

    assert not earns_a_report("Website Visitor ID")
    assert not earns_a_report("Additional Billing")
    assert not earns_a_report("Live Chat")
    assert earns_a_report("Display") and earns_a_report("LinkedIn")

    # But only two of those three are absent from the page.
    assert not on_a_report("Website Visitor ID")
    assert not on_a_report("Additional Billing")
    assert on_a_report("Live Chat"), "it belongs on the report, it just does not earn one"


def test_a_client_running_only_those_is_not_on_the_board():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import db as dbm
    from app.board import expected_for

    eng = create_engine("sqlite://")
    dbm.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(_line(dbm, "Billing Only", "Additional Billing", ids="1"))
    db.add(_line(dbm, "Chat Only", "Live Chat", ids="2"))
    db.add(_line(dbm, "Visitor Only", "Website Visitor ID", ids="3"))
    # And one client running Live Chat WITH something else, which is the real
    # shape - the chat is on that report, and the report is owed.
    db.add(_line(dbm, "Real Client", "Live Chat", ids="4"))
    db.add(_line(dbm, "Real Client", "Display", ids="4"))
    db.commit()

    clients = {e.client for e in expected_for(db, "2026-08")}
    assert clients == {"Real Client"}, clients
    row = next(e for e in expected_for(db, "2026-08"))
    assert "Live Chat" in row.products, "it rides along on the report it is sold with"
    db.close(); eng.dispose()


def test_a_report_is_not_failed_for_missing_what_can_never_be_on_it():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import db as dbm
    from app.roster import expected_products

    eng = create_engine("sqlite://")
    dbm.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(_line(dbm, "A Client", "Display", ids="9"))
    db.add(_line(dbm, "A Client", "Website Visitor ID", ids="9"))
    db.add(_line(dbm, "A Client", "Live Chat", ids="9"))
    db.commit()
    got = expected_products(db, "A Client", "9", period="2026-08")
    assert got == {"Display", "Live Chat"}, got
    db.close(); eng.dispose()


# Spellings that must not appear anywhere a person reads. The value is what to
# write instead, so a failure tells you the fix rather than only the fault.
BRITISH_SPELLINGS = {
    "colour": "color", "colours": "colors", "coloured": "colored",
    "colouring": "coloring",
    "recognise": "recognize", "recognised": "recognized",
    "recognisable": "recognizable", "recognising": "recognizing",
    "organise": "organize", "organised": "organized",
    "normalise": "normalize", "normalised": "normalized",
    "normalising": "normalizing",
    "summarise": "summarize", "analyse": "analyze", "analysed": "analyzed",
    "analysing": "analyzing",
    "behaviour": "behavior", "labelled": "labeled", "labelling": "labeling",
    "whilst": "while", "amongst": "among", "catalogue": "catalog",
    "centre": "center", "centred": "centered", "judgement": "judgment",
    "acknowledgement": "acknowledgment", "licence": "license",
    "defence": "defense", "favourite": "favorite", "programme": "program",
    "apologise": "apologize", "capitalise": "capitalize",
    "prioritise": "prioritize", "utilise": "utilize", "customise": "customize",
    "optimise": "optimize", "authorise": "authorize",
    "authorised": "authorized", "authorises": "authorizes",
    "realise": "realize", "realised": "realized",
    "cancelling": "canceling", "travelled": "traveled", "modelled": "modeled",
    "sceptical": "skeptical", "grey": "gray", "towards": "toward",
    "afterwards": "afterward", "maths": "math", "humanise": "humanize",
    "itemised": "itemized", "memorised": "memorized",
    "enquiry": "inquiry", "speciality": "specialty", "storey": "story",
    "aluminium": "aluminum", "metre": "meter", "theatre": "theater",
}


def test_everything_is_american_english():
    """One tool, one spelling. This covers the templates and the code together,
    because half the words a person reads on screen are written in a Python
    string and the comments are read by whoever comes to fix this next.

    NOT the tests and not the fixtures: those carry client names and order
    statuses copied out of the export - "Cancelled", "Centre" - and correcting
    somebody's data is a different thing entirely.
    """
    import re as _re
    root = Path(__file__).resolve().parents[1] / "app"
    pattern = _re.compile(r"\b(" + "|".join(BRITISH_SPELLINGS) + r")\b", _re.I)
    bad = []
    for path in sorted(root.rglob("*")):
        if path.suffix not in (".py", ".html") or "__pycache__" in path.parts:
            continue
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in pattern.finditer(line):
                word = m.group(0).lower()
                bad.append(f"{path.name}:{n} {m.group(0)!r} -> "
                           f"{BRITISH_SPELLINGS[word]!r}")
    assert not bad, "British spellings:\n  " + "\n  ".join(bad[:25])


def test_an_older_export_does_not_beat_a_newer_one():
    """Two exports of the same order pulled a week apart carry the same order
    id, the same line id and the same flight - and not the same status. Keyed
    without the status, the first file read won and the second was thrown away
    as a duplicate, so an order that was "RFP Pending" on Tuesday and has been
    IO Live since was dropped by the RFP filter and never seen again.

    The page said "Overlapping exports are fine" the whole time.
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, OrderLine
    from app.orders_io import import_io_export

    header = ("client_business_unit,orders_status,client,orders_id,product,id,"
              "status,orders_start_date,start_date,end_date,orders_end_date\n")
    stale = ("Local Media San Diego,RFP Pending Approval,CBF Productions,55377,"
             "Social Mirror Ads,134401,RFP Pending Approval,2026-08-24,"
             "2026-08-24,2026-09-24,2026-09-24\n")
    fresh = ("Local Media San Diego,IO Live,CBF Productions,55377,"
             "Social Mirror Ads,134401,IO Live,2026-08-24,"
             "2026-08-24,2026-09-24,2026-09-24\n")

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    # The stale file first, which is the case that used to lose the order.
    import_io_export(db, (header + stale + fresh).encode(), period="2026-08")
    got = db.query(OrderLine).all()
    assert len(got) == 1, "the live row is read, not swallowed as a duplicate"
    assert got[0].client == "CBF Productions"
    assert got[0].live is True

    # And the same rows in the other order come out the same way.
    db.query(OrderLine).delete(); db.commit()
    import_io_export(db, (header + fresh + stale).encode(), period="2026-08")
    assert db.query(OrderLine).count() == 1
    db.close(); eng.dispose()


def test_a_dropped_client_is_recorded_by_name():
    """The skip counts said "5,796 RFP" and nothing about whose, so "I can see
    this order in the export and it is not on the board" could only be answered
    by downloading the export and running the importer over it by hand. It took
    exactly that, twice."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base
    from app.orders_io import import_io_export

    header = ("client_business_unit,orders_status,client,orders_id,product,id,"
              "status,orders_start_date,start_date,end_date,orders_end_date\n")
    rfp = ("Zoey Advertising,RFP Pending Approval,Safe Harbor- Auburn Workshop,"
           "55963,Social Mirror Ads,135993,RFP Pending Approval,2026-08-28,"
           "2026-08-28,2026-09-08,2026-09-08\n")
    old = ("Zoey Advertising,IO Complete,An Old Client,111,Display,222,"
           "IO Complete,2024-01-01,2024-01-01,2024-06-30,2024-06-30\n")

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    res = import_io_export(db, (header + rfp + old).encode(), period="2026-08")
    dropped = res["dropped"]
    assert dropped["Zoey Advertising|Safe Harbor- Auburn Workshop"] == \
        "the export has it as an RFP, not a live order"
    assert "ended before 2026-08 started" in \
        dropped["Zoey Advertising|An Old Client"]
    db.close(); eng.dispose()


def test_the_lookup_says_it_was_read_and_dropped():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, OrderSync
    from app.lookup import find

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(OrderSync(source="s3://bucket/orders-db-all-1.csv", ok=True, rows=10,
                     dropped={"Zoey Advertising|Safe Harbor- Auburn Workshop":
                              "the export has it as an RFP, not a live order"}))
    db.commit()

    got = find(db, "Safe Harbor- Auburn Workshop", "2026-08")
    joined = " ".join(got["notes"])
    assert "Not in the order list loaded here." in joined
    assert "IT IS IN THE EXPORT AND WAS DROPPED ON THE WAY IN" in joined
    assert "RFP" in joined
    db.close(); eng.dispose()


def test_names_read_as_first_names():
    """It is how everybody here refers to each other, and a card already
    carrying a partner, a percentage, a progress bar and six state pills does
    not need a surname on top of it."""
    from app.partners import first_name

    assert first_name("Lauren Hunter") == "Lauren"
    assert first_name("Katie Oxman") == "Katie"
    assert first_name("Anna Halligan") == "Anna"
    assert first_name("") == ""
    # Two people in one field stay two people.
    assert first_name("Todd, Megan") == "Todd, Megan"
    assert first_name("Todd Sanders, Megan Hill") == "Todd, Megan"


def test_two_people_with_one_first_name_keep_their_surnames():
    """The trainer Katie and the buyer Katie are different people, and roles
    keep them apart - a name is only ever read inside its own role. Two Katies
    in the SAME role is the case a first name cannot survive."""
    from app.partners import first_name

    role = {"Katie Oxman", "Katie Reed", "Lauren Hunter"}
    assert first_name("Katie Oxman", role) == "Katie Oxman"
    assert first_name("Katie Reed", role) == "Katie Reed"
    # The one that is not shared still shortens.
    assert first_name("Lauren Hunter", role) == "Lauren"
    # And a bare "Katie" beside a "Katie Oxman" is left alone - there is
    # nothing more to show.
    assert first_name("Katie", {"Katie", "Katie Oxman"}) == "Katie"


def test_the_same_person_spelled_two_ways_is_one_person_on_the_workload():
    """The sheet says "Lauren" on one partner and "Lauren Hunter" on another.
    Counted as written that is two people with half a workload each."""
    from app.partners import first_name

    pool = {"Lauren", "Lauren Hunter", "Matt"}
    assert first_name("Lauren", pool) == first_name("Lauren Hunter", pool)


def test_a_sign_off_shows_a_first_name_and_stores_the_whole_one():
    """The stored value is the record of who said so. This is only how it
    reads on a row."""
    from app.db import Report

    r = Report(review_state="reviewed", reviewed_by="Lauren Hunter")
    assert r.signed_off_by == "Lauren"
    assert r.reviewed_by == "Lauren Hunter", "the record keeps what was typed"


def _served(dbm, market, client, days, first, last, period="2026-08"):
    from app.serving import _key
    return dbm.ServedDays(period=period, market_key=_key(market),
                          client_key=_key(client), market=market, client=client,
                          days=days, first_day=dt.date.fromisoformat(first),
                          last_day=dt.date.fromisoformat(last))


def test_a_campaign_trailing_off_is_not_a_missing_order():
    """An order that ran to the last day of a month does not stop dead at
    midnight - a few impressions land in the first days of the next one, and
    the serving file counts them as days served. The order behind them ended
    before this month started, so it is correctly not in the order list, and
    the client came up as "delivered with no order behind it" every month on
    the way out."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app import db as dbm
    from app.serving import served_but_no_order, tailing_off

    eng = create_engine("sqlite://")
    dbm.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    # The partner has orders, so "no file landed" is not the answer either way.
    db.add(dbm.OrderLine(market="Manning Media", client="Someone Else",
                         account_ids="1", product="Display",
                         starts_on=dt.date(2026, 1, 1),
                         ends_on=dt.date(2026, 12, 31)))
    # Three days, all in the first week: the last of a finished campaign.
    db.add(_served(dbm, "Manning Media", "Transit of Frederick", 3,
                   "2026-08-01", "2026-08-03"))
    # Three days at the END of the month is a campaign that just launched, and
    # that DOES need an order behind it.
    db.add(_served(dbm, "Manning Media", "Just Launched", 3,
                   "2026-08-27", "2026-08-29"))
    # And a month of delivery is never a tail.
    db.add(_served(dbm, "Manning Media", "Running All Month", 31,
                   "2026-08-01", "2026-08-31"))
    db.commit()

    got = {c for _m, c, _d, _w in served_but_no_order(db, "2026-08")}
    assert got == {"Just Launched", "Running All Month"}, got
    assert tailing_off(db, "2026-08") == 1, "counted, not silently dropped"
    db.close(); eng.dispose()


def test_the_tail_rule_needs_both_halves():
    from app import db as dbm
    from app.serving import trailing_off

    def row(days, last):
        return dbm.ServedDays(days=days, last_day=dt.date.fromisoformat(last))

    assert trailing_off(row(1, "2026-08-01"))
    assert trailing_off(row(3, "2026-08-03"))
    assert trailing_off(row(2, "2026-08-07"))
    # Too many days.
    assert not trailing_off(row(4, "2026-08-04"))
    # Too late in the month.
    assert not trailing_off(row(3, "2026-08-08"))
    assert not trailing_off(row(2, "2026-08-30"))
    # No dates at all - say nothing rather than guess.
    assert not trailing_off(dbm.ServedDays(days=2, last_day=None))


@pytest.fixture()
def audit_client(tmp_path, monkeypatch):
    import importlib
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'a.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("AUTO_RECHECK", "false")
    monkeypatch.setenv("DEFAULT_PERIOD", "2026-08")
    from app import config as cfg; importlib.reload(cfg)
    from app import db as dbm; importlib.reload(dbm); dbm.init_db()
    from app import main as mmod; importlib.reload(mmod)
    db = dbm.SessionLocal()
    yield TestClient(mmod.app), db, dbm
    db.close(); monkeypatch.undo()
    importlib.reload(cfg); importlib.reload(dbm); importlib.reload(mmod)


def test_the_pasted_list_survives_a_refresh(audit_client):
    """Four hundred rows of somebody's tracker, pasted, compared, and gone the
    moment the page reloads - or worse, the browser offering to send it all
    again. The way this check gets used is: read it, go and fix three rows,
    look again. That needs the list to still be here."""
    c, db, dbm = audit_client
    # NOT the sample in the box's placeholder, or this proves nothing.
    rows = "LOCK AUG - Augusta Technical College #54006\nCCBM - Cape Symphony #53956"
    c.post("/cycle/audit", data={"period": "2026-08", "rows": rows, "group": ""})

    # A plain visit brings it back and re-runs it.
    page = c.get("/cycle/audit").text
    assert "Augusta Technical College #54006" in page
    assert "Kept for August 2026" in page
    assert db.query(dbm.AuditList).count() == 1

    # Submitting an empty box forgets it, which is what the Clear button does.
    c.post("/cycle/audit", data={"period": "2026-08", "rows": "", "group": ""})
    assert db.query(dbm.AuditList).count() == 0
    assert "Augusta Technical College #54006" not in c.get("/cycle/audit").text


def test_a_row_can_be_approved_onto_the_board(audit_client):
    """Some of these belong on the board and the rules missed them. Approving
    is the same override as "Needs a report" on the not-owed panel - the client
    appears in the cycle with the note as the reason it is there."""
    c, db, dbm = audit_client
    db.add(dbm.OrderLine(market="7 Mountains PA Selinsgrove", client="Benton Rodeo",
                         account_ids="53915", product="Display",
                         starts_on=dt.date(2026, 1, 1), ends_on=dt.date(2026, 6, 30)))
    db.commit()

    c.post("/cycle/audit/call", data={
        "period": "2026-08", "ref": "53915", "kind": "monthly",
        "client": "Benton Rodeo", "call": "approved",
        "note": "rodeo ran, tracker is right", "who": "Kiri"})

    call = db.query(dbm.AuditCall).one()
    assert call.call == "approved" and call.note == "rodeo ran, tracker is right"
    assert call.who == "Kiri"
    # AND IT REACHED THE BOARD, as the same override the not-owed panel writes.
    mark = db.query(dbm.CycleDone).one()
    assert mark.reason == "needed"
    assert mark.market == "7 Mountains PA Selinsgrove"
    assert mark.note == "rodeo ran, tracker is right"
    assert mark.ident == "7mountainspaselinsgrove|bentonrodeo|monthly"


def test_a_rejected_row_stays_rejected(audit_client):
    """It is a decision, and the point of it is not making forty of them again
    next time somebody opens the page."""
    c, db, dbm = audit_client
    c.post("/cycle/audit/call", data={
        "period": "2026-08", "ref": "26734", "kind": "monthly",
        "client": "7 Mountains Media LIVE CHAT ONLY", "call": "rejected",
        "note": "live chat only, no report owed", "who": "Kiri"})
    call = db.query(dbm.AuditCall).one()
    assert call.call == "rejected"
    # Nothing was put on the board.
    assert db.query(dbm.CycleDone).count() == 0

    # And it can be undone.
    c.post("/cycle/audit/call", data={"period": "2026-08", "ref": "26734",
                                      "kind": "monthly", "call": "clear"})
    assert db.query(dbm.AuditCall).count() == 0
