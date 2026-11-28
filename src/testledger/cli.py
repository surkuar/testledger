"""TestLedger command-line interface.

testledger init                      # write a starter testledger.yml
testledger analyze --out reports/    # ingest, book, report
testledger memo reports/             # print the memo to the terminal
testledger pr-comment reports/       # emit a Markdown comment for a PR
testledger diff old.json new.json    # compare two saved ledger reports
"""

from __future__ import annotations

import glob
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table
from rich.text import Text

from testledger import __version__
from testledger.aitest import attach_mutation_scores, load_mutation_report, scan_sources
from testledger.config import Config
from testledger.ingest import (
    collect_churn,
    collect_fix_commits,
    parse_allure_results,
    parse_cucumber_json,
    parse_junit_xml,
)
from testledger.ingest.gitlog import default_since
from testledger.ledger import build_ledger
from testledger.models import TestRun
from testledger.reports import build_reports, write_reports
from testledger.reports.diff import Snapshot, compare_ledgers, diff_markdown, diff_tables

app = typer.Typer(
    add_completion=False, help="TestLedger — Test Estate Balance Sheet.", invoke_without_command=True
)
console = Console()

RUN_META = re.compile(r"(?P<sha>[0-9a-f]{7,40})[-_](?:attempt|run|a)(?P<attempt>\d+)$", re.IGNORECASE)
RUN_DATE = re.compile(
    r"^(?P<y>20\d{2})-?(?P<m>\d{2})-?(?P<d>\d{2})(?:T(?P<H>\d{2}):?(?P<M>\d{2}))?(?=[-_.]|$)"
)


def _meta_from_path(p: Path) -> tuple[str | None, int, datetime | None]:
    """Convention for local report folders: reports/<YYYYMMDD>[THHMM]-<sha>-attempt<N>/junit.xml.

    Date and SHA are optional; without a date the file's mtime is used, and
    without a SHA no rerun/flake detection is possible for that run.
    """
    sha, attempt, ts = None, 1, None
    for part in reversed(p.parts[:-1]):
        m = RUN_META.search(part)
        if m and sha is None:
            sha, attempt = m.group("sha"), int(m.group("attempt"))
        d = RUN_DATE.search(part)
        if d and ts is None:
            try:
                ts = datetime(
                    int(d["y"]),
                    int(d["m"]),
                    int(d["d"]),
                    int(d["H"] or 0),
                    int(d["M"] or 0),
                    tzinfo=UTC,
                )
            except ValueError:
                ts = None
        if sha and ts:
            break
    return sha, attempt, ts


def _glob_unique(root: Path, patterns: list[str]) -> list[str]:
    """Matches for every pattern, each path once, in first-match order.

    A file matched by two patterns (e.g. ``surefire-reports/TEST-Foo.xml`` by both
    ``**/surefire-reports/*.xml`` and ``**/TEST-*.xml``) would otherwise be ingested
    twice and double its runs and costs.
    """
    seen: dict[Path, str] = {}
    for pattern in patterns:
        for match in glob.glob(str(root / pattern), recursive=True):
            seen.setdefault(Path(match).resolve(), match)
    return list(seen.values())


def _load_local_runs(cfg: Config, root: Path) -> list[TestRun]:
    runs: list[TestRun] = []
    for f in _glob_unique(root, cfg.sources.junit_globs):
        sha, attempt, ts = _meta_from_path(Path(f))
        try:
            runs.append(
                parse_junit_xml(
                    f, commit_sha=sha, attempt=attempt, started_at=ts, run_id=str(Path(f).parent.name)
                )
            )
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]skip {f}: {exc}[/yellow]")
    for f in _glob_unique(root, cfg.sources.cucumber_globs):
        sha, attempt, ts = _meta_from_path(Path(f))
        try:
            runs.append(
                parse_cucumber_json(
                    f, commit_sha=sha, attempt=attempt, started_at=ts, run_id=str(Path(f).parent.name)
                )
            )
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]skip {f}: {exc}[/yellow]")
    for d in _glob_unique(root, cfg.sources.allure_globs):
        folder = Path(d)
        if not folder.is_dir():
            continue
        # _meta_from_path reads the parents of its argument, so pass a path inside the folder.
        sha, attempt, ts = _meta_from_path(folder / "_")
        try:
            runs.extend(
                parse_allure_results(
                    folder, commit_sha=sha, attempt=attempt, started_at=ts, run_id=folder.parent.name
                )
            )
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]skip {d}: {exc}[/yellow]")
    return runs


