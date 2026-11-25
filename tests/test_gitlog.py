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
from testledger.ingest.gitlog import collect_churn, collect_fix_commits, is_test_path

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
    assert "sources.fix_commit_pattern: '(unclosed' is not a valid regular expression" in text
    assert not isinstance(res.exception, ValidationError)


def test_init_includes_fix_pattern(tmp_path: Path):
    res = CliRunner().invoke(app, ["init", "--path", str(tmp_path / "t.yml")])
    assert res.exit_code == 0
    text = (tmp_path / "t.yml").read_text(encoding="utf-8")
    assert "fix_commit_pattern:" in text
    assert Config.load(tmp_path / "t.yml").sources.fix_commit_pattern == Config().sources.fix_commit_pattern


GO, CYPRESS, INTEG = "pkg/foo_test.go", "cypress/e2e/login.cy.ts", "src/integrationTest/java/CheckoutIT.java"


def test_custom_test_patterns_cover_go_cypress_and_integration_layouts():
    patterns = [r"_test\.go$", r"\.cy\.ts$", r"(^|/)integrationTest/"]
    assert all(is_test_path(p, patterns) for p in (GO, CYPRESS, INTEG))
    assert not is_test_path(INTEG)  # the default misses this layout
    assert not is_test_path("pkg/foo.go", patterns)


def test_custom_test_patterns_replace_the_default():
    only_cypress = [r"\.cy\.ts$"]
    assert is_test_path(CYPRESS, only_cypress)
    assert not is_test_path(GO, only_cypress)  # the default would count this as a test


def test_test_patterns_match_windows_paths():
    assert is_test_path(r"src\integrationTest\java\CheckoutIT.java", [r"(^|/)integrationTest/"])
    assert is_test_path(r"src\test\java\CalcTest.java")
    assert not is_test_path(r"src\main\Calc.java")


@pytest.mark.parametrize("patterns", [["(unclosed"], []])
def test_invalid_test_patterns_rejected_at_load(patterns):
    with pytest.raises(ValidationError, match="test_path_patterns"):
        Config.model_validate({"sources": {"test_path_patterns": patterns}})


def test_test_patterns_drive_churn_and_fix_commits(repo: Path):
    it = repo / INTEG
    it.parent.mkdir(parents=True)
    it.write_text("class CheckoutIT {}", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "test: add checkout IT")
    it.write_text("class CheckoutIT { int a; }", encoding="utf-8")
    (repo / "src/main/Calc.java").write_text("class Calc { int z; }", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "fix: checkout total")
    since = datetime.now(tz=UTC) - timedelta(days=1)
    patterns = [*Config().sources.test_path_patterns, r"(^|/)integrationTest/"]

    assert INTEG not in collect_churn(repo, since)
    assert INTEG in collect_churn(repo, since, patterns)  # maintenance cost

    default_fix = next(f for f in collect_fix_commits(repo, since) if f.message == "fix: checkout total")
    custom_fix = next(
        f
        for f in collect_fix_commits(repo, since, test_patterns=patterns)
        if f.message == "fix: checkout total"
    )
    assert INTEG in default_fix.files  # wrongly treated as product code
    assert custom_fix.files == ["src/main/Calc.java"]  # regression credit


@pytest.mark.parametrize("local_tz", ["UTC", "EST5EDT", "Asia/Kolkata"])
def test_since_cutoff_is_the_exact_instant(tmp_path: Path, monkeypatch, local_tz):
    # Regression: --since used to get only the UTC date, which git reads as that date at the
    # current local time of day, moving the cutoff by up to a day (flaky in US evenings).
    monkeypatch.setenv("TZ", local_tz)
    since = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)

    def commit(message: str, when: datetime) -> None:
        stamp = when.isoformat()
        env = os.environ | {"GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp}
        _git_env(tmp_path, env, "commit", "-q", "-am", message)

    _git(tmp_path, "init", "-q")
    test_file = tmp_path / "src/test/java/CalcTest.java"
    test_file.parent.mkdir(parents=True)
    (tmp_path / "src/main").mkdir(parents=True)
    product = tmp_path / "src/main/Calc.java"
    test_file.write_text("a", encoding="utf-8")
    product.write_text("a", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    commit("feat: initial", since - timedelta(days=2))
    test_file.write_text("b", encoding="utf-8")
    product.write_text("b", encoding="utf-8")
    commit("fix: before cutoff", since - timedelta(hours=1))
    test_file.write_text("c", encoding="utf-8")
    product.write_text("c", encoding="utf-8")
    commit("fix: after cutoff", since + timedelta(hours=1))

    assert [f.message for f in collect_fix_commits(tmp_path, since)] == ["fix: after cutoff"]
    assert collect_churn(tmp_path, since)["src/test/java/CalcTest.java"].commits == 1


def _git_env(repo: Path, env: dict[str, str], *args: str) -> None:
    env = env | {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=env)
