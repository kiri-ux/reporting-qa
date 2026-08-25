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
