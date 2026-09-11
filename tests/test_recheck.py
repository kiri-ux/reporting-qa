"""Re-checking reports when the checking code changes.

The failure this prevents: findings are written once and stored, so a deploy
that fixes a rule leaves every report already on the board showing the old
answer. The two things it must not do while fixing that are lose somebody's
acceptance and lose somebody's sign-off.
"""
import datetime as dt

import pytest

from app.recheck import _new_failures, remap_acks


def _f(code, title, sev="fail"):
    return {"code": code, "title": title, "severity": sev, "detail": ""}


# --------------------------------------------------------------- acceptances
def test_an_acceptance_follows_its_finding_when_the_list_shifts():
    """Acks were stored as indexes. When a finding disappears the indexes move,
    and an automatic sweep would slide the tick onto the finding below it."""
    old = [_f("a", "CTR excludes CTV", "info"), _f("b", "Device over"),
           _f("c", "Line items short")]
    new = [_f("b", "Device over"), _f("c", "Line items short")]
    # they accepted "Device over", at index 1 of the old list
    assert remap_acks(old, [1], new) == [0]


def test_an_acceptance_of_a_finding_that_is_gone_drops_off():
    old = [_f("a", "Top-line CTR does not match its own numbers")]
    new = [_f("b", "Device breakout exceeds what was served")]
    assert remap_acks(old, [0], new) == []


def test_accepting_one_row_does_not_accept_its_siblings():
    """A report carries four "Row CTR does not match" findings. The code alone
    is too coarse to re-map by - the title carries which row."""
    old = [_f("row_ctr", 'Row CTR: "nypost.com"', "warn"),
           _f("row_ctr", 'Row CTR: "tmz.com"', "warn")]
    new = list(old)
    assert remap_acks(old, [0], new) == [0]


def test_nothing_accepted_stays_nothing():
    assert remap_acks([_f("a", "x")], [], [_f("a", "x")]) == []


def test_acceptances_survive_a_recheck_that_changes_nothing():
    old = [_f("a", "one"), _f("b", "two")]
    assert remap_acks(old, [0, 1], list(old)) == [0, 1]


# ------------------------------------------------------------------ sign-off
def test_a_failure_that_was_not_there_before_is_reported():
    old = [_f("a", "one")]
    new = [_f("a", "one"), _f("b", "two")]
    assert _new_failures(old, [], new) == ["two"]


def test_a_failure_that_was_already_there_is_not_new():
    old = [_f("a", "one")]
    assert _new_failures(old, [], [_f("a", "one")]) == []


def test_a_warning_appearing_is_not_a_new_failure():
    """A sign-off is reset only for something that would have changed the
    verdict. A new warning does not empty somebody's review."""
    assert _new_failures([], [], [_f("b", "two", "warn")]) == []


def test_findings_going_away_is_not_a_new_failure():
    """The whole point of the sweep is fixed rules dropping their old findings.
    That must never reset a sign-off."""
    old = [_f("a", "one"), _f("b", "two")]
    assert _new_failures(old, [], [_f("a", "one")]) == []


# --------------------------------------------------------------- fingerprint
def test_the_rules_version_is_derived_from_the_source():
    """A number somebody has to remember to bump is forgotten on exactly the
    deploy that most needed it."""
    from app import version
    a = version.rules_fingerprint()
    assert a and len(a) == 16
    assert a == version.rules_fingerprint()      # stable within a build


def test_changing_a_check_changes_the_fingerprint(tmp_path, monkeypatch):
    import hashlib
    from pathlib import Path
    from app import version

    d = tmp_path / "checks"
    d.mkdir()
    (d / "rules.py").write_text("x = 1\n")

    def fake_fp():
        h = hashlib.sha256()
        for name in sorted(p.name for p in d.glob("*.py")):
            h.update(name.encode())
            h.update((d / name).read_bytes())
        return h.hexdigest()[:16]

    before = fake_fp()
    (d / "rules.py").write_text("x = 2\n")
    assert fake_fp() != before


# ------------------------------------------------------------- the whole loop
@pytest.fixture()
def live(tmp_path):
    """A real database, a real PDF, one real report.

    Built on its own engine rather than by reloading app.config and app.db.
    Reloading rebinds the settings object while every module that did
    "from .config import settings" keeps the old one, and the tests that run
    afterwards fail in ways that have nothing to do with them.
    """
    import shutil
    from pathlib import Path
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import db as dbm

    engine = create_engine(f"sqlite:///{tmp_path/'t.db'}")
    dbm.Base.metadata.create_all(engine)

    src = Path(__file__).parent / "fixtures" / "centre_hills.pdf"
    if not src.exists():
        pytest.skip("fixture missing")
    dst = tmp_path / "centre_hills.pdf"
    shutil.copy(src, dst)

    s = sessionmaker(bind=engine)()
    rep = dbm.Report(batch_id=1, period="2026-07", client="Centre Hills",
                     account_ids="1", market="7 Mountains PA State College",
                     filename="centre_hills.pdf", stored_path=str(dst),
                     severity="pass", findings=[], checks=[], acked=[],
                     review_state="reviewed", reviewed_by="kiri",
                     reviewed_at=dt.datetime(2026, 8, 24),
                     rules_version="an-older-build")
    s.add(rep); s.commit()
    yield s, rep, dbm
    s.close()
    engine.dispose()


def test_a_stale_report_is_found_and_rechecked(live):
    from app.recheck import recheck, stale_count
    from app.version import rules_version
    s, rep, _dbm = live

    assert stale_count(s) == 1
    out = recheck(s, rep)
    assert out["ok"]
    assert rep.rules_version == rules_version()
    assert stale_count(s) == 0
    assert any(f["code"] == "device_over" for f in rep.findings)


def test_a_new_failure_resets_a_sign_off(live):
    """They signed off on an answer with no failures. The re-check finds one."""
    from app.recheck import recheck
    s, rep, _ = live
    assert rep.review_state == "reviewed"
    out = recheck(s, rep)
    assert out["signoff_reset"] is True
    assert rep.review_state == "new"
    assert "Device breakout exceeds what was served" in out["new_failures"]


