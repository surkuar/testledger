"""The ledger engine: books cost and value entries per test and issues verdicts.

Cost model (debits), per test over the analysis window
------------------------------------------------------
* **CI compute**      = Σ duration × ci_minute_usd  (every execution)
* **Rerun compute**   = executions on attempt > 1 × duration × ci_minute_usd
* **Triage time**     = flaky failures × triage_minutes × engineer_hourly / 60
* **Maintenance**     = lines changed in the test's file × maintenance_minutes_per_line × engineer_hourly / 60
                        (apportioned across tests sharing the file)

Value model (credits)
---------------------
* **Regression caught** = a genuine failure (failed on first attempt, and was
  NOT subsequently passed on a rerun of the same SHA) that was followed within
  ``thresholds.fix_window_days`` by a product-code commit matching the fix pattern.
  Each such event is worth ``regression_value_usd``.
* **Risk coverage**     = optional flat value per stable test (default 0) for
  organisations that want to recognise a "safety net" value.

Flake detection
---------------
A flaky failure is: failed on attempt N and passed on attempt N+1 for the
same commit SHA. Nothing changed between the two runs, so this is unambiguous.
Flake rate = flaky commits / commits tested (first-attempt executions); reruns
are excluded from the denominator so they cannot dilute the signal they reveal.
(A future rule could also flag fail→pass across adjacent commits that touched
no related product code.)

Verdicts
--------
QUARANTINE  flake_rate ≥ quarantine threshold
RETIRE      enough executions, zero regressions caught, net < 0, cost ≥ retire_min_cost_usd
DEMOTE      regressions caught > 0 but UI layer and cost per catch ≥ ratio × service-layer median
FIX         regressions caught > 0 and (flake_rate ≥ fix threshold or avg duration ≥ slow threshold)
ASSET       roi ≥ asset threshold
HOLD        everything else, including insufficient data
"""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, datetime, timedelta
from statistics import median

from testledger.config import Config
from testledger.ingest.codeowners import CodeOwners
from testledger.models import (
    ChurnRecord,
    EntryKind,
    FixCommit,
    Layer,
    Ledger,
    LedgerEntry,
    Outcome,
    TestAccount,
    TestRun,
    Verdict,
)


def classify_layer(test_id: str, file: str | None, cfg: Config) -> Layer:
    hay = f"{test_id} {file or ''}".lower()
    for layer, needles in (
        (Layer.UI, cfg.layers.ui),
        (Layer.SERVICE, cfg.layers.service),
        (Layer.UNIT, cfg.layers.unit),
    ):
        if any(n.lower() in hay for n in needles):
            return layer
    return Layer.UNKNOWN


def _detect_flakes(runs: list[TestRun]) -> dict[str, int]:
    """Count same-SHA fail→pass transitions per test."""
    by_sha: dict[str, list[TestRun]] = defaultdict(list)
    for r in runs:
        if r.commit_sha:
            by_sha[r.commit_sha].append(r)
    flakes: dict[str, int] = defaultdict(int)
    for sha_runs in by_sha.values():
        sha_runs.sort(key=lambda r: (r.attempt, r.started_at))
        outcomes: dict[str, list[Outcome]] = defaultdict(list)
        for r in sha_runs:
            for res in r.results:
                outcomes[res.test_id].append(res.outcome)
        for test_id, seq in outcomes.items():
            for a, b in zip(seq, seq[1:], strict=False):
                if a.is_failure and b == Outcome.PASSED:
                    flakes[test_id] += 1
    return flakes


def _genuine_failures(
    runs: list[TestRun], flakes_by_sha_test: set[tuple[str, str]]
) -> dict[str, list[datetime]]:
    """Failures on first attempt that were not later cleared by a same-SHA rerun."""
    out: dict[str, list[datetime]] = defaultdict(list)
    for r in runs:
        if r.attempt != 1:
            continue
        for res in r.results:
            if res.outcome.is_failure and (r.commit_sha or "", res.test_id) not in flakes_by_sha_test:
                out[res.test_id].append(r.started_at)
    return out


