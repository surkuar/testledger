"""Narrative feature: off by default, stubbed provider, figure validation."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from conftest import NOW, make_run
from typer.testing import CliRunner

from testledger.cli import app
from testledger.config import Config, Narrative
from testledger.ledger import build_ledger
from testledger.models import Outcome
from testledger.narrative import NarrativeError, generate_narrative, untraced_figures
from testledger.narrative.generate import build_payload
from testledger.reports import build_reports

P, F = Outcome.PASSED, Outcome.FAILED


def _bundle_parts(cfg: Config):
    unit, ui = "com.acme.unit.CalcTest.adds", "com.acme.ui.CheckoutSeleniumTest.full_flow"
    runs = [
        make_run("r1", "sha1", 1, 20, {unit: F, ui: F}, duration=5),
        make_run("r1b", "sha1", 2, 20, {unit: F, ui: P}, duration=5),
    ]
    runs += [make_run(f"r{i}", f"sha{i}", 1, i, {unit: P, ui: P}, duration=5) for i in range(2, 9)]
    ledger = build_ledger(runs, cfg, now=NOW)
    bundle = build_reports(ledger, cfg)
    return ledger, bundle


def test_generate_with_stub_client(cfg: Config):
    _, bundle = _bundle_parts(cfg)
    settings = Narrative(provider="stub", model="stub-1")

    def complete(system: str, prompt: str, s: Narrative) -> str:
        assert "total_cost_usd" in prompt  # the provider sees the figures
        assert "errorMessage" not in prompt and "AssertionError" not in prompt
        cost = bundle.summary["total_cost_usd"]
        return (
            f"The estate carried ${cost:.2f} in 90 days and broke even; triage on flaky UI tests dominates."
        )

    text = generate_narrative(settings, bundle.summary, bundle.writeoffs, bundle.assumptions, complete)
    assert text is not None and text.startswith("The estate carried")


def test_untraced_dollar_figure_is_rejected(cfg: Config):
    _, bundle = _bundle_parts(cfg)
    settings = Narrative(provider="stub", model="stub-1")
    with pytest.raises(NarrativeError, match="not present in the input"):
        generate_narrative(
            settings,
            bundle.summary,
            bundle.writeoffs,
            bundle.assumptions,
            lambda *_: "We spent $9,999.99 this quarter.",
        )


def test_percentage_must_trace_but_ratio_forms_count():
    payload = {"summary": {"flake": 0.35, "total_cost_usd": 12.5}, "top_writeoffs": []}
    assert untraced_figures("Flakiness hit 35% and cost $12.50.", payload) == []
    assert untraced_figures("Flakiness hit 99%.", payload) == [99.0]
    assert untraced_figures("Cost was $12.51.", payload) == [12.51]
    # thousands separators and k-suffixes normalise
    assert untraced_figures("About $2,000 per defect, i.e. $2k.", {"assumptions": {"reg": 2000}}) == []


def test_payload_contains_no_failure_text(cfg: Config):
    _, bundle = _bundle_parts(cfg)
    payload = build_payload(bundle.summary, bundle.writeoffs, bundle.assumptions, 10)
    blob = str(payload)
    assert "AssertionError" not in blob and "message" not in blob
    assert set(payload) == {"summary", "top_writeoffs", "assumptions"}


def test_memo_labels_narrative_and_names_model(cfg: Config):
    ledger, _ = _bundle_parts(cfg)
    bundle = build_reports(
        ledger, cfg, narrative="Cost broke even; flaky UI tests dominate triage.", narrative_model="llama3.1"
    )
    assert "AI-generated summary (model: llama3.1)" in bundle.memo_markdown
    assert "not an independent analysis" in bundle.memo_markdown
    assert "AI-generated summary · model: llama3.1" in bundle.memo_html


def test_default_memo_has_no_narrative_and_imports_nothing(cfg: Config):
    ledger, _ = _bundle_parts(cfg)
    bundle = build_reports(ledger, cfg)
    assert "AI-generated" not in bundle.memo_markdown
    assert "openai" not in sys.modules


def test_cli_narrative_off_by_default(tmp_path: Path, fixtures: Path):
    d = tmp_path / "ci" / "20261001-aaaaaaa-attempt1"
    d.mkdir(parents=True)
    (d / "TEST-results.xml").write_text(
        (fixtures / "junit/surefire.xml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "testledger.yml").write_text(
        "org_name: T\nwindow_days: 3650\nsources:\n  junit_globs: ['ci/**/TEST-*.xml']\n"
        "  cucumber_globs: []\n  git_repo: null\n",
        encoding="utf-8",
    )
    res = CliRunner().invoke(
        app,
        [
            "analyze",
            "--config",
            str(tmp_path / "testledger.yml"),
            "--reports",
            str(tmp_path),
            "--out",
            str(tmp_path / "out"),
            "--skip-ci",
            "--skip-git",
        ],
    )
    assert res.exit_code == 0, res.output
    memo = (tmp_path / "out/memo.md").read_text(encoding="utf-8")
    assert "AI-generated" not in memo
    assert "openai" not in sys.modules


def test_cli_narrative_falls_back_when_provider_unavailable(tmp_path: Path, fixtures: Path):
    """--narrative with provider 'openai' but no optional package: warn, still write the plain memo."""
    d = tmp_path / "ci" / "20261001-aaaaaaa-attempt1"
    d.mkdir(parents=True)
    (d / "TEST-results.xml").write_text(
        (fixtures / "junit/surefire.xml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "testledger.yml").write_text(
        "org_name: T\nwindow_days: 3650\n"
        "narrative: {provider: openai, model: gpt-test}\n"
        "sources:\n  junit_globs: ['ci/**/TEST-*.xml']\n  cucumber_globs: []\n  git_repo: null\n",
        encoding="utf-8",
    )
    res = CliRunner().invoke(
        app,
        [
            "analyze",
            "--config",
            str(tmp_path / "testledger.yml"),
            "--reports",
            str(tmp_path),
            "--out",
            str(tmp_path / "out"),
            "--skip-ci",
            "--skip-git",
            "--narrative",
        ],
    )
    assert res.exit_code == 0, res.output
    assert "narrative skipped" in res.output
    memo = (tmp_path / "out/memo.md").read_text(encoding="utf-8")
    assert "AI-generated" not in memo