def test_a_recheck_finding_nothing_new_keeps_the_sign_off(live):
    """The common case after a rule is fixed: same answer, or a shorter one.
    Emptying every reviewer's sign-off for that would make the sweep hostile."""
    from app.recheck import recheck
    s, rep, _ = live
    recheck(s, rep)                      # first pass finds device_over
    rep.review_state = "reviewed"
    rep.reviewed_at = dt.datetime(2026, 8, 24)
    s.commit()
    out = recheck(s, rep)                # nothing has changed since
    assert out["signoff_reset"] is False
    assert rep.review_state == "reviewed"


def test_a_missing_pdf_is_stamped_so_the_sweep_does_not_loop(live):
    from app.recheck import recheck, stale_count
    s, rep, _ = live
    rep.stored_path = "/nowhere/gone.pdf"
    rep.rules_version = "an-older-build"
    s.commit()
    out = recheck(s, rep)
    assert out["ok"] is False
    assert stale_count(s) == 0


def test_the_automatic_sweep_reads_signed_off_reports_too(live):
    """A change of mind twice over, and worth saying why.

    It used to sweep them, then stopped: with a rule changing several times a
    day it re-read finished work over and over, and every pass that found a new
    failure pulled somebody's sign-off.

    That was the wrong half to leave out. Signed off is what has GONE TO THE
    PARTNER, so a check added afterward reached no report a client was holding.
    A new failure on a delivered one now has somewhere to go - Report review -
    instead of dropping back into the pile waiting to be read.
    """
    from app.recheck import stale_count, sweep_once
    s, rep, _ = live                       # the fixture report is reviewed
    # Named rather than left to the automatic window: the sweep covers the
    # cycle being worked, and the fixture is a month that has shipped.
    assert stale_count(s, period=rep.period) == 1
    assert sweep_once(s, limit=8, scoped=False, period=rep.period) == 1, \
        "finished work is read too"
    assert stale_count(s, period=rep.period) == 0


def test_sweep_once_works_through_the_ones_still_open(live):
    from app.recheck import stale_count, sweep_once
    s, rep, _ = live
    rep.review_state = "new"
    s.commit()
    assert sweep_once(s, limit=8, scoped=False, period=rep.period) == 1
    assert stale_count(s, period=rep.period) == 0


# --------------------------------------------------------- scope and pacing
def test_the_sweep_rests_no_longer_than_it_worked():
    """The rest is proportional to the work, never a flat wait: the old pacing
    rested twenty seconds after four seconds of work and turned a ten-minute
    job into two hours. Smaller batches now, so the pauses come more often and
    the board is never waiting on twenty-five PDFs in a row."""
    from app import recheck as rc
    assert rc.BATCH <= 10
    assert not hasattr(rc, "PAUSE_SECONDS")


def test_only_one_sweeper_runs_across_both_workers():
    """Each gunicorn worker starts one, and two streams of pdftotext against a
    box that is also serving the board is most of the way to the dashboard
    hanging. The claim is a row, because a lock one worker holds means nothing
    to the other."""
    import datetime as dt
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, RecheckJob
    from app.recheck import SWEEP_KEY, _claim, _release

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    assert _claim(s, SWEEP_KEY) is True
    assert _claim(s, SWEEP_KEY) is False          # the other worker stands down
    _release(s, SWEEP_KEY)
    assert _claim(s, SWEEP_KEY) is True           # and can take it next time

    # A claim left behind by a killed process expires rather than blocking the
    # sweep until somebody notices.
    row = s.query(RecheckJob).filter_by(key=SWEEP_KEY).one()
    row.updated_at = dt.datetime.utcnow() - dt.timedelta(hours=2)
    s.commit()
    assert _claim(s, SWEEP_KEY) is True


def test_the_automatic_sweep_only_covers_the_cycle_being_worked():
    """It covered three cycles, which on a board of twelve hundred reports is a
    queue of two and a half thousand - twice the work in front of anybody, and
    most of it months that already shipped. A finding on a cycle that shipped in
    March is not in anybody's way; that board still has its own button."""
    from app.config import Settings
    from app.recheck import recent_periods
    n = Settings.model_fields["recheck_periods"].default
    assert n == 1
    # The current cycle, plus the pinned one while the new month is empty.
    assert len(recent_periods(n)) <= 2


def test_the_pinned_period_is_always_swept_even_if_it_has_aged_out():
    """The board opens on it, so a stale answer there is the most visible one
    there is."""
    from app.recheck import recent_periods
    from app.config import settings
    if settings.default_period:
        assert settings.default_period in recent_periods(1)


def test_an_on_demand_run_ignores_the_recent_cycle_limit(live):
    """The scope exists to keep the automatic sweep cheap. Asking for a partner
    by hand is a different question and must reach any month."""
    from app.recheck import _stale_batch
    s, rep, _ = live
    rep.period = "2019-03"                    # far outside the sweep's window
    s.commit()
    assert _stale_batch(s, 25, scoped=True) == []
    assert len(_stale_batch(s, 25, scoped=False)) == 1


def test_a_group_scoped_run_covers_every_market_in_that_group(live):
    from app.board import market_names_for_group
    from app.db import Partner
    s, rep, _ = live
    s.add(Partner(partner="7 Mountains PA State College",
                  group="7 Mountains PA", buyer="x"))
    s.add(Partner(partner="7 Mountains PA Altoona",
                  group="7 Mountains PA", buyer="x"))
    s.commit()
    markets = market_names_for_group(s, "7 Mountains PA")
    assert "7 Mountains PA State College" in markets
    assert "7 Mountains PA Altoona" in markets


def test_stale_count_can_be_asked_about_one_partner(live):
    from app.recheck import stale_count
    s, rep, _ = live
    assert stale_count(s, period="2026-07") == 1
    assert stale_count(s, period="2026-07",
                       group="7 Mountains PA State College") == 1
    assert stale_count(s, period="2026-07", group="Somebody Else") == 0


def test_a_second_job_for_the_same_scope_does_not_start_twice(live):
    """Two clicks must not put two threads on the same reports."""
    import datetime as _dt
    from app.db import RecheckJob
    from app.recheck import start_job
    s, _rep, _ = live
    s.add(RecheckJob(key="k", state="running", done=3, total=9,
                     started_at=_dt.datetime.utcnow(),
                     updated_at=_dt.datetime.utcnow()))
    s.commit()
    assert start_job(s, "k")["done"] == 3


