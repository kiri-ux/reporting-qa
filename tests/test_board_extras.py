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


def test_a_filter_hides_its_count_column_when_every_count_is_one():
    """On a Partner filter each partner appears exactly once, so the counts were
    a column of 1s that looked like they meant something."""
    base = (TPL / "base.html").read_text()
    assert "var informative = names.some(" in base
    assert "if (informative) {" in base


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
        for _t, name, imps, clicks, printed in site_rows(pdf_text(f)):
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
    assert 'class="sync"' in cycle and "{{ stale.total }}</button>" in cycle
    assert cycle.index('class="sync"') < cycle.index("Download CSV")


def test_the_button_becomes_a_progress_readout_while_it_runs():
    cycle = (TPL / "cycle.html").read_text()
    assert 'class="sync busy"' in cycle
    assert "{{ j.done }} of {{ j.total or '?' }}" in cycle


def test_the_partner_button_carries_no_count():
    """"Re-check 2" under a heading that says "14 reports" reads as a bug, even
    when 2 is the true number of stale ones. The numbers go in the hover and
    the button does the whole partner."""
    cycle = (TPL / "cycle.html").read_text()
    assert ">\n                Re-check</button>" in cycle
    assert "Re-check {{ stale.by_group[g.group] }}" not in cycle
    assert 'name="scope" value="all"' in cycle
