"""Git ingestion against a real throwaway repository (requires the `git` CLI)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from testledger.cli import app
from testledger.config import Config
from testledger.ingest.gitlog import collect_churn, collect_fix_commits

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(repo: Path, *args: str) -> None:
    env = os.environ | {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    test_file = tmp_path / "src/test/java/CalcTest.java"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("\n".join(f"line {i}" for i in range(50)), encoding="utf-8")
    (tmp_path / "src/main").mkdir(parents=True)
    (tmp_path / "src/main/Calc.java").write_text("class Calc {}", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "feat: add calculator and its tests")
    test_file.write_text(test_file.read_text(encoding="utf-8") + "\nextra 1\nextra 2\n", encoding="utf-8")
    _git(tmp_path, "commit", "-q", "-am", "test: tweak calc test")
    (tmp_path / "src/main/Calc.java").write_text("class Calc { int x; }", encoding="utf-8")
    _git(tmp_path, "commit", "-q", "-am", "fix: rounding bug in Calc")
    return tmp_path


def test_churn_counts_modifications_not_authoring(repo: Path):
    churn = collect_churn(repo, datetime.now(tz=UTC) - timedelta(days=1))
    rec = churn["src/test/java/CalcTest.java"]
    # The 50 lines written when the test was created are authoring, not maintenance.
    assert rec.commits == 1
    assert 0 < rec.lines_changed < 10
    assert "src/main/Calc.java" not in churn


def test_fix_commits_only_on_product_code(repo: Path):
    fixes = collect_fix_commits(repo, datetime.now(tz=UTC) - timedelta(days=1))
    assert [f.message for f in fixes] == ["fix: rounding bug in Calc"]
    assert fixes[0].files == ["src/main/Calc.java"]


def test_non_repo_returns_empty(tmp_path: Path):
    since = datetime.now(tz=UTC) - timedelta(days=1)
    assert collect_churn(tmp_path, since) == {} and collect_fix_commits(tmp_path, since) == []


def test_custom_fix_pattern_replaces_default(repo: Path):
    (repo / "src/main/Calc.java").write_text("class Calc { int y; }", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "INC-123: overflow in Calc")
    since = datetime.now(tz=UTC) - timedelta(days=1)
    default = [f.message for f in collect_fix_commits(repo, since)]
    assert default == ["fix: rounding bug in Calc"]  # the default misses the ticket prefix
    custom = [f.message for f in collect_fix_commits(repo, since, r"^INC-\d+")]
    assert custom == ["INC-123: overflow in Calc"]  # and the custom one ignores "fix:"


def test_fix_pattern_is_case_insensitive(repo: Path):
    since = datetime.now(tz=UTC) - timedelta(days=1)
    assert [f.message for f in collect_fix_commits(repo, since, "ROUNDING")] == ["fix: rounding bug in Calc"]


def test_invalid_fix_pattern_rejected_at_load():
    with pytest.raises(ValidationError, match="fix_commit_pattern"):
        Config.model_validate({"sources": {"fix_commit_pattern": "(unclosed"}})


def test_invalid_config_gives_message_not_traceback(tmp_path: Path):
    cfg_file = tmp_path / "testledger.yml"
    cfg_file.write_text("sources:\n  fix_commit_pattern: '(unclosed'\n", encoding="utf-8")
    res = CliRunner().invoke(app, ["analyze", "--config", str(cfg_file), "--skip-ci", "--skip-git"])
    assert res.exit_code == 2
    plain = re.sub(r"\x1b\[[0-9;]*m", "", res.output)  # CI forces colour (GITHUB_ACTIONS is set)
    text = " ".join(plain.replace("\u2502", " ").split())  # undo the error box's wrapping
    assert "sources.fix_commit_pattern: not a valid regular expression" in text
    assert not isinstance(res.exception, ValidationError)


def test_init_includes_fix_pattern(tmp_path: Path):
    res = CliRunner().invoke(app, ["init", "--path", str(tmp_path / "t.yml")])
    assert res.exit_code == 0
    text = (tmp_path / "t.yml").read_text(encoding="utf-8")
    assert "fix_commit_pattern:" in text
    assert Config.load(tmp_path / "t.yml").sources.fix_commit_pattern == Config().sources.fix_commit_pattern