def test_a_stalled_job_can_be_restarted(live):
    """A job that died left "0 of 6" on screen forever, and pressing the button
    again did nothing because a row said it was still running."""
    import datetime as _dt
    from app.db import RecheckJob
    from app.recheck import start_job
    s, _rep, _ = live
    old = _dt.datetime.utcnow() - _dt.timedelta(minutes=10)
    row = RecheckJob(key="k2", state="running", done=0, total=6,
                     started_at=old, updated_at=old)
    s.add(row); s.commit()
    assert row.stalled is True
    start_job(s, "k2", period="2026-07")
    assert row.done == 0 and row.total >= 0 and row.state == "running"
    assert row.stalled is False          # restarted, so the clock reset


def test_a_running_job_is_visible_to_the_other_worker(live):
    """Held in process memory it was not - press the button, land on the other
    gunicorn worker, and the card showed no job at all."""
    import datetime as _dt
    from app.db import RecheckJob
    from app.recheck import running_jobs
    s, _rep, _ = live
    s.add(RecheckJob(key="2026-07:Acme", partner_group="Acme", period="2026-07",
                     state="running", done=2, total=5,
                     started_at=_dt.datetime.utcnow(),
                     updated_at=_dt.datetime.utcnow()))
    s.commit()
    jobs = running_jobs(s)
    assert jobs["2026-07:Acme"]["done"] == 2
    assert jobs["2026-07:Acme"]["group"] == "Acme"


def test_a_partner_run_covers_every_report_not_only_the_stale_ones(live):
    """"Re-check 2" on a card headed "14 reports" reads as a bug. The button
    now means "make this partner right", which is all of them."""
    from app.recheck import _stale_batch, stale_count
    from app.version import rules_version
    s, rep, _ = live
    rep.rules_version = rules_version()          # already current
    s.commit()

    assert stale_count(s, period="2026-07") == 0
    assert stale_count(s, period="2026-07", stale_only=False) == 1
    assert len(_stale_batch(s, 25, scoped=False, stale_only=False)) == 1


def test_a_run_over_everything_walks_forward_instead_of_looping(live):
    """A stale-only run shrinks its own queue as it goes. A run over everything
    does not - a re-checked report still matches - so it pages by id."""
    from app.recheck import _stale_batch
    s, rep, _ = live
    first = _stale_batch(s, 25, scoped=False, stale_only=False)
    assert first and first[0].id == rep.id
    assert _stale_batch(s, 25, scoped=False, stale_only=False,
                        after=rep.id) == []


# ------------------------------------------------- the two findings differ
def test_the_impressions_and_clicks_findings_carry_different_working():
    """Both showed the same Investigate panel, which is no help at all when the
    question is which of the two numbers is wrong."""
    from app.checks.rules import check_line_items
    text = ("LINE ITEMS - PAGE 1\n"
            "Line Item Performance\n"
            "Line Item Name              Impressions   Clicks   CTR\n"
            "Acme - Auto Loans/Banking       64,242   500   0.78%\n"
            "Acme - AI CTV                   36,057   900   2.50%\n")
    out = check_line_items({"text": text, "imps": 500000.0, "clicks": 100.0})
    assert len(out) == 2
    labels = [[t["label"] for t in f["trace"]] for f in out]
    assert labels[0] != labels[1]
    assert "Their impressions" in labels[0] and "Their clicks" in labels[1]
    assert "Largest line items" in labels[0]
    assert "Left unexplained" in labels[1]


def test_a_remainder_worth_a_look_but_not_a_failure_is_a_warning():
    """Between one percent of the tile and five: not a rounding difference, not
    a line item missing from the pull either."""
    from app.checks.rules import check_line_items
    text = ("Line Item Performance\n"
            "Line Item Name        Impressions   Clicks   CTR\n"
            "Acme - Auto Loans        64,242   600   0.93%\n"
            "Acme - AI CTV            36,057   111   0.31%\n"
            "Acme - Facebook          61,790   2,500   4.05%\n")
    out = check_line_items({"text": text, "imps": 162089.0, "clicks": 3050.0})
    f = next(x for x in out if "clicks" in x["code"])
    assert f["severity"] == "warn"
    assert "leaves 50" in f["detail"]


def test_a_remainder_under_one_percent_of_the_tile_says_nothing():
    """WHICH LINES THE TILE EXCLUDES IS A JUDGEMENT.

    "Retargeting Social Mirror OTT" is a Social Mirror line with an OTT
    placement, and nothing in the PDF says whether the Clicks tile leaves it
    out - so the remainder never lands on nought. WVU Parkersburg came out at
    52 clicks against a tile of 39,566 and warned about it every month."""
    from app.checks.rules import check_line_items
    text = ("Line Item Performance\n"
            "Line Item Name        Impressions   Clicks   CTR\n"
            "Acme - Auto Loans        64,242   500   0.78%\n"
            "Acme - AI CTV            36,057   111   0.31%\n"
            "Acme - Facebook          61,790   2,500   4.05%\n")
    out = check_line_items({"text": text, "imps": 162089.0, "clicks": 3008.0})
    assert not [x for x in out if "clicks" in x["code"]]


def test_an_exact_match_after_the_exclusion_is_expected_and_silent():
    from app.checks.rules import check_line_items
    text = ("Line Item Performance\n"
            "Line Item Name        Impressions   Clicks   CTR\n"
            "Acme - Auto Loans        64,242   500   0.78%\n"
            "Acme - AI CTV            36,057   103   0.31%\n"
            "Acme - Facebook          61,790   2,500   4.05%\n")
    out = check_line_items({"text": text, "imps": 162089.0, "clicks": 3000.0})
    f = next(x for x in out if "clicks" in x["code"])
    assert f["severity"] == "info" and "all 103" in f["detail"]


def test_the_trace_names_the_lines_that_were_taken_out():
    """A total on its own says "trust me". Eight clicks are only findable if
    you can see which lines were excluded and for how much."""
    from app.checks.rules import check_line_items
    text = ("Line Item Performance\n"
            "Line Item Name        Impressions   Clicks   CTR\n"
            "Acme - Auto Loans        64,242   500   0.78%\n"
            "Acme - AI CTV            36,057   103   0.31%\n"
            "Acme - Retargeting OTT    5,000     8   0.16%\n"
            "Acme - Facebook          61,790   2,500   4.05%\n")
    out = check_line_items({"text": text, "imps": 167089.0, "clicks": 2900.0})
    f = next(x for x in out if "clicks" in x["code"])
    named = next(t["value"] for t in f["trace"] if t["label"] == "Which lines those are")
    assert "AI CTV: 103" in named and "Retargeting OTT: 8" in named


