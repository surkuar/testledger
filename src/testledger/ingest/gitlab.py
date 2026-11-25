"""GitLab pipeline test-report ingestion. Network calls accept an injected session.

Only finished pipelines (``success`` / ``failed``) are ingested, newest first up to
``max_pipelines``. A rerun is a later pipeline for the same ``(sha, ref, source)``; branch,
merge-request, tag and schedule pipelines on one commit are never counted as reruns of each other.
Known limitations: GitLab's job "Retry" stays inside one pipeline and the pipeline test report only
shows the latest attempt, so those retries are not seen; tests in child/downstream pipelines are
not followed.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from urllib.parse import quote

import requests

from testledger.models import Outcome, TestResult, TestRun

log = logging.getLogger(__name__)

_FINISHED = {"success", "failed"}

_STATUS = {
    "success": Outcome.PASSED,
    "failed": Outcome.FAILED,
    "error": Outcome.ERROR,
    "skipped": Outcome.SKIPPED,
    "disabled": Outcome.SKIPPED,
}


class GitLabClient:
    def __init__(self, base_url: str, token: str | None = None, session=None):
        self.base = base_url.rstrip("/") + "/api/v4"
        self.session = session if session is not None else requests.Session()
        self.session.headers["User-Agent"] = "testledger"
        if token:
            self.session.headers["PRIVATE-TOKEN"] = token

    def pipelines(self, project: str, since: datetime, max_pipelines: int = 100) -> list[dict]:
        found: list[dict] = []
        page = 1
        page_size = min(100, max_pipelines)
        while len(found) < max_pipelines:
            params: dict[str, str | int] = {
                "updated_after": since.isoformat(),
                "order_by": "id",
                "sort": "desc",
                "per_page": page_size,
                "page": page,
            }
            response = self.session.get(
                f"{self.base}/projects/{quote(str(project), safe='')}/pipelines",
                params=params,
                timeout=30,
            )
            response.raise_for_status()
            rows = response.json()
            if not rows:
                break
            found.extend(rows[: max_pipelines - len(found)])
            if len(rows) < page_size:
                break
            page += 1
        return found

    def test_report(self, project: str, pipeline_id: int) -> dict:
        response = self.session.get(
            f"{self.base}/projects/{quote(str(project), safe='')}/pipelines/{pipeline_id}/test_report",
            timeout=60,
        )
        response.raise_for_status()
        return response.json()


def fetch_pipelines(
    base_url: str,
    project: str,
    *,
    since: datetime,
    token_env: str = "GITLAB_TOKEN",
    max_pipelines: int = 100,
    client: GitLabClient | None = None,
) -> list[TestRun]:
    client = client if client is not None else GitLabClient(base_url, os.environ.get(token_env))
    runs = []
    attempts: dict[tuple[str | None, str | None, str | None], int] = {}
    for pipeline in sorted(client.pipelines(project, since, max_pipelines), key=lambda p: p["id"]):
        if pipeline.get("status") not in _FINISHED:
            continue
        raw_time = pipeline.get("started_at") or pipeline.get("created_at") or pipeline.get("updated_at")
        if not raw_time:
            log.warning("Skipping GitLab pipeline %s: no run timestamp", pipeline["id"])
            continue
        timestamp = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        if timestamp < since:
            continue
        report = client.test_report(project, pipeline["id"])
        results = []
        for suite in report.get("test_suites", []):
            for case in suite.get("test_cases", []):
                classname = case.get("classname") or suite.get("name", "")
                name = case.get("name", "unnamed")
                results.append(
                    TestResult(
                        test_id=f"{classname}.{name}",
                        name=name,
                        suite=classname,
                        file=case.get("file"),
                        duration_s=float(case.get("execution_time") or 0),
                        outcome=_STATUS.get(case.get("status", ""), Outcome.SKIPPED),
                        message=(case.get("system_output") or "")[:300] or None,
                    )
                )
        if not results:
            continue
        sha = pipeline.get("sha")
        attempt = 1
        if sha:
            key = (sha, pipeline.get("ref"), pipeline.get("source"))
            attempts[key] = attempts.get(key, 0) + 1
            attempt = attempts[key]
        runs.append(
            TestRun(
                run_id=f"gitlab-{project.replace('/', '_')}-{pipeline['id']}",
                started_at=timestamp,
                commit_sha=sha,
                branch=pipeline.get("ref"),
                attempt=attempt,
                source="gitlab",
                results=results,
            )
        )
    return runs
