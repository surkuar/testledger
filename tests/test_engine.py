from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import NOW, make_run
from pydantic import ValidationError

from testledger.config import Config
from testledger.ledger import build_ledger
from testledger.models import ChurnRecord, EntryKind, FixCommit, Layer, Outcome, Verdict

P, F = Outcome.PASSED, Outcome.FAILED
UNIT = "com.acme.unit.CalcTest.adds"
UI = "com.acme.ui.CheckoutSeleniumTest.full_flow"


def test_same_sha_rerun_is_flake_and_books_triage(cfg):
    runs = [
        make_run("r1", "sha1", 1, 10, {UI: F, UNIT: P}, duration=60),
        make_run("r1b", "sha1", 2, 10, {UI: P, UNIT: P}, duration=60),
    ] + [make_run(f"r{i}", f"sha{i}", 1, i, {UI: P, UNIT: P}, duration=60) for i in range(2, 8)]
    ledger = build_ledger(runs, cfg, now=NOW)
    ui = ledger.accounts[UI]
    assert ui.rerun_passes == 1
    assert ui.flake_rate > 0
    kinds = {e.kind for e in ui.entries}
    assert (
        EntryKind.TRIAGE_TIME in kinds and EntryKind.RERUN_COMPUTE in kinds and EntryKind.CI_COMPUTE in kinds
    )
    assert ledger.accounts[UNIT].rerun_passes == 0


def test_genuine_failure_followed_by_fix_commit_is_credited(cfg):
    runs = [make_run("r1", "sha1", 1, 20, {UNIT: F})] + [
        make_run(f"r{i}", f"sha{i}", 1, i, {UNIT: P}) for i in range(2, 8)
    ]
    fixes = [FixCommit(sha="f1", timestamp=NOW - timedelta(days=19), message="fix: rounding bug")]
    ledger = build_ledger(runs, cfg, fixes=fixes, now=NOW)
    acc = ledger.accounts[UNIT]
    assert acc.regressions_caught == 1
    assert acc.total_value == cfg.rates.regression_value_usd
    assert acc.verdict == Verdict.ASSET


def test_fix_commit_outside_window_is_not_credited(cfg):
    runs = [make_run("r1", "sha1", 1, 20, {UNIT: F})] + [
        make_run(f"r{i}", f"sha{i}", 1, i, {UNIT: P}) for i in range(2, 8)
    ]
    fixes = [FixCommit(sha="f1", timestamp=NOW - timedelta(days=10), message="fix: unrelated")]
    ledger = build_ledger(runs, cfg, fixes=fixes, now=NOW)
    assert ledger.accounts[UNIT].regressions_caught == 0


def _fix_five_days_after_failure():
    runs = [make_run("r1", "sha1", 1, 20, {UNIT: F})] + [
        make_run(f"r{i}", f"sha{i}", 1, i, {UNIT: P}) for i in range(2, 8)
    ]
    return runs, [FixCommit(sha="f1", timestamp=NOW - timedelta(days=15), message="fix: calc")]


def test_fix_window_is_configurable():
    runs, fixes = _fix_five_days_after_failure()
    default = build_ledger(runs, Config(window_days=90), fixes=fixes, now=NOW)
    assert default.accounts[UNIT].regressions_caught == 0
    week = Config.model_validate({"window_days": 90, "thresholds": {"fix_window_days": 7}})
    ledger = build_ledger(runs, week, fixes=fixes, now=NOW)
    assert ledger.accounts[UNIT].regressions_caught == 1


@pytest.mark.parametrize("days", [0, -1])
def test_fix_window_must_be_positive(days):
    with pytest.raises(ValidationError, match="fix_window_days"):
        Config.model_validate({"thresholds": {"fix_window_days": days}})


def test_flaky_failure_is_not_credited_even_with_nearby_fix(cfg):
    runs = [
        make_run("r1", "sha1", 1, 20, {UNIT: F}),
        make_run("r1b", "sha1", 2, 20, {UNIT: P}),
    ] + [make_run(f"r{i}", f"sha{i}", 1, i, {UNIT: P}) for i in range(2, 8)]
    fixes = [FixCommit(sha="f1", timestamp=NOW - timedelta(days=19), message="fix: something")]
    ledger = build_ledger(runs, cfg, fixes=fixes, now=NOW)
    assert ledger.accounts[UNIT].regressions_caught == 0