def test_a_remainder_that_matters_is_still_a_failure():
    from app.checks.rules import check_line_items
    text = ("Line Item Performance\n"
            "Line Item Name        Impressions   Clicks   CTR\n"
            "Acme - Auto Loans        64,242   500   0.78%\n"
            "Acme - AI CTV            36,057   111   0.31%\n"
            "Acme - Facebook          61,790   2,500   4.05%\n")
    out = check_line_items({"text": text, "imps": 162089.0, "clicks": 2000.0})
    f = next(x for x in out if "clicks" in x["code"])
    assert f["severity"] == "fail"
    assert "unaccounted for" in f["detail"]


def test_youtube_clicks_are_not_taken_out_of_the_clicks_tile():
    """The footnote says the CTR excludes YouTube. It says nothing about the
    Clicks tile, and Service One settled it: line items 3,111, tile 3,008, and
    the CTV and OTT lines carry exactly 103. The YouTube+ line carries the
    other 8 and is plainly in the tile."""
    from app.checks.rules import check_line_items
    text = ("Line Item Performance\n"
            "Line Item Name                       Impressions   Clicks   CTR\n"
            "Acme - Auto Loans/Car Financing YouTube+   64,242     8   0.01%\n"
            "Acme - Facebook/Instagram                  61,790   714   1.16%\n"
            "Acme - Personal Finance Behavioral CTV     36,314    61   0.17%\n"
            "Acme - AI CTV                              36,057    41   0.11%\n"
            "Acme - Retargeting Amazon CTV               7,612     1   0.01%\n"
            "Acme - Dynamic PPC                          4,331  2286  52.78%\n")
    out = check_line_items({"text": text, "imps": 210346.0, "clicks": 3008.0})
    f = next(x for x in out if "clicks" in x["code"])
    assert f["severity"] == "info", f["detail"]
    excl = next(t["value"] for t in f["trace"]
                if t["label"] == "Clicks on CTV and OTT line items")
    assert excl == "103"
    named = next(t["value"] for t in f["trace"] if t["label"] == "Which lines those are")
    assert "YouTube" not in named


def test_the_ctr_side_still_excludes_youtube():
    """Two different filters. The footnote is explicit about the CTR one."""
    from app.checks.rules import CLICKS_EXCLUDED, CTR_EXCLUDED
    assert CTR_EXCLUDED.search("Acme - AI YouTube+")
    assert not CLICKS_EXCLUDED.search("Acme - AI YouTube+")
    assert CTR_EXCLUDED.search("Acme - Performance Max")
    assert not CLICKS_EXCLUDED.search("Acme - Performance Max")
    for both in ("Acme - AI CTV", "Acme - Retargeting OTT"):
        assert CTR_EXCLUDED.search(both) and CLICKS_EXCLUDED.search(both)


# ------------------------------------------------------------- the pulled sign-off
#
# "Why does this one have a k if it's not reviewed?" - because a re-check found
# a new failure, pulled the sign-off, and left the reviewer's name printed
# beside a report in the unreviewed state. The name has to survive (somebody
# has to be told whose sign-off went) but it must not read as a sign-off.
import datetime as _dt

from app.db import Report as _Report


def _signed(name="k", state="reviewed"):
    r = _Report(client="Awaken Bakery", filename="x.pdf", period="2026-07")
    r.review_state = state
    r.reviewed_by = name
    r.reviewed_at = _dt.datetime(2026, 8, 20)
    return r


def test_a_standing_signoff_shows_the_name():
    assert _signed().signed_off_by == "k"
    assert _signed(state="waived").signed_off_by == "k"
    assert _signed(state="needs_fix").signed_off_by == "k"


def test_a_pulled_signoff_shows_no_name():
    r = _signed()
    r.review_state = "new"
    r.reviewed_at = None
    r.signoff_cleared_at = _dt.datetime(2026, 8, 25)
    assert r.signed_off_by == ""
    assert "k signed this off" in r.signoff_pulled


def test_a_report_nobody_ever_signed_says_nothing():
    r = _Report(client="x", filename="x.pdf", period="2026-07")
    assert r.signed_off_by == "" and r.signoff_pulled == ""


def test_the_recheck_marks_the_pull_rather_than_erasing_who():
    from app.recheck import _new_failures
    old = [{"code": "a", "title": "One"}]
    new = [{"code": "a", "title": "One"}, {"code": "b", "title": "Two",
                                           "severity": "fail"}]
    assert _new_failures(old, [], new) == ["Two"]


# ------------------------------------------- the partner button skips sign-offs
def test_the_partner_recheck_query_leaves_signed_off_reports_alone():
    """It said "6 of 8" on a partner with one report still pending, and worked
    through six somebody had already read and signed."""
    import inspect
    from app import recheck as rmod
    src = inspect.getsource(rmod._stale_query)
    assert "skip_signed" in src
    assert 'notin_(("reviewed", "waived"))' in src
    assert "skip_signed" in inspect.signature(rmod.start_job).parameters


def test_the_sweep_and_the_button_now_agree():
    """Both cover everything judged by older code, signed off or not. What the
    banner counts is what the sweep will read."""
    import inspect
    from app import recheck as rmod
    assert "skip_signed" not in inspect.getsource(rmod.sweep_once)


def test_the_order_import_fingerprint_covers_the_import_rules_too():
    """It was just the product mapping, which was too narrow by exactly the bug
    it was written for. orders_io.py holds the rule that a live line item
    rescues an order whose header says IO Pending Launch - order 55216."""
    import hashlib
    from pathlib import Path
    from app.version import product_map_version

    before = product_map_version()
    here = Path(rmod_path()).parent
    # Changing orders_io.py must move the fingerprint.
    src = here / "orders_io.py"
    original = src.read_bytes()
    try:
        src.write_bytes(original + b"\n# touched\n")
        assert product_map_version() != before
    finally:
        src.write_bytes(original)
    assert product_map_version() == before


