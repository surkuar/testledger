from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

from conftest import NOW, make_run
from typer.testing import CliRunner

from testledger.cli import _meta_from_path, app
from testledger.config import Config
from testledger.ledger import build_ledger
from testledger.models import FixCommit, Outcome
from testledger.reports import build_reports, write_reports

P, F = Outcome.PASSED, Outcome.FAILED


def _ledger(cfg):
    unit, ui = "com.acme.unit.CalcTest.adds", "com.acme.ui.CheckoutSeleniumTest.full_flow"
    runs = [
        make_run("r1", "sha1", 1, 20, {unit: F, ui: F}, duration=5),
        make_run("r1b", "sha1", 2, 20, {unit: F, ui: P}, duration=5),
    ]
    runs += [make_run(f"r{i}", f"sha{i}", 1, i, {unit: P, ui: P}, duration=5) for i in range(2, 9)]
    fixes = [FixCommit(sha="f", timestamp=NOW - timedelta(days=19), message="fix: calc")]
    return build_ledger(
        runs,
        cfg,
        fixes=fixes,
        ai_signals={ui: {"ai_generated": True, "assertion_density": 0.2, "mutation_score": None}},
        now=NOW,
    )


def test_reports_render_and_write(tmp_path: Path, cfg: Config):
    bundle = build_reports(_ledger(cfg), cfg)
    assert bundle.summary["regressions_caught"] == 1
    assert bundle.summary["ai_unverified_liabilities"] == 1
    assert "Quality Investment Memo" in bundle.memo_markdown and "<html" in bundle.memo_html
    assert "regression fix window: 3.0 days" in bundle.memo_markdown
    assert "within 3.0 days by a product fix commit" in bundle.memo_markdown
    assert "regression fix window (days)" in bundle.memo_html
    assert bundle.income_statement["net_usd"] == bundle.summary["net_usd"]
    written = write_reports(bundle, tmp_path)
    assert {
        "memo.md",
        "memo.html",
        "ledger.json",
        "summary.json",
        "writeoffs.json",
        "ai_liabilities.json",
    } <= set(written)
    data = json.loads((tmp_path / "ledger.json").read_text(encoding="utf-8"))
    assert len(data["accounts"]) == 2 and all("entries" in a for a in data["accounts"])


def test_meta_from_path_conventions():
    sha, attempt, ts = _meta_from_path(Path("ci/20260915T1030-066b939951-attempt2/TEST-x.xml"))
    assert sha == "066b939951" and attempt == 2 and ts.isoformat() == "2026-09-15T10:30:00+00:00"
    sha, attempt, ts = _meta_from_path(Path("ci/2026-09-15-abc1234-a1/junit.xml"))
    assert sha == "abc1234" and attempt == 1 and ts.day == 15
    assert _meta_from_path(Path("surefire-reports/TEST-x.xml")) == (None, 1, None)


def test_cli_end_to_end(tmp_path: Path, fixtures: Path):
    runner = CliRunner()
    for day, sha, attempt in (
        ("20261001", "aaaaaaa", 1),
        ("20261001", "aaaaaaa", 2),
        ("20261003", "bbbbbbb", 1),
        ("20261005", "ccccccc", 1),
        ("20261007", "ddddddd", 1),
        ("20261009", "eeeeeee", 1),
    ):
        d = tmp_path / "ci" / f"{day}-{sha}-attempt{attempt}"
        d.mkdir(parents=True)
        (d / "TEST-results.xml").write_text(
            (fixtures / "junit/surefire.xml").read_text(encoding="utf-8"), encoding="utf-8"
        )
        (d / "cucumber.json").write_text(
            (fixtures / "cucumber/cucumber.json").read_text(encoding="utf-8"), encoding="utf-8"
        )
    (tmp_path / "testledger.yml").write_text(
        "org_name: T\nwindow_days: 3650\nsources:\n  junit_globs: ['ci/**/TEST-*.xml']\n  cucumber_globs: ['ci/**/cucumber.json']\n  git_repo: null\n",
        encoding="utf-8",
    )
    res = runner.invoke(
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
    assert summary["runs_analysed"] == 12 and summary["tests"] == 15
    res = runner.invoke(app, ["pr-comment", str(tmp_path / "out")])
    assert res.exit_code == 0 and "TestLedger" in res.output
    res = runner.invoke(app, ["init", "--path", str(tmp_path / "new.yml")])
    assert res.exit_code == 0 and (tmp_path / "new.yml").exists()


def test_memo_survives_non_utf8_console(tmp_path: Path, cfg: Config):
    """Regression: `memo` crashed with UnicodeEncodeError on '≈' when stdout was cp1252 (Windows)."""
    bundle = build_reports(_ledger(cfg), cfg)
    assert "≈" in bundle.memo_markdown
    write_reports(bundle, tmp_path)
    env = {**os.environ, "PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
    proc = subprocess.run(
        [sys.executable, "-c", "from testledger.cli import app; app()", "memo", str(tmp_path)],
        capture_output=True,
        env=env,
        timeout=60,
    )
    stderr = proc.stderr.decode("cp1252", errors="replace")
    assert proc.returncode == 0, stderr
    out = proc.stdout.decode("cp1252")
    assert "Quality Investment Memo" in out and "save ? $" in out