def test_quarantine_verdict_for_high_flake(cfg):
    runs = []
    for i in range(1, 11):
        runs.append(make_run(f"r{i}", f"sha{i}", 1, i, {UI: F if i % 2 else P}, duration=30))
        if i % 2:
            runs.append(make_run(f"r{i}b", f"sha{i}", 2, i, {UI: P}, duration=30))
    ledger = build_ledger(runs, cfg, now=NOW)
    assert ledger.accounts[UI].verdict == Verdict.QUARANTINE


def test_retire_requires_min_cost_and_hold_otherwise():
    cfg = Config(window_days=90)
    cheap = [make_run(f"r{i}", f"sha{i}", 1, i, {UNIT: P}, duration=0.1) for i in range(1, 8)]
    ledger = build_ledger(cheap, cfg, now=NOW)
    assert ledger.accounts[UNIT].verdict == Verdict.HOLD
    churn = {
        "src/test/java/com/acme/ui/CheckoutSeleniumTest.java": ChurnRecord(
            file="src/test/java/com/acme/ui/CheckoutSeleniumTest.java", commits=6, lines_changed=400
        )
    }
    expensive = [
        make_run(
            f"r{i}",
            f"sha{i}",
            1,
            i,
            {UI: P},
            duration=120,
            file="src/test/java/com/acme/ui/CheckoutSeleniumTest.java",
        )
        for i in range(1, 8)
    ]
    ledger = build_ledger(expensive, cfg, churn=churn, now=NOW)
    acc = ledger.accounts[UI]
    assert acc.layer == Layer.UI
    assert acc.churn_lines == 400 and any(e.kind == EntryKind.MAINTENANCE for e in acc.entries)
    assert acc.verdict == Verdict.RETIRE


def test_demote_ui_test_that_is_far_costlier_per_catch_than_service_layer(cfg):
    svc = "com.acme.api.OrderServiceIT.creates_order"
    runs = [make_run("r1", "sha1", 1, 20, {svc: F, UI: F}, duration=1.0)]
    runs[0].results[1].duration_s = 300.0
    for i in range(2, 9):
        r = make_run(f"r{i}", f"sha{i}", 1, i, {svc: P, UI: P}, duration=1.0)
        r.results[1].duration_s = 300.0
        runs.append(r)
    fixes = [FixCommit(sha="f1", timestamp=NOW - timedelta(days=19), message="fix: order bug")]
    ledger = build_ledger(runs, cfg, fixes=fixes, now=NOW)
    assert ledger.accounts[svc].verdict == Verdict.ASSET
    assert ledger.accounts[UI].verdict == Verdict.DEMOTE


def test_insufficient_executions_is_hold(cfg):
    runs = [make_run("r1", "sha1", 1, 1, {UI: F}, duration=100)]
    ledger = build_ledger(runs, cfg, now=NOW)
    assert ledger.accounts[UI].verdict == Verdict.HOLD


def test_ai_signals_attach_to_account(cfg):
    f = "src/test/java/com/acme/ai/RecTest.java"
    runs = [
        make_run(f"r{i}", f"sha{i}", 1, i, {"com.acme.ai.RecTest.returns": P}, file=f) for i in range(1, 8)
    ]
    ledger = build_ledger(
        runs,
        cfg,
        ai_signals={f: {"ai_generated": True, "assertion_density": 0.0, "mutation_score": 0.2}},
        now=NOW,
    )
    acc = ledger.accounts["com.acme.ai.RecTest.returns"]
    assert acc.ai_generated and acc.mutation_score == 0.2 and acc.assertion_density == 0.0


def test_runs_outside_window_are_ignored(cfg):
    runs = [make_run("old", "shaold", 1, 200, {UNIT: F})] + [
        make_run(f"r{i}", f"sha{i}", 1, i, {UNIT: P}) for i in range(1, 6)
    ]
    ledger = build_ledger(runs, cfg, now=NOW)
    assert ledger.runs_analysed == 5 and ledger.accounts[UNIT].failures == 0


def test_flake_rate_is_per_commit_not_per_execution(cfg):
    # 10 commits; 4 of them fail then pass on a same-SHA rerun.
    # Executions = 14, but the honest flake rate is 4/10 commits = 0.40 (quarantine-grade).
    runs = []
    for i in range(1, 11):
        flaky = i <= 4
        runs.append(make_run(f"r{i}", f"sha{i}", 1, i, {UI: F if flaky else P}, duration=30))
        if flaky:
            runs.append(make_run(f"r{i}b", f"sha{i}", 2, i, {UI: P}, duration=30))
    acc = build_ledger(runs, cfg, now=NOW).accounts[UI]
    assert acc.executions == 14
    assert acc.flake_rate == 0.4
    assert acc.verdict == Verdict.QUARANTINE
