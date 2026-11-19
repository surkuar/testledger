from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from conftest import NOW
from typer.testing import CliRunner

from testledger.cli import app
from testledger.config import Config
from testledger.ingest import parse_allure_results, parse_cucumber_json, parse_junit_xml
from testledger.ledger import build_ledger
from testledger.models import Outcome


def test_parse_surefire_xml(fixtures):
    run = parse_junit_xml(fixtures / "junit/surefire.xml", run_id="r1", commit_sha="abc", attempt=1)
    assert run.run_id == "r1" and run.commit_sha == "abc" and run.source == "junit"
    assert len(run.results) == 12
    ids = {r.test_id for r in run.results}
    assert "com.acme.unit.PriceCalculatorTest.applies_discount" in ids
    failed = [r for r in run.results if r.outcome.is_failure]
    assert len(failed) == 2 and all(r.message for r in failed)
    assert all(r.file for r in run.results)


def test_parse_testng_xml(fixtures):
    run = parse_junit_xml(fixtures / "junit/testng-results.xml")
    by = {r.test_id: r for r in run.results}
    assert set(by) == {
        "com.acme.LoginTest.validLogin",
        "com.acme.LoginTest.lockedAccount",
        "com.acme.LoginTest.sso",
    }
    assert by["com.acme.LoginTest.lockedAccount"].outcome == Outcome.FAILED
    assert by["com.acme.LoginTest.sso"].outcome == Outcome.SKIPPED
    assert by["com.acme.LoginTest.validLogin"].duration_s == 1.2


def test_parse_cucumber_json(fixtures):
    ts = datetime(2026, 9, 15, tzinfo=UTC)
    run = parse_cucumber_json(fixtures / "cucumber/cucumber.json", started_at=ts, commit_sha="abc", attempt=2)
    assert run.source == "cucumber" and run.attempt == 2 and run.started_at == ts
    assert len(run.results) == 3
    checkout = [r for r in run.results if r.suite == "Checkout"]
    assert len(checkout) == 2 and all("@ui" in r.tags for r in checkout)
    assert all(r.file == "features/checkout.feature" for r in checkout)
    assert all(r.duration_s > 0 for r in run.results)


ALLURE = "allure/allure-results"
T0 = 1790000000000  # epoch ms used by the fixture's start/stop times
LOCKED = "com.acme.ui.LoginTest.locked_account"


def test_parse_allure_results(fixtures):
    runs = parse_allure_results(fixtures / ALLURE, run_id="r1", commit_sha="abc", branch="main")
    assert [(r.run_id, r.attempt) for r in runs] == [("r1", 1), ("r1-retry1", 2)]
    first, retry = runs
    assert all(r.source == "allure" and r.commit_sha == "abc" and r.branch == "main" for r in runs)
    by = {r.test_id: r for r in first.results}
    assert set(by) == {  # container, attachment and executor.json are ignored
        "com.acme.api.CartApiTest.adds_item",
        LOCKED,
        "tests/test_payments.py::test_refund",
        "com.acme.unit.TaxTest.vat_eu",
    }
    assert (
        by[LOCKED].outcome == Outcome.FAILED and by[LOCKED].message == "expected 'Locked' but was 'Welcome'"
    )
    assert by[LOCKED].duration_s == 2.5
    assert by["tests/test_payments.py::test_refund"].outcome == Outcome.ERROR
    assert by["com.acme.unit.TaxTest.vat_eu"].outcome == Outcome.SKIPPED
    # suite: testClass, then suite, then package
    assert by["com.acme.api.CartApiTest.adds_item"].suite == "com.acme.api.CartApiTest"
    assert by["tests/test_payments.py::test_refund"].suite == "test_payments"
    assert by["com.acme.unit.TaxTest.vat_eu"].suite == "com.acme.unit"
    assert by["com.acme.api.CartApiTest.adds_item"].tags == ["smoke"]
    # the retry round holds only the retried test, ordered by start, and starts later
    assert [(r.test_id, r.outcome) for r in retry.results] == [(LOCKED, Outcome.PASSED)]
    assert first.started_at == datetime.fromtimestamp((T0 + 500) / 1000, tz=UTC)  # earliest start
    assert retry.started_at > first.started_at


def test_allure_retries_are_ordered_by_start_and_offset_from_folder_attempt(tmp_path):
    for name, status, start in (("c", "passed", 3000), ("a", "failed", 1000), ("b", "broken", 2000)):
        (tmp_path / f"{name}-result.json").write_text(
            json.dumps(
                {
                    "historyId": "h",
                    "fullName": "T.t",
                    "name": "t",
                    "status": status,
                    "start": start,
                    "stop": start + 10,
                }
            ),
            encoding="utf-8",
        )
    runs = parse_allure_results(tmp_path, run_id="r", attempt=2)
    assert [(r.attempt, r.results[0].outcome) for r in runs] == [
        (2, Outcome.FAILED),
        (3, Outcome.ERROR),
        (4, Outcome.PASSED),
    ]


def test_allure_retry_is_a_flake_not_a_regression(fixtures):
    runs = parse_allure_results(fixtures / ALLURE, commit_sha="abc", started_at=NOW)
    ledger = build_ledger(runs, Config(window_days=90), now=NOW)
    acc = ledger.accounts[LOCKED]
    assert acc.rerun_passes == 1 and acc.flake_rate == 1.0
    assert acc.regressions_caught == 0


def test_allure_empty_and_invalid_folders(tmp_path):
    assert parse_allure_results(tmp_path) == []
    (tmp_path / "x-result.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError, match="x-result.json"):
        parse_allure_results(tmp_path)


def test_allure_unknown_status_and_missing_times(tmp_path):
    (tmp_path / "u-result.json").write_text(
        json.dumps({"fullName": "T.u", "name": "u", "status": "unknown"}), encoding="utf-8-sig"
    )
    [run] = parse_allure_results(tmp_path, started_at=NOW)
    assert run.results[0].outcome == Outcome.SKIPPED and run.results[0].duration_s == 0.0
    assert run.started_at == NOW


def test_cli_reads_allure_folders(tmp_path: Path, fixtures: Path):
    for sha, attempt in (("aaaaaaa", 1), ("bbbbbbb", 1)):
        shutil.copytree(
            fixtures / ALLURE, tmp_path / "ci" / f"20261001-{sha}-attempt{attempt}" / "allure-results"
        )
    (tmp_path / "testledger.yml").write_text(
        "window_days: 3650\nsources:\n  junit_globs: []\n  cucumber_globs: []\n  git_repo: null\n",
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
    summary = json.loads((tmp_path / "out/summary.json").read_text(encoding="utf-8"))
    assert summary["runs_analysed"] == 4 and summary["tests"] == 4  # 2 folders x (run + retry round)
    ledger = json.loads((tmp_path / "out/ledger.json").read_text(encoding="utf-8"))
    locked = next(a for a in ledger["accounts"] if a["test_id"] == LOCKED)
    assert locked["executions"] == 4 and locked["flake_rate"] == 1.0  # fail -> pass retry in each folder
    assert locked["regressions_caught"] == 0
    kinds = {e["kind"] for e in locked["entries"]}
    assert {"rerun_compute", "triage_time"} <= kinds