def rmod_path():
    from app import version
    return version.__file__


# ------------------------------------------- the amber dot that never cleared
def test_the_stale_count_and_the_button_cover_the_same_reports():
    """It kept turning amber after a re-check, because the button and the count
    covered different populations. They cover the same one: everything judged
    by older code, signed off or not."""
    import inspect
    from app import main as mmod
    src = inspect.getsource(mmod._stale_here)
    assert "case((stale, 1), else_=0)" in src
    assert "case((signed, 0), (stale, 1), else_=0)" not in src


def test_the_deliberate_pass_covers_only_the_signed_off_ones(live):
    """The sweep reads everything now, but the narrowed passes still exist -
    signed_only for a deliberate look at finished work, skip_signed for a
    button that means "what is still in the way"."""
    from app.recheck import stale_count
    s, rep, dbm = live                     # reviewed, stamped with older code
    s.add(dbm.Report(batch_id=1, period="2026-07", client="Still Open",
                     account_ids="2", market=rep.market, filename="x.pdf",
                     stored_path="", severity="pass", findings=[], checks=[],
                     acked=[], review_state="new", rules_version="older"))
    s.commit()

    assert stale_count(s) == 2                             # what the sweep does
    assert stale_count(s, skip_signed=True) == 1           # still open only
    assert stale_count(s, signed_only=True) == 1           # finished work only


def test_the_sweep_covers_signed_off_reports():
    """It used to. Then it stopped, because with a rule changing several times
    a day, re-reading finished work meant the queue never emptied and every
    pass that found a new failure pulled somebody's sign-off - the board kept
    un-reviewing itself.

    That was the wrong half to leave out. Signed off is what has GONE TO THE
    PARTNER, so a check added afterwards - the CTV completion tile is the one
    that made this obvious - reached no report a client was actually holding.
    On a cycle of 1,222 reports with 1,206 signed off, the sweep covered 16.

    What makes it bearable is that a new failure on a delivered report now has
    somewhere to go: it is marked for resending and shows as Report review,
    rather than dropping back into the pile waiting to be read."""
    import inspect
    from pathlib import Path

    from app import main, recheck

    sweep = inspect.getsource(recheck.sweep_once)
    assert "skip_signed" not in sweep, "the sweep reads finished work too"
    # The number on the banner has to be what the sweep will actually do, and
    # Skip this re-check has to stamp everything the sweep would have read - or
    # it stops the sweep and leaves it a queue.
    assert "skip_signed=True" not in Path("app/recheck.py").read_text()
    assert "skip_signed=True" not in Path("app/main.py").read_text()
    assert "skip_signed=True" not in inspect.getsource(recheck.skip_the_sweep)
    # The partner button too: "bring this partner up to date" has to mean the
    # copies the partner is holding.
    assert "skip_signed=False" in inspect.getsource(main.cycle_recheck)


def test_a_job_whose_process_died_stops_claiming_to_be_running():
    """The work happens in a thread and a deploy takes the thread with it, so
    the row said "running" and the card sat at "52 of 93" for an hour with a
    spinner on it - which reads as the tool being stuck rather than as the job
    having been killed."""
    import datetime as dt
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.db import Base, RecheckJob
    from app.recheck import running_jobs

    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    s = sessionmaker(bind=eng)()
    old = RecheckJob(key="g:7 Mountains PA", partner_group="7 Mountains PA",
                     state="running", total=93, done=52,
                     started_at=dt.datetime.utcnow() - dt.timedelta(hours=1),
                     updated_at=dt.datetime.utcnow() - dt.timedelta(hours=1))
    live = RecheckJob(key="g:Other", partner_group="Other", state="running",
                      total=10, done=3, started_at=dt.datetime.utcnow(),
                      updated_at=dt.datetime.utcnow())
    s.add_all([old, live]); s.commit()

    jobs = running_jobs(s)
    assert "g:Other" in jobs and "g:7 Mountains PA" not in jobs
    s.expire_all()
    dead = s.query(RecheckJob).filter_by(key="g:7 Mountains PA").one()
    assert dead.state == "stopped" and "52 of 93" in dead.note