def _load_ci_runs(cfg: Config, since: datetime) -> list[TestRun]:
    runs: list[TestRun] = []
    gh = cfg.sources.github
    if gh and gh.get("repo"):
        from testledger.ingest.github_actions import fetch_runs

        console.print(f"Fetching GitHub Actions runs for {gh['repo']} …")
        runs += fetch_runs(
            gh["repo"],
            workflow=gh.get("workflow"),
            since=since,
            token_env=gh.get("token_env", "GITHUB_TOKEN"),
            max_runs=int(gh.get("max_runs", 200)),
            artifact_name_contains=gh.get("artifact_name_contains", "test"),
        )
    jk = cfg.sources.jenkins
    if jk and jk.get("url") and jk.get("job"):
        from testledger.ingest.jenkins import fetch_builds

        console.print(f"Fetching Jenkins builds for {jk['job']} …")
        runs += fetch_builds(
            jk["url"],
            jk["job"],
            since=since,
            user_env=jk.get("user_env", "JENKINS_USER"),
            token_env=jk.get("token_env", "JENKINS_TOKEN"),
            max_builds=int(jk.get("max_builds", 100)),
        )
    az = cfg.sources.azure_devops
    if az and az.get("org_url") and az.get("project"):
        from testledger.ingest.azure_devops import fetch_builds as fetch_azdo_builds

        console.print(f"Fetching Azure DevOps builds for {az['project']} …")
        runs += fetch_azdo_builds(
            az["org_url"],
            az["project"],
            since=since,
            token_env=az.get("token_env", "AZURE_DEVOPS_TOKEN"),
            definition=az.get("definition"),
            max_builds=int(az.get("max_builds", 100)),
        )
    return runs


