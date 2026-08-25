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
def live(tmp_path, monkeypatch):
    """A real database, a real PDF, one real report."""
    import importlib
    import shutil
    from pathlib import Path

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import config as cfg
    importlib.reload(cfg)
    from app import db as dbm
    importlib.reload(dbm)
    dbm.init_db()

    src = Path(__file__).parent / "fixtures" / "centre_hills.pdf"
    if not src.exists():
        pytest.skip("fixture missing")
    dst = tmp_path / "centre_hills.pdf"
    shutil.copy(src, dst)

    s = dbm.SessionLocal()
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
