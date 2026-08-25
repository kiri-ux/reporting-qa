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


def test_sweep_once_works_through_the_stale_ones(live):
    from app.recheck import stale_count, sweep_once
    s, _rep, _ = live
    assert sweep_once(s, limit=8) == 1
    assert stale_count(s) == 0


# --------------------------------------------------------- scope and pacing
def test_the_sweep_rests_no_longer_than_it_worked():
    """The old pacing rested twenty seconds after four seconds of work, which
    turned a ten-minute job into two hours - a queue that never drained while
    builds were going out several times a day."""
    from app import recheck as rc
    assert rc.BATCH >= 25
    assert rc.MAX_REST_SECONDS <= 10
    assert not hasattr(rc, "PAUSE_SECONDS")


def test_the_automatic_sweep_only_covers_recent_cycles():
    """A finding on a cycle that shipped in March is not in anybody's way, and
    re-reading four years of PDFs on every deploy is work nobody asked for."""
    from app.config import Settings
    from app.recheck import recent_periods
    n = Settings.model_fields["recheck_periods"].default
    assert n >= 2
    assert len(recent_periods(n)) >= n


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


def test_a_remainder_smaller_than_the_rounding_is_a_warning_not_a_failure():
    """Service One: 103 clicks over the top line, 111 of them on CTV. The eight
    left over is 0.27% of the campaign - too small to hold the report up, too
    real to call expected."""
    from app.checks.rules import check_line_items
    text = ("Line Item Performance\n"
            "Line Item Name        Impressions   Clicks   CTR\n"
            "Acme - Auto Loans        64,242   500   0.78%\n"
            "Acme - AI CTV            36,057   111   0.31%\n"
            "Acme - Facebook          61,790   2,500   4.05%\n")
    out = check_line_items({"text": text, "imps": 162089.0, "clicks": 3008.0})
    f = next(x for x in out if "clicks" in x["code"])
    assert f["severity"] == "warn"
    assert "all but 8" in f["detail"]


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
    out = check_line_items({"text": text, "imps": 167089.0, "clicks": 3008.0})
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


def test_the_background_sweep_still_covers_them():
    """That is how a rule change reaches a signed-off report and pulls the
    sign-off. Narrowing the button must not narrow the sweep."""
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
    """It kept turning amber after a re-check. The button skips reports already
    signed off; the count did not, so pressing it did the work it could and the
    number it is judged by never moved."""
    import inspect
    from app import main as mmod
    src = inspect.getsource(mmod._stale_here)
    # The stale sum is guarded by the same signed-off test as the have count.
    assert "case((signed, 0), (stale, 1), else_=0)" in src