def test_a_recheck_works_out_the_orders_again(tmp_path, monkeypatch):
    """A re-check wrote back findings and left the order ids exactly as the
    import first read them, so fixing the rule that decides which orders belong
    to a client reached no stored report at all. Z90's report kept carrying
    51666 51923 53511 54820 54822 54824 55200 - one of them Z90's, and 55200
    belonging to Excel Summer-Fall 2026 at Red Pony Marketing - and went on
    being paced against all seven."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'n.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    db = db_mod.SessionLocal()
    OrderLine, Report = db_mod.OrderLine, db_mod.Report

    db.add(OrderLine(market="Local Media San Diego",
                     client="LMSD - Z90 Secret Contest 2026",
                     account_ids="54824", product="Social Mirror",
                     starts_on=dt.date(2026, 7, 1), ends_on=dt.date(2026, 9, 30),
                     live=True))
    db.add(OrderLine(market="Red Pony Marketing", client="Excel Summer-Fall 2026",
                     account_ids="55200", product="Meta",
                     starts_on=dt.date(2026, 7, 1), ends_on=dt.date(2026, 9, 30),
                     live=True))
    db.commit()

    b = db_mod.Batch(market="Local Media San Diego", period="2026-08")
    db.add(b); db.flush()
    rep = Report(batch_id=b.id, filename="x.pdf",
                 client="LMSD - Z90 Secret Contest 2026", period="2026-08", market="Local Media San Diego", findings=[],
                 severity="pass",
                 account_ids="51666 51923 53511 54820 54822 54824 55200")
    db.add(rep); db.commit()

    from app.naming import rebuild_ids
    assert rebuild_ids(db, rep) == "54824"

    # A client that is not on the order list keeps the ids it has - a blank
    # name is worse than a stale one.
    other = Report(client="Nobody At All", period="2026-08", findings=[],
                   severity="pass", account_ids="12345", filename="y.pdf")
    assert rebuild_ids(db, other) == ""

    # And the re-check is what applies it.
    import inspect
    from app import recheck as rmod
    src = inspect.getsource(rmod.recheck)
    assert "rebuild_ids(db, rep)" in src
    assert src.index("rebuild_ids(db, rep)") < src.index("client_flight(db")


def test_a_check_can_be_switched_off_without_re_reading_the_board(tmp_path,
                                                                  monkeypatch):
    """The two halves of the ask, in one test.

    Every edit to any rule put all seven hundred reports in the queue to be
    judged again, and there was no way for the person reading the findings to
    stop a check she did not want. Both of those are the same fact: the
    fingerprint is a hash of the checking code, so turning a check off was the
    single most expensive thing anybody could do.

    Off has to be free. Nothing needs re-reading - a finding from a check that
    is off stops counting where it stands - so the reports are re-stamped with
    the new hash and the sweep has nothing to do. On is not free, and should
    not be: that rule never ran on any of them.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'off.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    from app import checkctl
    from app.version import forget_fingerprint, rules_version
    checkctl.refresh()
    forget_fingerprint()

    db = db_mod.SessionLocal()
    was = rules_version()
    rep = db_mod.Report(
        batch_id=1, period="2026-08", client="Acme", filename="a.pdf",
        stored_path=str(tmp_path / "a.pdf"), severity="fail",
        rules_version=was,
        findings=[{"code": "social_mirror_ad_size", "severity": "fail",
                   "title": "2 Social Mirror creatives named with an ad size",
                   "detail": "", "check": "check_social_mirror_sizes"}],
        checks=[{"key": "check_social_mirror_sizes", "label": "x",
                 "state": "failed", "count": 1}], acked=[])
    db.add(rep)
    db.commit()
    assert rep.open_findings
    assert rep.effective_severity == "fail"

    # OFF. The finding stops counting immediately, and nothing is left behind.
    checkctl.set_check(db, "check_social_mirror_sizes", False, who="kiri")
    db.expire_all()
    rep = db.get(db_mod.Report, rep.id)
    assert not rep.open_findings
    assert rep.effective_severity == "pass"
    now = rules_version()
    assert now != was, "the rules did change - one of them is not running"
    assert rep.rules_version == now, "nothing to re-read, so nothing is behind"

    # AND EDITING IT COSTS NOTHING. The source of a check that is off is not
    # part of the hash, so a change to it queues no reports at all.
    from pathlib import Path

    from app import version as ver
    src = Path(ver.__file__).resolve().parent / "checks" / "quality.py"
    text = src.read_text(encoding="utf-8")
    edited = text.replace('"""Social Mirror creative names should not carry an '
                          'ad size."""',
                          '"""Social Mirror creative names should not carry an '
                          'ad size. Edited."""')
    assert edited != text
    src.write_text(edited, encoding="utf-8")
    try:
        forget_fingerprint()
        assert rules_version() == now
    finally:
        src.write_text(text, encoding="utf-8")
        forget_fingerprint()

    # BACK ON COSTS A RE-CHECK, and it should: that rule did not run on
    # anything judged while it was off.
    checkctl.set_check(db, "check_social_mirror_sizes", True, who="kiri")
    db.expire_all()
    rep = db.get(db_mod.Report, rep.id)
    assert rep.open_findings
    assert rep.rules_version != rules_version()
    db.close()


def test_an_old_finding_is_matched_back_to_its_check_by_code(tmp_path,
                                                             monkeypatch):
    """Findings stored before this stamp nothing about who wrote them, and
    there are hundreds of thousands of them. The code is matched back to the
    checks that can emit it - and only hidden when every one of them is off,
    because hiding a finding a live check would still raise is the mistake here
    that costs something."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'old.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    from app import checkctl
    checkctl.refresh()

    owners = checkctl.code_owners()
    assert owners["social_mirror_ad_size"] == {"check_social_mirror_sizes"}
    # Nothing raised outside a check function is ever hidden.
    assert "rule_error" not in owners

    db = db_mod.SessionLocal()
    checkctl.set_check(db, "check_social_mirror_sizes", False)
    old = {"code": "social_mirror_ad_size", "severity": "fail", "title": "x"}
    assert checkctl.finding_is_off(old)
    assert not checkctl.finding_is_off({"code": "rule_error", "severity": "warn"})
    checkctl.set_check(db, "check_social_mirror_sizes", True)
    assert not checkctl.finding_is_off(old)
    db.close()


def test_a_switched_off_check_does_not_run_and_says_so(monkeypatch):
    """Off means the rule does not run - and its row stays on the report's own
    checklist saying it did not, because a check that quietly stops happening is
    how nobody notices for a month."""
    from pathlib import Path

    import app.checkctl as ctl
    from app.checks import rules as rules_mod

    calls = []

    def check_never_runs(_ctx):
        calls.append("ran")
        return []

    def check_still_runs(_ctx):
        calls.append("other")
        return []

    monkeypatch.setattr(ctl, "switched_off",
                        lambda: frozenset({"check_never_runs"}))
    monkeypatch.setattr(rules_mod, "CHECKS",
                        [(check_never_runs, "never"),
                         (check_still_runs, "still")])

    pdf = sorted((Path(__file__).resolve().parent / "fixtures").glob("*.pdf"))[0]
    out = rules_mod.run_all(pdf)
    states = {c["key"]: c["state"] for c in out["checks"]}
    assert states == {"check_never_runs": "off", "check_still_runs": "passed"}
    assert calls == ["other"]


def test_nothing_re_checks_itself_on_its_own():
    """The automatic sweep is gone, and it was asked for. A rule changing put
    every report on the board in a queue that ran for hours on its own
    schedule, while the person who needed an answer this morning watched a
    number that was not moving.

    Re-checking is pressed now: Run against one check, or Run all. The order
    re-read stays automatic - the product checks stand down entirely until it
    has happened, and nobody would know to press it.
    """
    import inspect

    from app import recheck

    src = inspect.getsource(recheck.start_sweeper)
    assert "_remap_orders_if_stale()" in src
    assert "sweep_once(" not in src
    # And the switch that used to hold it is gone with it.
    from app import checkctl
    assert not hasattr(checkctl, "held")
    assert not hasattr(checkctl, "set_hold")


def test_several_checks_can_be_switched_at_once(tmp_path, monkeypatch):
    """Switching four off meant four page loads, and every one of them landed
    back on the other tab - the rules sheet has no script of its own, so the
    tabs are radios and a reload resets them.

    One form round the whole list: a checkbox per row and two submit buttons.
    The per-row switch is a button carrying its own value, which a browser
    sends only when it is the button pressed, so the two do not collide.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'b.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    import app.config
    import app.db
    import app.main
    for m in (app.config, app.db, app.main):
        importlib.reload(m)
    app.db.init_db()
    from fastapi.testclient import TestClient

    from app import checkctl
    checkctl.refresh()
    client = TestClient(app.main.app, follow_redirects=False)

    picked = ["check_date_range", "check_row_math", "check_market_logo"]
    r = client.post("/checks/set", data={"pick": picked, "on": "",
                                         "back": "/rules?tab=flags"})
    assert r.status_code == 303
    # BACK WHERE THE SWITCH WAS. It went to /rules, which opens on What is
    # owed, so switching two off meant finding the tab again in between.
    assert r.headers["location"] == "/rules?tab=flags"
    checkctl.refresh()
    assert checkctl.switched_off() == set(picked)

    # One row's own button switches that one and leaves the ticks alone.
    r = client.post("/checks/set", data={"pick": picked,
                                         "one": "check_row_math|1"})
    assert r.status_code == 303
    checkctl.refresh()
    assert checkctl.switched_off() == {"check_date_range", "check_market_logo"}

    # And back on, all of them.
    client.post("/checks/set", data={"pick": picked, "on": "1"})
    checkctl.refresh()
    assert checkctl.switched_off() == set()

    # A name nobody has heard of does nothing at all.
    client.post("/checks/set", data={"pick": ["check_not_a_thing"], "on": ""})
    checkctl.refresh()
    assert checkctl.switched_off() == set()

    # The page opens on the tab the URL names.
    body = client.get("/rules?tab=flags&frag=1").text
    assert 'id="rtab-flags" checked' in body
    assert 'name="pick"' in body


