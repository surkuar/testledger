"""CODEOWNERS ingestion, owner resolution and memo grouping."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import NOW, make_run
from typer.testing import CliRunner

from testledger.cli import app
from testledger.config import Config
from testledger.ingest.codeowners import CodeOwners
from testledger.ledger import build_ledger
from testledger.models import Outcome
from testledger.reports import build_reports

P, F = Outcome.PASSED, Outcome.FAILED

SAMPLE = """\
# Global fallback
*                       @org/everyone

/apps/                  @org/mobile
docs/*                  @org/docs-team
*.py                    @pythonista
/src/**/*.py            @org/platform
!src/negated.py         @org/never-matched
src/[abc]/odd.py        @org/char-classes-unsupported
"""


def test_star_does_not_cross_directories():
    co = CodeOwners.parse("docs/* @d\n")
    assert co.owners_for("docs/guide.md") == ["@d"]
    # 'docs/*' matches the directory docs/sub, so its contents are owned too (gitignore rule)
    assert co.owners_for("docs/sub/deep.md") == ["@d"]
    assert co.owners_for("other/docs/guide.md") == []


def test_double_star_crosses_directories():
    co = CodeOwners.parse("/src/**/*.py @plat\n")
    assert co.owners_for("src/a/b/c.py") == ["@plat"]
    assert co.owners_for("src/c.py") == ["@plat"]
    assert co.owners_for("tests/c.py") == []


def test_directory_pattern_owns_contents():
    co = CodeOwners.parse("/apps/ @m\n")
    assert co.owners_for("apps/ios/Main.swift") == ["@m"]
    assert co.owners_for("src/apps/x") == []


def test_basename_pattern_matches_any_depth():
    co = CodeOwners.parse("*.py @p\n")
    assert co.owners_for("a/b/c.py") == ["@p"]
    assert co.owners_for("c.py") == ["@p"]
    assert co.owners_for("c.pyc") == []


def test_last_matching_pattern_wins():
    co = CodeOwners.parse(SAMPLE)
    assert co.owners_for("src/shop/cart.py") == ["@org/platform"]
    assert co.owners_for("apps/ios/Main.swift") == ["@org/mobile"]
    assert co.owners_for("README.md") == ["@org/everyone"]
    # '!' negation and '[...]' character classes are unsupported: those lines never match
    assert CodeOwners.parse("!src/negated.py @n\n").owners_for("src/negated.py") == []
    assert CodeOwners.parse("src/[abc]/odd.py @x\n").owners_for("src/a/odd.py") == []
    assert co.owners_for("src/negated.py") == ["@org/platform"]
    assert co.owners_for("src/a/odd.py") == ["@org/platform"]


def test_windows_paths_and_multiple_owners():
    co = CodeOwners.parse("/src/ @a @b @org/team\n")
    assert co.owners_for("src\\test\\java\\CartTest.java") == ["@a", "@b", "@org/team"]


def test_load_detection_order_and_explicit_path(tmp_path: Path):
    (tmp_path / "CODEOWNERS").write_text("* @root\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/CODEOWNERS").write_text("* @docs\n", encoding="utf-8")
    assert CodeOwners.load(tmp_path).owners_for("x") == ["@root"]
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/CODEOWNERS").write_text("* @gh\n", encoding="utf-8")
    assert CodeOwners.load(tmp_path).owners_for("x") == ["@gh"]
    assert CodeOwners.load(tmp_path, "docs/CODEOWNERS").owners_for("x") == ["@docs"]
    assert CodeOwners.load(tmp_path / "empty") is None
    assert CodeOwners.load(tmp_path, "missing") is None


def _runs_with_files():
    f1, f2 = "src/test/java/com/acme/CartTest.java", "src/test/java/com/acme/OrderTest.java"
    runs = [make_run("r1", "sha1", 1, 10, {"com.acme.CartTest.adds": P}, file=f1)]
    runs += [
        make_run(f"r{i}", f"sha{i}", 1, i, {"com.acme.OrderTest.places": F}, file=f2) for i in range(2, 9)
    ]
    return runs


def test_engine_fills_owners_and_builder_groups_by_team(cfg: Config):
    co = CodeOwners.parse("/src/test/java/com/acme/CartTest.java @org/cart\n")
    ledger = build_ledger(_runs_with_files(), cfg, codeowners=co, now=NOW)
    assert ledger.accounts["com.acme.CartTest.adds"].owners == ["@org/cart"]
    assert ledger.accounts["com.acme.OrderTest.places"].owners == []

    bundle = build_reports(ledger, cfg)
    assert set(bundle.balance_sheet["by_owner"]) == {"@org/cart", "(unowned)"}
    assert bundle.balance_sheet["by_owner"]["@org/cart"]["tests"] == 1
    rows = bundle.ledger_json["accounts"]
    assert all("file" in row and "owners" in row for row in rows)
    assert next(r for r in rows if r["test_id"] == "com.acme.CartTest.adds")["owners"] == ["@org/cart"]
    assert "| @org/cart |" in bundle.memo_markdown
    assert "@org/cart" in bundle.memo_html


def test_no_codeowners_means_no_team_section(cfg: Config):
    bundle = build_reports(build_ledger(_runs_with_files(), cfg, now=NOW), cfg)
    assert bundle.balance_sheet["by_owner"] == {}
    assert "By team" not in bundle.memo_markdown
    assert "Team" not in bundle.memo_html


def test_cli_groups_memo_by_team(tmp_path: Path, fixtures: Path):
    codeowners = tmp_path / "CODEOWNERS"
    codeowners.write_text("/src/test/java/ @org/qe\n", encoding="utf-8")
    d = tmp_path / "ci" / "20261001-aaaaaaa-attempt1"
    d.mkdir(parents=True)
    (d / "TEST-results.xml").write_text(
        (fixtures / "junit/surefire.xml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "testledger.yml").write_text(
        "org_name: T\nwindow_days: 3650\nsources:\n  junit_globs: ['ci/**/TEST-*.xml']\n"
        "  cucumber_globs: []\n  git_repo: null\n"
        f"  codeowners: {codeowners}\n",
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
    bs = json.loads((tmp_path / "out/balance_sheet.json").read_text(encoding="utf-8"))
    assert bs["by_owner"]["@org/qe"]["tests"] == 12
    memo = (tmp_path / "out/memo.md").read_text(encoding="utf-8")
    assert "| @org/qe |" in memo


def test_cli_codeowners_null_disables(tmp_path: Path, fixtures: Path):
    d = tmp_path / "ci" / "20261001-aaaaaaa-attempt1"
    d.mkdir(parents=True)
    (d / "TEST-results.xml").write_text(
        (fixtures / "junit/surefire.xml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    (tmp_path / "testledger.yml").write_text(
        "org_name: T\nwindow_days: 3650\nsources:\n  junit_globs: ['ci/**/TEST-*.xml']\n"
        "  cucumber_globs: []\n  git_repo: null\n  codeowners: null\n",
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
    bs = json.loads((tmp_path / "out/balance_sheet.json").read_text(encoding="utf-8"))
    assert bs["by_owner"] == {}
