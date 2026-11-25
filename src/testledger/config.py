"""Configuration: rates, thresholds and input sources, loaded from ``testledger.yml``.

Every number here is an *assumption the organisation owns*. Defaults are
conservative industry figures; the memo prints them so finance can challenge them.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_FIX_COMMIT_PATTERN = r"\b(fix|fixes|fixed|bug|hotfix|regression|defect|patch)\b"
DEFAULT_TEST_PATH_PATTERNS = [
    r"(^|/)(test|tests|spec|specs|features|e2e|__tests__)(/|$)|_test\.|Test\.|\.spec\.|\.feature$"
]


def _check_regex(pattern: str) -> None:
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"{pattern!r} is not a valid regular expression ({exc})") from exc


class Rates(BaseModel):
    engineer_hourly_usd: float = Field(default=75.0, description="Fully loaded engineer cost per hour")
    ci_minute_usd: float = Field(
        default=0.008, description="CI compute cost per minute (GitHub-hosted Linux default)"
    )
    triage_minutes_per_false_alarm: float = Field(
        default=18.0, description="Avg minutes an engineer spends on a flaky failure"
    )
    maintenance_minutes_per_line: float = Field(
        default=1.5, description="Avg engineer minutes per changed test line"
    )
    regression_value_usd: float = Field(
        default=2000.0, description="Avg cost of a defect reaching production"
    )
    risk_coverage_value_usd: float = Field(
        default=0.0, description="Optional baseline value per stable test per window"
    )


class Thresholds(BaseModel):
    quarantine_flake_rate: float = 0.30
    fix_flake_rate: float = 0.10
    slow_test_seconds: float = 60.0
    min_executions_for_verdict: int = 5
    asset_roi: float = 0.5
    retire_min_cost_usd: float = Field(
        default=25.0, description="Tests cheaper than this per window are never 'retire', only 'hold'"
    )
    demote_layer_cost_ratio: float = 3.0
    ai_min_mutation_score: float = 0.5
    ai_min_assertion_density: float = 0.8
    fix_window_days: float = Field(
        default=3.0,
        gt=0,
        description="A genuine failure earns regression credit only if a fix commit lands within this many days",
    )


class LayerRules(BaseModel):
    """Regex-free glob/substring rules that map test ids or paths to pyramid layers."""

    unit: list[str] = Field(default_factory=lambda: ["unit", "/test/", "Test."])
    service: list[str] = Field(default_factory=lambda: ["api", "service", "integration", "contract"])
    ui: list[str] = Field(
        default_factory=lambda: ["ui", "e2e", "selenium", "playwright", "cucumber", "features/"]
    )


class Sources(BaseModel):
    junit_globs: list[str] = Field(
        default_factory=lambda: ["**/surefire-reports/*.xml", "**/junit*.xml", "**/TEST-*.xml"]
    )
    cucumber_globs: list[str] = Field(default_factory=lambda: ["**/cucumber*.json"])
    allure_globs: list[str] = Field(
        default_factory=lambda: ["**/allure-results"],
        description="Allure results folders; one folder is one run, retries inside it become reruns",
    )
    git_repo: str | None = Field(
        default=".", description="Path to the repo for churn analysis; null disables"
    )
    fix_commit_pattern: str = Field(
        default=DEFAULT_FIX_COMMIT_PATTERN,
        description="Regex (case-insensitive) a commit subject must match to count as a bug fix",
    )
    test_path_patterns: list[str] = Field(
        default_factory=lambda: list(DEFAULT_TEST_PATH_PATTERNS),
        description="Regexes (case-insensitive, '/' separators) identifying test files; a path matching "
        "any one is a test. Drives maintenance churn and which files fix commits ignore",
    )
    github: dict[str, Any] | None = Field(
        default=None,
        description="{'repo': 'owner/name', 'workflow': 'ci.yml', 'token_env': 'GITHUB_TOKEN', 'max_runs': 200}",
    )
    gitlab: dict[str, Any] | None = Field(
        default=None,
        description="{'url': 'https://gitlab.com', 'project': 'group/project', "
        "'token_env': 'GITLAB_TOKEN', 'max_pipelines': 100}",
    )
    jenkins: dict[str, Any] | None = Field(
        default=None,
        description="{'url': 'https://ci.acme.com', 'job': 'acme-main', 'user_env': 'JENKINS_USER', 'token_env': 'JENKINS_TOKEN', 'max_builds': 100}",
    )

    @field_validator("fix_commit_pattern")
    @classmethod
    def _valid_regex(cls, v: str) -> str:
        _check_regex(v)
        return v

    @field_validator("test_path_patterns")
    @classmethod
    def _valid_regex_list(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("needs at least one pattern")
        for pattern in v:
            _check_regex(pattern)
        return v


class Config(BaseModel):
    org_name: str = "Your Organisation"
    window_days: int = 90
    rates: Rates = Field(default_factory=Rates)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    layers: LayerRules = Field(default_factory=LayerRules)
    sources: Sources = Field(default_factory=Sources)
    ai_generated_markers: list[str] = Field(
        default_factory=lambda: [
            "@ai-generated",
            "Generated-By:",
            "Co-Authored-By: Copilot",
            "Co-Authored-By: Claude",
        ],
        description="Strings in test source or commit messages that mark a test as AI-generated",
    )

    @classmethod
    def load(cls, path: str | Path | None) -> Config:
        if path is None:
            return cls()
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"Config not found: {p}")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        return cls.model_validate(data)

    def dump_example(self) -> str:
        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False)