def test_both_workers_agree_on_the_rules_hash_after_a_switch(tmp_path,
                                                             monkeypatch):
    """The number on the banner sat at 1,415 and did not move, with the sweep
    working the whole time.

    The hash was cached flat, and it stopped being a pure function of the source
    the moment a check could be switched off. One gunicorn worker took the
    click and recomputed; the other went on holding the hash from before the
    switch, stamped every report it re-checked with it, and the first worker
    went on counting those same reports as behind. Neither was wrong about
    anything it could see.

    Cached against the set of switched-off checks instead. checkctl re-reads
    the switches every fifteen seconds, so both workers land on the same answer
    without either of them having to be told.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'w.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    from app import checkctl
    from app import version as ver
    checkctl.refresh()
    ver.forget_fingerprint()

    db = db_mod.SessionLocal()
    was = ver.rules_version()
    checkctl.set_check(db, "check_date_range", False, who="kiri")
    now = ver.rules_version()
    assert now != was

    # THE OTHER WORKER. It never saw the click, and its cache still holds the
    # hash from before it - so it recomputes off the switches it can read.
    ver._FINGERPRINT = was
    ver._FINGERPRINT_FOR = frozenset()
    assert ver.rules_version() == now
    db.close()


def test_a_thread_that_throws_does_not_wedge_the_worker():
    """One flag says this is already going. A throw in it - the order re-read
    is an 850 MB download and the heaviest thing in the file - left it set with
    nothing behind it, and that worker never started another one."""
    import inspect

    from app import recheck

    src = inspect.getsource(recheck.start_sweeper)
    # The flag comes off in a finally, not at each early return.
    assert "finally:\n            _running.clear()" in src
    assert src.count("_running.clear()") == 1


def test_one_check_can_be_run_over_only_the_reports_it_is_about(tmp_path,
                                                                monkeypatch):
    """The sweep gets to everything eventually. Eventually was overnight and
    still going, with the answer wanted this morning.

    A rule about the CTV tile has nothing to say about a report with no CTV on
    it, so the button reads the reports carrying the product rather than the
    twelve hundred on the board. It is a full re-check of those reports, not a
    run of that one rule: the cost is reading the PDF, which happens either
    way, and half-judging a report is how one ends up carrying two builds'
    answers.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'r.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    import app.config
    import app.db
    import app.main
    for m in (app.config, app.db, app.main):
        importlib.reload(m)
    app.db.init_db()

    db = app.db.SessionLocal()
    for i, products in enumerate(["CTV, Display", "Display", "Social Mirror",
                                  "Connected TV (CTV)"]):
        db.add(app.db.Report(batch_id=1, period="2026-08", client=f"C{i}",
                             filename=f"c{i}.pdf", stored_path="",
                             products=products, severity="pass", findings=[],
                             checks=[], acked=[], rules_version="x"))
    db.commit()

    from app.checks.rules import CHECK_PRODUCTS
    from app.recheck import stale_count

    # YouTube is in the CTV scope because a YouTube+ order delivers CTV
    # through its YouTube TV line items, and those rows belong to the YouTube
    # product - see test_the_ctv_run_reads_the_youtube_reports_too.
    assert CHECK_PRODUCTS["check_ctv_tile"] == ("CTV", "YouTube")
    # Two of the four carry CTV. The other two are not read at all.
    assert stale_count(db, period="2026-08", stale_only=False) == 4
    assert stale_count(db, period="2026-08", stale_only=False,
                       products=("CTV",)) == 2
    # A check with nothing written for it runs over the whole cycle, which is
    # slower and never wrong.
    assert CHECK_PRODUCTS.get("check_date_range") is None
    db.close()


def test_the_run_button_is_in_the_same_form_as_the_switches():
    """A browser sends a button's own name and value only when it is the one
    pressed, so Run, the row's switch and the two bulk buttons share one form
    without a line of JavaScript - which the rules sheet could not run anyway,
    being injected as innerHTML."""
    from pathlib import Path

    body = (Path(__file__).resolve().parent.parent / "app" / "templates"
            / "rules_body.html").read_text()
    assert 'name="run" value="{{ c.key }}"' in body
    # One form, and the Run button inside it. A nested form is not valid HTML
    # and the browser drops it.
    assert body.count('<form method="post" action="/checks/set"') == 1
    assert body.index('action="/checks/set"') < body.index('name="run"')
    # A row with a run going stays on screen - Flagging now hides the checks
    # with no count, which is every check that has just been fixed.
    assert "not c.n and not c.job" in body