def _replace_unencodable_output() -> None:
    """Print '?' for characters the console can't encode instead of crashing.

    The memo contains characters such as '≈' that legacy encodings (e.g. cp1252 on a
    Windows console or redirected stdout) can't represent. Files are still written as UTF-8.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(errors="replace")


@app.callback()
def _root(
    ctx: typer.Context,
    version: bool = typer.Option(False, "--version", "-V", help="Show version and exit"),
) -> None:
    _replace_unencodable_output()
    if version:
        console.print(f"testledger {__version__}")
        raise typer.Exit()
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit()


@app.command()
def init(
    path: Path = typer.Option(Path("testledger.yml"), "--path", help="Where to write the config"),
) -> None:
    """Write a starter testledger.yml with documented defaults."""
    if path.exists():
        console.print(f"[red]{path} already exists[/red]")
        raise typer.Exit(1)
    path.write_text(Config().dump_example(), encoding="utf-8")
    console.print(f"Wrote {path}. Edit rates and sources, then run: testledger analyze")


def _load_config(path: Path | None) -> Config:
    """Load testledger.yml, turning validation errors into a message naming each bad setting."""
    if path is None:
        return Config()
    try:
        return Config.load(path)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg'].removeprefix('Value error, ')}"
            for err in exc.errors()
        )
        raise typer.BadParameter(f"Invalid {path}: {problems}", param_hint="--config") from exc


@app.command()
def analyze(
    config: Path | None = typer.Option(None, "--config", "-c", help="testledger.yml (defaults if omitted)"),
    reports_root: Path = typer.Option(
        Path("."), "--reports", "-r", help="Folder searched for local JUnit/Cucumber reports"
    ),
    out: Path = typer.Option(Path("testledger-out"), "--out", "-o", help="Output folder"),
    mutation_report: Path | None = typer.Option(
        None, "--mutation", help="PIT XML / Stryker JSON / {file: score} JSON"
    ),
    skip_ci: bool = typer.Option(False, "--skip-ci", help="Do not call GitHub/Jenkins APIs"),
    skip_git: bool = typer.Option(False, "--skip-git", help="Do not run git for churn/fix analysis"),
    fail_on_quarantine: bool = typer.Option(
        False, "--fail-on-quarantine", help="Exit 2 if any test is quarantine-grade"
    ),
) -> None:
    """Ingest test results + git history, book the ledger, write reports."""
    cfg = _load_config(config)
    now = datetime.now(tz=UTC)
    since = default_since(cfg.window_days)

    runs = _load_local_runs(cfg, reports_root)
    if not skip_ci:
        runs += _load_ci_runs(cfg, since)
    if not runs:
        console.print(
            "[red]No test runs found. Point --reports at JUnit/Cucumber output or configure sources.github / sources.jenkins.[/red]"
        )
        raise typer.Exit(1)

    churn, fixes, ai_signals = {}, [], {}
    repo = cfg.sources.git_repo
    if repo and not skip_git:
        src = cfg.sources
        churn = collect_churn(repo, since, src.test_path_patterns)
        fixes = collect_fix_commits(repo, since, src.fix_commit_pattern, src.test_path_patterns)
        ai_signals = scan_sources(repo, cfg.ai_generated_markers)
        if mutation_report:
            attach_mutation_scores(ai_signals, load_mutation_report(mutation_report))

    ledger = build_ledger(runs, cfg, churn=churn, fixes=fixes, ai_signals=ai_signals, now=now)
    bundle = build_reports(ledger, cfg)
    written = write_reports(bundle, out)
    _print_summary(bundle.summary)
    console.print(f"\nReports written to [bold]{out}[/bold]: " + ", ".join(written))
    if fail_on_quarantine and bundle.summary["verdicts"].get("quarantine"):
        raise typer.Exit(2)


def _print_summary(s: dict) -> None:
    t = Table(title=f"TestLedger — {s['org']} (last {s['window_days']} days)")
    t.add_column("Metric")
    t.add_column("Value", justify="right")
    for k in (
        "runs_analysed",
        "tests",
        "total_cost_usd",
        "total_value_usd",
        "net_usd",
        "portfolio_roi",
        "regressions_caught",
        "annualised_writeoff_savings_usd",
        "annualised_demote_savings_usd",
        "ai_unverified_liabilities",
    ):
        t.add_row(k.replace("_", " "), str(s.get(k)))
    for v, n in sorted(s["verdicts"].items()):
        t.add_row(f"verdict: {v}", str(n))
    console.print(t)


def _read_ledger_snapshot(path: Path) -> Snapshot:
    try:
        return Snapshot.read(path)
    except UnicodeDecodeError as exc:
        raise typer.BadParameter(f"Save {path} as UTF-8 JSON and retry.") from exc
    except OSError as exc:
        raise typer.BadParameter(f"Could not read {path}: {exc.strerror}") from exc
    except ValidationError as exc:
        problem = exc.errors(include_input=False)[0]["msg"]
        raise typer.BadParameter(f"Invalid ledger at {path}: {problem}") from exc


@app.command()
def diff(
    old: Path = typer.Argument(..., exists=True, dir_okay=False, help="Previous ledger.json"),
    new: Path = typer.Argument(..., exists=True, dir_okay=False, help="Current ledger.json"),
    out: Path | None = typer.Option(None, "--out", help="Write the comparison as Markdown"),
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable JSON"),
) -> None:
    """Compare two saved ledger reports."""
    report = compare_ledgers(_read_ledger_snapshot(old), _read_ledger_snapshot(new))
    if out is not None:
        if out.resolve() in {old.resolve(), new.resolve()}:
            raise typer.BadParameter("Choose an output path different from either ledger.json input.")
        out.write_text(diff_markdown(report), encoding="utf-8")
    if json_output:
        typer.echo(json.dumps(report, indent=2))
        return
    for title, headers, rows in diff_tables(report):
        table = Table(*headers, title=title)
        for row in rows:
            table.add_row(*(Text(cell) for cell in row))
        if not rows:
            table.add_row("None")
        console.print(table)


@app.command()
def memo(out: Path = typer.Argument(Path("testledger-out"), help="Output folder from `analyze`")) -> None:
    """Print the Quality Investment Memo (Markdown)."""
    p = out / "memo.md"
    if not p.exists():
        console.print(f"[red]{p} not found — run `testledger analyze` first[/red]")
        raise typer.Exit(1)
    console.print(p.read_text(encoding="utf-8"))


@app.command("pr-comment")
def pr_comment(
    out: Path = typer.Argument(Path("testledger-out")),
    max_rows: int = typer.Option(10, help="Rows in the write-off table"),
) -> None:
    """Emit a compact Markdown comment for pull requests / Slack."""
    s = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    w = json.loads((out / "writeoffs.json").read_text(encoding="utf-8"))[:max_rows]
    lines = [
        "### 📒 TestLedger — Test Estate Balance Sheet",
        f"**Net:** ${s['net_usd']:,.0f} on ${s['total_cost_usd']:,.0f} carrying cost · ROI {s['portfolio_roi']} · {s['regressions_caught']} regressions caught in {s['window_days']} days",
        f"Annualised savings available: **${s['annualised_writeoff_savings_usd'] + s['annualised_demote_savings_usd']:,.0f}** "
        f"({s['verdicts'].get('retire', 0) + s['verdicts'].get('quarantine', 0)} retire/quarantine, {s['verdicts'].get('demote', 0)} demote)",
        "",
        "| Test | Verdict | Flake | Cost | Value |",
        "|---|---|---:|---:|---:|",
    ]
    for a in w:
        lines.append(
            f"| `{a['test_id']}` | {a['verdict']} | {a['flake_rate'] * 100:.0f}% | ${a['cost_usd']:.2f} | ${a['value_usd']:.2f} |"
        )
    if s.get("ai_unverified_liabilities"):
        lines.append(
            f"\n⚠️ {s['ai_unverified_liabilities']} AI-generated tests are below governance thresholds (see `ai_liabilities.json`)."
        )
    typer.echo("\n".join(lines))


if __name__ == "__main__":
    app()
