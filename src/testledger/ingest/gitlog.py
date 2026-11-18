"""Git history analysis via the ``git`` CLI (works on Windows, macOS, Linux).

Two signals are extracted:
* **Churn** on test files — how often and how heavily each test file was edited.
  Heavy churn is the maintenance cost of a test.
* **Fix commits** on product code — commits whose message looks like a bug fix.
  A test that failed shortly *before* such a commit touched related code is
  credited with having caught the regression.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from testledger.config import DEFAULT_FIX_COMMIT_PATTERN
from testledger.models import ChurnRecord, FixCommit

TEST_PATH_PATTERN = re.compile(
    r"(^|/)(test|tests|spec|specs|features|e2e|__tests__)(/|$)|_test\.|Test\.|\.spec\.|\.feature$",
    re.IGNORECASE,
)


def git_available() -> bool:
    return shutil.which("git") is not None


def _git(repo: Path, *args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {out.stderr.strip()}")
    return out.stdout


def collect_churn(repo: str | Path, since: datetime) -> dict[str, ChurnRecord]:
    """Per-file churn for test files touched since ``since``."""
    repo = Path(repo)
    if not git_available() or not (repo / ".git").exists():
        return {}
    log = _git(
        repo,
        "log",
        f"--since={since.date().isoformat()}",
        "--numstat",
        "--diff-filter=M",  # modifications only: authoring a new test is not maintenance
        "--format=COMMIT%x09%H%x09%an%x09%aI",
    )
    records: dict[str, ChurnRecord] = {}
    authors: dict[str, set[str]] = {}
    current_author = ""
    current_ts: datetime | None = None
    for line in log.splitlines():
        if line.startswith("COMMIT\t"):
            _, _sha, current_author, ts = line.split("\t")
            current_ts = datetime.fromisoformat(ts)
            continue
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, deleted, path = parts
        if not TEST_PATH_PATTERN.search(path):
            continue
        rec = records.setdefault(path, ChurnRecord(file=path))
        rec.commits += 1
        rec.lines_changed += (int(added) if added.isdigit() else 0) + (
            int(deleted) if deleted.isdigit() else 0
        )
        authors.setdefault(path, set()).add(current_author)
        if current_ts and (rec.last_touched is None or current_ts > rec.last_touched):
            rec.last_touched = current_ts
    for path, rec in records.items():
        rec.authors = len(authors.get(path, set()))
    return records


def collect_fix_commits(
    repo: str | Path, since: datetime, pattern: str = DEFAULT_FIX_COMMIT_PATTERN
) -> list[FixCommit]:
    """Product-code commits whose subject matches ``pattern`` (case-insensitive)."""
    fix_re = re.compile(pattern, re.IGNORECASE)
    repo = Path(repo)
    if not git_available() or not (repo / ".git").exists():
        return []
    log = _git(
        repo,
        "log",
        f"--since={since.date().isoformat()}",
        "--name-only",
        "--format=COMMIT%x09%H%x09%aI%x09%s",
    )
    fixes: list[FixCommit] = []
    current: FixCommit | None = None
    for line in log.splitlines():
        if line.startswith("COMMIT\t"):
            _, sha, ts, subject = line.split("\t", 3)
            current = (
                FixCommit(sha=sha, timestamp=datetime.fromisoformat(ts), message=subject)
                if fix_re.search(subject)
                else None
            )
            if current:
                fixes.append(current)
            continue
        if current and line.strip() and not TEST_PATH_PATTERN.search(line):
            current.files.append(line.strip())
    return fixes


def default_since(window_days: int) -> datetime:
    return datetime.now(tz=UTC) - timedelta(days=window_days)
