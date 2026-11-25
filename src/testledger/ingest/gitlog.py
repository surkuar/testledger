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
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from testledger.config import DEFAULT_FIX_COMMIT_PATTERN, DEFAULT_TEST_PATH_PATTERNS
from testledger.models import ChurnRecord, FixCommit


def is_test_path(path: str, patterns: Sequence[str] = DEFAULT_TEST_PATH_PATTERNS) -> bool:
    """True if ``path`` matches any pattern. Backslashes are normalised to '/' first."""
    normalised = path.replace("\\", "/")
    return any(re.search(p, normalised, re.IGNORECASE) for p in patterns)


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


def _since_arg(since: datetime) -> str:
    """``--since`` value for git: the full instant, never a bare date.

    Git reads a bare ``YYYY-MM-DD`` as that date in *local* time at the *current* time of
    day, so passing the UTC date shifts the cutoff by up to a day, and when the UTC date is
    ahead of the local one it lands only seconds in the past.
    """
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    return f"--since={since.isoformat(timespec='seconds')}"


def collect_churn(
    repo: str | Path, since: datetime, test_patterns: Sequence[str] = DEFAULT_TEST_PATH_PATTERNS
) -> dict[str, ChurnRecord]:
    """Per-file churn for test files (paths matching ``test_patterns``) touched since ``since``."""
    repo = Path(repo)
    if not git_available() or not (repo / ".git").exists():
        return {}
    log = _git(
        repo,
        "log",
        _since_arg(since),
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
        if not is_test_path(path, test_patterns):
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
    repo: str | Path,
    since: datetime,
    pattern: str = DEFAULT_FIX_COMMIT_PATTERN,
    test_patterns: Sequence[str] = DEFAULT_TEST_PATH_PATTERNS,
) -> list[FixCommit]:
    """Commits whose subject matches ``pattern`` (case-insensitive), with test files left out."""
    fix_re = re.compile(pattern, re.IGNORECASE)
    repo = Path(repo)
    if not git_available() or not (repo / ".git").exists():
        return []
    log = _git(
        repo,
        "log",
        _since_arg(since),
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
        if current and line.strip() and not is_test_path(line.strip(), test_patterns):
            current.files.append(line.strip())
    return fixes


def default_since(window_days: int) -> datetime:
    return datetime.now(tz=UTC) - timedelta(days=window_days)
