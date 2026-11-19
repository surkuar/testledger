"""Allure results parser (``allure-results/`` folders from JUnit 5, TestNG, pytest, Playwright, …).

One folder is one run. Each test execution is a ``*-result.json`` file with
``fullName``, ``status``, ``start``/``stop`` (epoch milliseconds), ``labels`` and
``statusDetails``. Container files (``*-container.json``) and attachments are ignored.

Allure records retries inside a run as several results sharing a ``historyId``.
They are returned as extra ``TestRun`` objects with increasing ``attempt``, ordered
by ``start``, so the ledger engine treats a fail→pass retry like a same-SHA rerun.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from testledger.models import Outcome, TestResult, TestRun

_STATUS = {
    "passed": Outcome.PASSED,
    "failed": Outcome.FAILED,
    "broken": Outcome.ERROR,
    "skipped": Outcome.SKIPPED,
    # "unknown" (no status recorded) is not evidence either way: never a flake or a catch.
    "unknown": Outcome.SKIPPED,
}


def _label(labels: list[dict[str, Any]], name: str) -> str | None:
    for lab in labels:
        if lab.get("name") == name and lab.get("value"):
            return str(lab["value"])
    return None


def _ms_to_dt(ms: Any) -> datetime | None:
    if not isinstance(ms, int | float):
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError):  # out-of-range timestamps
        return None


def _to_result(data: dict[str, Any]) -> TestResult:
    labels = data.get("labels") or []
    name = data.get("name") or "unnamed test"
    start, stop = data.get("start"), data.get("stop")
    duration = (
        (stop - start) / 1000 if isinstance(start, int | float) and isinstance(stop, int | float) else 0.0
    )
    message = ((data.get("statusDetails") or {}).get("message") or "")[:300] or None
    return TestResult(
        test_id=data.get("fullName") or name,
        name=name,
        suite=_label(labels, "testClass") or _label(labels, "suite") or _label(labels, "package") or "",
        outcome=_STATUS.get(str(data.get("status", "")).lower(), Outcome.SKIPPED),
        duration_s=max(duration, 0.0),
        message=message,
        tags=[str(lab["value"]) for lab in labels if lab.get("name") == "tag" and lab.get("value")],
    )


def parse_allure_results(
    directory: str | Path,
    *,
    run_id: str | None = None,
    started_at: datetime | None = None,
    commit_sha: str | None = None,
    branch: str | None = None,
    attempt: int = 1,
) -> list[TestRun]:
    """Parse one ``allure-results`` folder into a run plus one run per retry round.

    Returns ``[]`` for a folder with no result files. Raises ``ValueError`` naming the
    file if a result file is not valid JSON.
    """
    folder = Path(directory)
    executions: dict[str, list[tuple[float, TestResult]]] = defaultdict(list)
    earliest: float | None = None
    for f in sorted(folder.glob("*-result.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError(f"{f.name} is not valid Allure result JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"{f.name} is not valid Allure result JSON: expected an object")
        start = data.get("start")
        order = float(start) if isinstance(start, int | float) else float("inf")
        if isinstance(start, int | float):
            earliest = start if earliest is None else min(earliest, start)
        result = _to_result(data)
        key = data.get("historyId") or result.test_id
        executions[key].append((order, result))

    if not executions:
        return []

    # Round k holds the k-th execution of every test; round 0 is the run itself.
    rounds: dict[int, list[tuple[float, TestResult]]] = defaultdict(list)
    for runs_of_test in executions.values():
        runs_of_test.sort(key=lambda item: item[0])
        for k, item in enumerate(runs_of_test):
            rounds[k].append(item)

    base_id = run_id or folder.parent.name or folder.name
    base_time = started_at or _ms_to_dt(earliest) or datetime.fromtimestamp(folder.stat().st_mtime, tz=UTC)
    out: list[TestRun] = []
    for k in sorted(rounds):
        items = rounds[k]
        if k == 0:
            when = base_time
        else:
            first = min((o for o, _ in items if o != float("inf")), default=None)
            when = max(base_time, _ms_to_dt(first) or base_time)
        out.append(
            TestRun(
                run_id=base_id if k == 0 else f"{base_id}-retry{k}",
                started_at=when,
                commit_sha=commit_sha,
                branch=branch,
                attempt=attempt + k,
                source="allure",
                results=[r for _, r in items],
            )
        )
    return out
