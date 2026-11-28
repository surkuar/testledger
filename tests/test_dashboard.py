"""The static dashboard: self-contained output, schema versioning, diff reuse."""

from __future__ import annotations

import json
import re
from pathlib import Path

from typer.testing import CliRunner

from testledger.cli import app


def _make_out(tmp_path: Path, fixtures: Path, name: str, sha: str = "aaaaaaa") -> Path:
    d = tmp_path / name / "ci" / f"20261001-{sha}-attempt1"
    d.mkdir(parents=True)
    (d / "TEST-results.xml").write_text(
        (fixtures / "junit/surefire.xml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / name / "testledger.yml").write_text(
        "org_name: T\nwindow_days: 3650\nsources:\n  junit_globs: ['ci/**/TEST-*.xml']\n"
        "  cucumber_globs: []\n  git_repo: null\n",
        encoding="utf-8",
    )
    res = CliRunner().invoke(
        app,
        [
            "analyze",
            "--config",
            str(tmp_path / name / "testledger.yml"),
            "--reports",
            str(tmp_path / name),
            "--out",
            str(tmp_path / name / "out"),
            "--skip-ci",
            "--skip-git",
        ],
    )
    assert res.exit_code == 0, res.output
    return tmp_path / name / "out"


def test_ledger_json_has_schema_version(tmp_path: Path, fixtures: Path):
    out = _make_out(tmp_path, fixtures, "w")
    data = json.loads((out / "ledger.json").read_text(encoding="utf-8"))
    assert data["schema_version"] == 1


def test_dashboard_writes_self_contained_html(tmp_path: Path, fixtures: Path):
    out = _make_out(tmp_path, fixtures, "w")
    res = CliRunner().invoke(app, ["dashboard", str(out)])
    assert res.exit_code == 0, res.output
    html = (out / "dashboard.html").read_text(encoding="utf-8")
    # data is embedded, no network references: fully offline
    assert "com.acme.unit.PriceCalculatorTest" in html
    assert not re.search(r'<(?:script|link)[^>]+(?:src|href)\s*=\s*"https?://', html)
    assert "ledger-data" in html and "prefers-color-scheme: dark" in html


def test_dashboard_refuses_unknown_or_missing_schema_version(tmp_path: Path, fixtures: Path):
    out = _make_out(tmp_path, fixtures, "w")
    ledger = out / "ledger.json"
    original = json.loads(ledger.read_text(encoding="utf-8"))

    ledger.write_text(json.dumps(original | {"schema_version": 2}), encoding="utf-8")
    res = CliRunner().invoke(app, ["dashboard", str(out)])
    assert res.exit_code != 0 and "schema_version" in (res.output + (res.stderr or ""))

    del original["schema_version"]
    ledger.write_text(json.dumps(original), encoding="utf-8")
    res = CliRunner().invoke(app, ["dashboard", str(out)])
    assert res.exit_code != 0 and "schema_version" in (res.output + (res.stderr or ""))


def test_dashboard_compare_shows_changes(tmp_path: Path, fixtures: Path):
    old = _make_out(tmp_path, fixtures, "q3", sha="aaaaaaa")
    new = _make_out(tmp_path, fixtures, "q4", sha="bbbbbbb")
    res = CliRunner().invoke(app, ["dashboard", str(new), "--compare", str(old)])
    assert res.exit_code == 0, res.output
    html = (new / "dashboard.html").read_text(encoding="utf-8")
    assert "Changes over time" in html and "diff-data" in html
    diff = json.loads(re.search(r'id="diff-data">(.*?)</script>', html, re.S).group(1))
    assert "portfolio" in diff and "verdicts" in diff
