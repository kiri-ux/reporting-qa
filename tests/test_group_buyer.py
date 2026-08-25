"""Who the group card names as the buyer.

Two faults it fixes, both visible on the July board:

  * Amp Digital Innovations showed "Hanna Walentukonis, Matt Ogden" on its B
    tag, and Matt Ogden is the SEO manager - already on the S tag as "Matt".
    Whichever order line was read first decided the buyer, and one of them was
    the SEO line.
  * ADX Communications showed "Anna Halligan, Bella Duddy", which answers "who
    do I chase" with "work it out yourself".
"""
import datetime as dt
import importlib

import pytest


@pytest.fixture()
def db(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base
    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    engine.dispose()


LIVE = dict(starts_on=dt.date(2026, 1, 1), ends_on=dt.date(2026, 12, 31))


def _line(db, market, client, product, buyer):
    from app.db import OrderLine
    db.add(OrderLine(market=market, client=client, account_ids="1",
                     product=product, buyer=buyer, **LIVE))


def _partner(db, name, buyer, seo=""):
    from app.db import Partner
    db.add(Partner(partner=name, group=name, buyer=buyer, seo=seo,
                   reporting_team="Paulina", trainer="Jennaya"))


def _groups(db):
    from app.board import by_group
    return {g.group: g for g in by_group(db, "2026-07")}


def test_the_seo_manager_is_not_the_buyer(db):
    """Amp Digital Innovations. Matt Ogden owns the SEO line and nothing else."""
    _partner(db, "Amp Digital Innovations", buyer="Hanna Walentukonis", seo="Matt")
    _line(db, "Amp Digital Innovations", "A Co", "Social Mirror", "Hanna Walentukonis")
    _line(db, "Amp Digital Innovations", "A Co", "SEO", "Matt Ogden")
    db.commit()
    g = _groups(db)["Amp Digital Innovations"]
    assert g.buyer == "Hanna Walentukonis"
    assert "Matt Ogden" not in g.buyer
    assert g.seo == "Matt"


def test_the_seo_line_read_first_still_does_not_win(db):
    """The old behaviour depended on row order, which is not a decision."""
    _partner(db, "Amp Digital Innovations", buyer="Hanna Walentukonis", seo="Matt")
    _line(db, "Amp Digital Innovations", "A Co", "SEO", "Matt Ogden")
    _line(db, "Amp Digital Innovations", "A Co", "Social Mirror", "Hanna Walentukonis")
    db.commit()
    assert _groups(db)["Amp Digital Innovations"].buyer == "Hanna Walentukonis"


def test_an_seo_only_client_keeps_its_seo_owner_as_the_buyer(db):
    """There is nobody else to name, and a blank tag is worse than the truth."""
    _partner(db, "Amp Digital Innovations", buyer="", seo="Matt")
    _line(db, "Amp Digital Innovations", "A Co", "SEO", "Matt Ogden")
    db.commit()
    assert _groups(db)["Amp Digital Innovations"].buyer == "Matt Ogden"


def test_two_buyers_collapse_to_the_reporting_breakout(db):
    """ADX Communications. Two campaign managers, one roster answer."""
    _partner(db, "ADX Communications", buyer="Anna Halligan")
    _line(db, "ADX Communications", "A Co", "Display", "Anna Halligan")
    _line(db, "ADX Communications", "B Co", "Display", "Bella Duddy")
    db.commit()
    assert _groups(db)["ADX Communications"].buyer == "Anna Halligan"


def test_one_buyer_is_used_even_when_the_roster_disagrees(db):
    """The roster is the tie-break, not an override. If every line agrees, the
    order is the more current answer."""
    _partner(db, "ADX Communications", buyer="Someone Else")
    _line(db, "ADX Communications", "A Co", "Display", "Anna Halligan")
    _line(db, "ADX Communications", "B Co", "Display", "Anna Halligan")
    db.commit()
    assert _groups(db)["ADX Communications"].buyer == "Anna Halligan"


def test_two_buyers_and_no_roster_entry_still_names_both(db):
    """Nothing to fall back on. Both names beats no name."""
    _line(db, "Nowhere Media", "A Co", "Display", "Anna Halligan")
    _line(db, "Nowhere Media", "B Co", "Display", "Bella Duddy")
    db.commit()
    g = _groups(db)["Nowhere Media"]
    assert "Anna Halligan" in g.buyer and "Bella Duddy" in g.buyer


def test_the_seo_line_does_not_count_towards_two_buyers(db):
    """One real buyer plus an SEO manager is one buyer, not a disagreement."""
    _partner(db, "Amp Digital Innovations", buyer="Roster Person", seo="Matt")
    _line(db, "Amp Digital Innovations", "A Co", "Social Mirror", "Hanna Walentukonis")
    _line(db, "Amp Digital Innovations", "B Co", "SEO", "Matt Ogden")
    db.commit()
    assert _groups(db)["Amp Digital Innovations"].buyer == "Hanna Walentukonis"
