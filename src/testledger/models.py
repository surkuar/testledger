"""Core domain model.

The model is deliberately small and framework-agnostic. Every ingester
(JUnit, Cucumber, Allure, GitHub Actions, GitLab CI, Jenkins, git) normalises its source into
``TestRun`` / ``TestResult`` objects plus optional ``ChurnRecord`` entries.
The ledger engine then books ``LedgerEntry`` rows against each ``TestAccount``.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Outcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"

    @property
    def is_failure(self) -> bool:
        return self in (Outcome.FAILED, Outcome.ERROR)


class Layer(StrEnum):
    """Test-pyramid layer. Drives default cost multipliers and demotion advice."""

    UNIT = "unit"
    SERVICE = "service"
    UI = "ui"
    UNKNOWN = "unknown"


class TestResult(BaseModel):
    """One execution of one test case inside one run."""

    test_id: str = Field(description="Stable identifier, e.g. 'com.acme.LoginTest.valid_login'")
    name: str
    suite: str = ""
    file: str | None = Field(default=None, description="Source path when known (used for git churn)")
    outcome: Outcome
    duration_s: float = 0.0
    message: str | None = None
    tags: list[str] = Field(default_factory=list)


class TestRun(BaseModel):
    """A single CI execution (one workflow run / GitLab pipeline / Jenkins build / local run)."""

    run_id: str
    started_at: datetime
    commit_sha: str | None = None
    branch: str | None = None
    attempt: int = Field(default=1, description="1 = first execution; >1 means a rerun of the same SHA")
    source: str = Field(
        default="junit", description="junit | cucumber | allure | github-actions | gitlab | jenkins"
    )
    results: list[TestResult] = Field(default_factory=list)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.outcome.is_failure)


class ChurnRecord(BaseModel):
    """Git activity against a test file in the analysis window."""

    file: str
    commits: int = 0
    lines_changed: int = 0
    authors: int = 0
    last_touched: datetime | None = None


class FixCommit(BaseModel):
    """A product-code commit that looks like a bug fix (used to credit tests that caught it)."""

    sha: str
    timestamp: datetime
    message: str
    files: list[str] = Field(default_factory=list)


class EntryKind(StrEnum):
    # Costs (debits)
    CI_COMPUTE = "ci_compute"
    RERUN_COMPUTE = "rerun_compute"
    TRIAGE_TIME = "triage_time"
    MAINTENANCE = "maintenance"
    # Value (credits)
    REGRESSION_CAUGHT = "regression_caught"
    RISK_COVERAGE = "risk_coverage"


class LedgerEntry(BaseModel):
    """One booked line. Costs are positive 'debit'; value is positive 'credit'."""

    test_id: str
    kind: EntryKind
    amount_usd: float
    quantity: float = 0.0
    unit: str = ""
    note: str = ""

    @property
    def is_cost(self) -> bool:
        return self.kind in (
            EntryKind.CI_COMPUTE,
            EntryKind.RERUN_COMPUTE,
            EntryKind.TRIAGE_TIME,
            EntryKind.MAINTENANCE,
        )


class Verdict(StrEnum):
    ASSET = "asset"  # value clearly exceeds cost
    HOLD = "hold"  # roughly break-even or insufficient data
    FIX = "fix"  # valuable but flaky/slow — repair it
    DEMOTE = "demote"  # valuable but expensive layer — rewrite lower in the pyramid
    RETIRE = "retire"  # pure cost, no evidence of value
    QUARANTINE = "quarantine"  # flake rate so high it is actively misleading


class TestAccount(BaseModel):
    """The per-test ledger account with derived statistics."""

    test_id: str
    name: str
    suite: str = ""
    file: str | None = None
    layer: Layer = Layer.UNKNOWN
    executions: int = 0
    failures: int = 0
    rerun_passes: int = Field(
        default=0, description="failed on attempt N, passed on attempt N+1 for same SHA"
    )
    flake_rate: float = 0.0
    avg_duration_s: float = 0.0
    regressions_caught: int = 0
    churn_commits: int = 0
    churn_lines: int = 0
    entries: list[LedgerEntry] = Field(default_factory=list)
    verdict: Verdict = Verdict.HOLD
    ai_generated: bool = False
    mutation_score: float | None = None
    assertion_density: float | None = None

    @property
    def total_cost(self) -> float:
        return round(sum(e.amount_usd for e in self.entries if e.is_cost), 4)

    @property
    def total_value(self) -> float:
        return round(sum(e.amount_usd for e in self.entries if not e.is_cost), 2)

    @property
    def net(self) -> float:
        return round(self.total_value - self.total_cost, 2)

    @property
    def roi(self) -> float | None:
        if self.total_cost <= 0:
            return None
        return round((self.total_value - self.total_cost) / self.total_cost, 3)


class Ledger(BaseModel):
    """The full book for one analysis window."""

    generated_at: datetime
    window_start: datetime
    window_end: datetime
    runs_analysed: int
    accounts: dict[str, TestAccount] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @property
    def total_cost(self) -> float:
        return round(sum(a.total_cost for a in self.accounts.values()), 2)

    @property
    def total_value(self) -> float:
        return round(sum(a.total_value for a in self.accounts.values()), 2)

    @property
    def net(self) -> float:
        return round(self.total_value - self.total_cost, 2)

    def by_verdict(self, verdict: Verdict) -> list[TestAccount]:
        return sorted(
            (a for a in self.accounts.values() if a.verdict == verdict),
            key=lambda a: a.net,
        )