def test_a_check_about_a_missing_product_is_never_scoped_by_product():
    """The scope is matched against what was DETECTED ON THE REPORT, so a check
    about a product being ABSENT must not be in it.

    "A CTV tile on a report with no CTV" only ever fires on a report with no
    CTV detected. Scoping it to reports carrying CTV would skip every report it
    is about, and the run would come back clean in thirty seconds.
    """
    from app.checks.rules import CHECK_PRODUCTS

    for name in ("check_rogue_ctv", "check_geofence_widget",
                 "check_products", "check_required_widgets",
                 "check_completion_present"):
        assert name not in CHECK_PRODUCTS, name
    # And the ones that are in it read the inside of that product's own widget.
    assert set(CHECK_PRODUCTS) == {"check_ctv_tile", "check_social_mirror_sizes",
                                   "check_creative_shape",
                                   "check_geofence_names"}


def test_the_ctv_scope_matches_both_ctv_products():
    """"CTV" and "Social Mirror CTV" are the two product names carrying CTV
    inventory, and the scope is a substring match, so both are read."""
    import sys

    from app.checks import products as P

    names = set()
    for item in getattr(P, "PRODUCT_LEADS", []):
        names.add(item[0])
    names |= set(getattr(P, "DELIVERS", {}))
    hit = sorted(n for n in names if "CTV" in n)
    assert hit == ["CTV", "Social Mirror CTV"], hit


def test_the_sheet_sends_which_button_was_pressed():
    """EVERY BUTTON IN THE RULES SHEET DID NOTHING, in silence.

    A browser sends a submit button's own name and value only when it is the
    one clicked, which is what lets Run, a row's On/Off and the two bulk
    buttons share one form. `new FormData(form)` does NOT include it - so the
    sheet posted a form saying nothing at all and the server did nothing with
    it. On the full page, where the browser submits for itself, all of it
    worked, which is why it went unnoticed.
    """
    from pathlib import Path

    base = (Path(__file__).resolve().parent.parent / "app" / "templates"
            / "base.html").read_text()
    js = base[base.index("function loadSheet"):]
    js = js[:js.index("function openSheet")]
    assert "new FormData(f)" in js
    assert "e.submitter" in js
    assert "data.append(hit.name" in js
    # e.submitter is not everywhere yet, so the last button pressed is
    # remembered as well.
    assert "_lastHit" in base


def test_a_finished_run_still_says_what_it_did(tmp_path, monkeypatch):
    """A run over a check nobody's reports match finishes before the page comes
    back, so the button reappeared and nothing on screen said it had run - which
    is indistinguishable from the button not working, and that is how it was
    read."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'j.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    from app.recheck import check_jobs

    db = db_mod.SessionLocal()
    db.add(db_mod.RecheckJob(key="check:check_ctv_tile:2026-08", state="done",
                             total=0, done=0, changed=0, period="2026-08"))
    db.add(db_mod.RecheckJob(key="check:check_row_math:2026-08",
                             state="running", total=96, done=12, changed=3,
                             period="2026-08"))
    db.commit()

    got = check_jobs(db, "2026-08")
    assert got["check_ctv_tile"]["state"] == "done"
    assert got["check_ctv_tile"]["total"] == 0
    assert got["check_row_math"] == {"state": "running", "done": 12,
                                     "total": 96, "changed": 3}
    # Another cycle's runs are not this cycle's.
    assert check_jobs(db, "2026-07") == {}
    db.close()

    from pathlib import Path
    body = (Path(__file__).resolve().parent.parent / "app" / "templates"
            / "rules_body.html").read_text()
    assert "no reports to read" in body
    assert "read{% if c.job.changed %}" in body


def test_the_run_scope_reads_the_orders_not_only_the_stored_products(tmp_path,
                                                                     monkeypatch):
    """THE SCOPE WAS BUILT ON THE ANSWER THE FIX HAD JUST CORRECTED.

    A report's product list is what the detector made of it LAST TIME it was
    read. The reports this scope exists for are exactly the ones the detector
    used to get wrong: a report whose CTV prints as OTT carried no CTV product,
    nothing has re-read it since, and the stored list still says so. Scoping on
    that alone skips precisely the reports the check was written for, and comes
    back "nothing to read" in a second - which is what pressing Run did.

    The orders do not have that problem. They are re-imported whenever the
    import code changes, so they say what the client bought today.
    """
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'s.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    import importlib
    from app import config as cfg_mod
    importlib.reload(cfg_mod)
    from app import db as db_mod
    importlib.reload(db_mod)
    db_mod.init_db()
    from app.recheck import stale_count

    db = db_mod.SessionLocal()
    # Bought CTV; the report was last read by the detector that could not see
    # it, so its stored products say nothing about CTV.
    db.add(db_mod.OrderLine(market="M", client="Bought CTV", account_ids="1",
                            product="CTV", starts_on=dt.date(2026, 8, 1),
                            ends_on=dt.date(2026, 9, 30), live=True))
    db.add(db_mod.Report(batch_id=1, period="2026-08", client="Bought CTV",
                         filename="a.pdf", stored_path="",
                         products="Mobile Conquesting, Video", severity="pass",
                         findings=[], checks=[], acked=[], rules_version="old"))
    # Detected CTV, no order loaded for them at all.
    db.add(db_mod.Report(batch_id=1, period="2026-08", client="Shows CTV",
                         filename="b.pdf", stored_path="",
                         products="CTV, Display", severity="pass",
                         findings=[], checks=[], acked=[], rules_version="old"))
    # Neither.
    db.add(db_mod.OrderLine(market="M", client="Display only", account_ids="3",
                            product="Display", starts_on=dt.date(2026, 8, 1),
                            ends_on=dt.date(2026, 9, 30), live=True))
    db.add(db_mod.Report(batch_id=1, period="2026-08", client="Display only",
                         filename="c.pdf", stored_path="", products="Display",
                         severity="pass", findings=[], checks=[], acked=[],
                         rules_version="old"))
    db.commit()

    assert stale_count(db, period="2026-08", stale_only=False) == 3
    # Both halves count, and the third is left alone.
    assert stale_count(db, period="2026-08", stale_only=False,
                       products=("CTV", "YouTube")) == 2
    db.close()