def _flaky_pairs(runs: list[TestRun]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    by_sha: dict[str, list[TestRun]] = defaultdict(list)
    for r in runs:
        if r.commit_sha:
            by_sha[r.commit_sha].append(r)
    for sha, sha_runs in by_sha.items():
        sha_runs.sort(key=lambda r: (r.attempt, r.started_at))
        seen_fail: set[str] = set()
        for r in sha_runs:
            for res in r.results:
                if res.outcome.is_failure:
                    seen_fail.add(res.test_id)
                elif res.outcome == Outcome.PASSED and res.test_id in seen_fail:
                    pairs.add((sha, res.test_id))
    return pairs


def _credit_regressions(
    failures: dict[str, list[datetime]], fixes: list[FixCommit], window: timedelta
) -> dict[str, int]:
    """A genuine failure followed by a fix commit within ``window`` counts once per fix."""
    credited: dict[str, int] = defaultdict(int)
    fix_times = sorted(f.timestamp.astimezone(UTC) for f in fixes)
    for test_id, times in failures.items():
        used: set[datetime] = set()
        for t in times:
            t = t.astimezone(UTC)
            for ft in fix_times:
                if ft in used:
                    continue
                if t <= ft <= t + window:
                    credited[test_id] += 1
                    used.add(ft)
                    break
    return credited


def build_ledger(
    runs: list[TestRun],
    cfg: Config,
    *,
    churn: dict[str, ChurnRecord] | None = None,
    fixes: list[FixCommit] | None = None,
    ai_signals: dict[str, dict] | None = None,
    codeowners: CodeOwners | None = None,
    now: datetime | None = None,
) -> Ledger:
    churn = churn or {}
    fixes = fixes or []
    ai_signals = ai_signals or {}
    now = now or datetime.now(tz=UTC)
    window_start = now - timedelta(days=cfg.window_days)
    runs = [r for r in runs if r.started_at >= window_start]
    rates, th = cfg.rates, cfg.thresholds

    accounts: dict[str, TestAccount] = {}
    durations: dict[str, list[float]] = defaultdict(list)
    rerun_execs: dict[str, int] = defaultdict(int)
    file_members: dict[str, set[str]] = defaultdict(set)

    for r in runs:
        for res in r.results:
            acc = accounts.get(res.test_id)
            if acc is None:
                acc = TestAccount(
                    test_id=res.test_id,
                    name=res.name,
                    suite=res.suite,
                    file=res.file,
                    layer=classify_layer(res.test_id, res.file, cfg),
                )
                accounts[res.test_id] = acc
            if res.outcome == Outcome.SKIPPED:
                continue
            acc.executions += 1
            if res.outcome.is_failure:
                acc.failures += 1
            durations[res.test_id].append(res.duration_s)
            if r.attempt > 1:
                rerun_execs[res.test_id] += 1
            if res.file:
                file_members[res.file].add(res.test_id)

    flakes = _detect_flakes(runs)
    flaky_pairs = _flaky_pairs(runs)
    genuine = _genuine_failures(runs, flaky_pairs)
    credited = _credit_regressions(genuine, fixes, timedelta(days=cfg.thresholds.fix_window_days))

    for test_id, acc in accounts.items():
        d = durations.get(test_id, [])
        acc.avg_duration_s = round(sum(d) / len(d), 3) if d else 0.0
        acc.rerun_passes = flakes.get(test_id, 0)
        # Flake rate is per commit tested (first attempts), not per execution:
        # counting reruns in the denominator would dilute the very signal reruns reveal.
        first_attempts = acc.executions - rerun_execs.get(test_id, 0)
        acc.flake_rate = round(acc.rerun_passes / first_attempts, 3) if first_attempts > 0 else 0.0
        acc.regressions_caught = credited.get(test_id, 0)

        total_minutes = sum(d) / 60.0
        if total_minutes:
            acc.entries.append(
                LedgerEntry(
                    test_id=test_id,
                    kind=EntryKind.CI_COMPUTE,
                    amount_usd=round(total_minutes * rates.ci_minute_usd, 4),
                    quantity=round(total_minutes, 2),
                    unit="ci-minutes",
                )
            )
        if rerun_execs.get(test_id):
            rerun_minutes = rerun_execs[test_id] * acc.avg_duration_s / 60.0
            acc.entries.append(
                LedgerEntry(
                    test_id=test_id,
                    kind=EntryKind.RERUN_COMPUTE,
                    amount_usd=round(rerun_minutes * rates.ci_minute_usd, 4),
                    quantity=rerun_execs[test_id],
                    unit="rerun-executions",
                )
            )
        if acc.rerun_passes:
            triage_hours = acc.rerun_passes * rates.triage_minutes_per_false_alarm / 60.0
            acc.entries.append(
                LedgerEntry(
                    test_id=test_id,
                    kind=EntryKind.TRIAGE_TIME,
                    amount_usd=round(triage_hours * rates.engineer_hourly_usd, 2),
                    quantity=acc.rerun_passes,
                    unit="false-alarms",
                )
            )
        if acc.file and acc.file in churn:
            rec = churn[acc.file]
            share = 1 / max(1, len(file_members.get(acc.file, {test_id})))
            acc.churn_commits = rec.commits
            acc.churn_lines = round(rec.lines_changed * share)
            maint_hours = acc.churn_lines * rates.maintenance_minutes_per_line / 60.0
            if maint_hours:
                acc.entries.append(
                    LedgerEntry(
                        test_id=test_id,
                        kind=EntryKind.MAINTENANCE,
                        amount_usd=round(maint_hours * rates.engineer_hourly_usd, 2),
                        quantity=acc.churn_lines,
                        unit="lines-changed",
                    )
                )
        if acc.regressions_caught:
            acc.entries.append(
                LedgerEntry(
                    test_id=test_id,
                    kind=EntryKind.REGRESSION_CAUGHT,
                    amount_usd=round(acc.regressions_caught * rates.regression_value_usd, 2),
                    quantity=acc.regressions_caught,
                    unit="regressions",
                )
            )
        if rates.risk_coverage_value_usd and acc.flake_rate < th.fix_flake_rate:
            acc.entries.append(
                LedgerEntry(
                    test_id=test_id,
                    kind=EntryKind.RISK_COVERAGE,
                    amount_usd=rates.risk_coverage_value_usd,
                    quantity=1,
                    unit="window",
                )
            )
        if codeowners is not None and acc.file:
            acc.owners = codeowners.owners_for(acc.file)
        sig = ai_signals.get(test_id) or (ai_signals.get(acc.file) if acc.file else None)
        if sig:
            acc.ai_generated = bool(sig.get("ai_generated", False))
            acc.mutation_score = sig.get("mutation_score")
            acc.assertion_density = sig.get("assertion_density")

    _assign_verdicts(accounts, cfg)

    notes = [
        f"Window: {cfg.window_days} days, {len(runs)} runs analysed.",
        f"Rates: engineer ${rates.engineer_hourly_usd}/h, CI ${rates.ci_minute_usd}/min, regression value ${rates.regression_value_usd}.",
        f"Fix commits detected: {len(fixes)}; test files with churn: {len(churn)}.",
    ]
    return Ledger(
        generated_at=now,
        window_start=window_start,
        window_end=now,
        runs_analysed=len(runs),
        accounts=accounts,
        notes=notes,
    )


def _assign_verdicts(accounts: dict[str, TestAccount], cfg: Config) -> None:
    th = cfg.thresholds
    service_cost_per_catch = [
        a.total_cost / a.regressions_caught
        for a in accounts.values()
        if a.layer in (Layer.SERVICE, Layer.UNIT) and a.regressions_caught > 0
    ]
    svc_median = median(service_cost_per_catch) if service_cost_per_catch else None

    for a in accounts.values():
        if a.executions < th.min_executions_for_verdict:
            a.verdict = Verdict.HOLD
            continue
        if a.flake_rate >= th.quarantine_flake_rate:
            a.verdict = Verdict.QUARANTINE
            continue
        if a.regressions_caught == 0:
            a.verdict = (
                Verdict.RETIRE if (a.net < 0 and a.total_cost >= th.retire_min_cost_usd) else Verdict.HOLD
            )
            continue
        if (
            a.layer == Layer.UI
            and svc_median
            and (a.total_cost / a.regressions_caught) >= th.demote_layer_cost_ratio * svc_median
        ):
            a.verdict = Verdict.DEMOTE
            continue
        if a.flake_rate >= th.fix_flake_rate or a.avg_duration_s >= th.slow_test_seconds:
            a.verdict = Verdict.FIX
            continue
        roi = a.roi
        # A test that caught a regression at (near-)zero cost is the best asset there is.
        if a.total_value > 0 and (roi is None or roi >= th.asset_roi):
            a.verdict = Verdict.ASSET
        else:
            a.verdict = Verdict.HOLD
