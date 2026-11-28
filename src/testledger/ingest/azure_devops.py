"""Azure DevOps Pipelines ingester.

Azure DevOps keeps test results in *test runs* linked to builds. This client
reads builds through ``_apis/build/builds`` (optionally filtered by
``definitions`` and ``minTime``), finds the test runs of each build through
``_apis/test/runs?buildUri=…``, and pulls per-test results from
``_apis/test/Runs/{id}/results`` (all with ``api-version=7.1``).

Re-running a failed stage creates a new test run on the same build, and a new
build can target the same commit: both produce several runs for one SHA, which
is how TestLedger counts reruns (attempt 2, 3, …), the same way the Jenkins
ingester does it.

Authentication: a Personal Access Token read from the environment variable
named by ``sources.azure_devops.token_env`` (default ``AZURE_DEVOPS_TOKEN``),
sent as the password of an HTTP basic-auth pair (any user name works).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import requests

from testledger.models import Outcome, TestResult, TestRun

API_VERSION = "7.1"

_OUTCOME = {
    "PASSED": Outcome.PASSED,
    "FAILED": Outcome.FAILED,
    "ERROR": Outcome.ERROR,
    "TIMEOUT": Outcome.ERROR,
    "ABORTED": Outcome.ERROR,
    "NOTEXECUTED": Outcome.SKIPPED,
    "INCONCLUSIVE": Outcome.SKIPPED,
    "NOTIMPACTED": Outcome.SKIPPED,
    "BLOCKED": Outcome.SKIPPED,
    "WARNING": Outcome.SKIPPED,
}


class AzureDevOpsClient:
    def __init__(
        self,
        org_url: str,
        project: str,
        token: str | None = None,
        session: requests.Session | None = None,
    ):
        self.base = f"{org_url.rstrip('/')}/{project}"
        self.session = session or requests.Session()
        if token:
            # Azure DevOps PATs go in the basic-auth password; the user name is ignored.
            self.session.auth = ("", token)
        self.session.headers["User-Agent"] = "testledger"

    def builds(
        self,
        definition: str | int | None = None,
        since: datetime | None = None,
        max_builds: int = 100,
    ) -> list[dict]:
        params: dict[str, str | int] = {"api-version": API_VERSION, "$top": max_builds}
        if definition is not None:
            params["definitions"] = definition
        if since is not None:
            params["minTime"] = since.astimezone(UTC).isoformat()
        resp = self.session.get(f"{self.base}/_apis/build/builds", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", [])

    def test_runs(self, build_uri: str) -> list[dict]:
        params: dict[str, str] = {"buildUri": build_uri, "api-version": API_VERSION}
        resp = self.session.get(f"{self.base}/_apis/test/runs", params=params, timeout=30)
        resp.raise_for_status()
        return resp.json().get("value", [])

    def run_results(self, run_id: int, max_results: int = 10000) -> list[dict]:
        params: dict[str, str | int] = {"api-version": API_VERSION, "$top": max_results}
        resp = self.session.get(f"{self.base}/_apis/test/Runs/{run_id}/results", params=params, timeout=60)
        resp.raise_for_status()
        return resp.json().get("value", [])


def _when(iso: str | None, fallback: datetime) -> datetime:
    if not iso:
        return fallback
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(UTC)


def _result(case: dict) -> TestResult:
    test_id = case.get("automatedTestName") or case.get("testCase", {}).get("name", "")
    outcome = _OUTCOME.get(str(case.get("outcome", "")).upper(), Outcome.PASSED)
    return TestResult(
        test_id=test_id,
        name=test_id.rsplit(".", 1)[-1],
        suite=test_id.rsplit(".", 1)[0] if "." in test_id else "",
        outcome=outcome,
        duration_s=float(case.get("durationInMs") or 0.0) / 1000.0,
        message=(case.get("errorMessage") or "")[:300] or None,
    )


def fetch_builds(
    org_url: str,
    project: str,
    *,
    since: datetime,
    token_env: str = "AZURE_DEVOPS_TOKEN",
    definition: str | int | None = None,
    max_builds: int = 100,
    client: AzureDevOpsClient | None = None,
) -> list[TestRun]:
    client = client or AzureDevOpsClient(org_url, project, os.environ.get(token_env))
    runs: list[TestRun] = []
    # The API returns newest first; reverse so attempts count upward in time.
    builds = sorted(
        client.builds(definition, since, max_builds),
        key=lambda b: b.get("startTime") or b.get("queueTime") or "",
    )
    for build in builds:
        build_start = _when(
            build.get("startTime") or build.get("finishTime") or build.get("queueTime"), since
        )
        if build_start < since:
            continue
        for tr in client.test_runs(build["uri"]):
            results = [_result(c) for c in client.run_results(tr["id"])]
            if not results:
                continue
            runs.append(
                TestRun(
                    run_id=f"azdo-{build['id']}-{tr['id']}",
                    started_at=_when(tr.get("startedDate") or tr.get("createdDate"), build_start),
                    commit_sha=build.get("sourceVersion"),
                    branch=build.get("sourceBranch"),
                    source="azure-devops",
                    results=results,
                )
            )
    # Several test runs on one build, or several builds on one SHA, are reruns:
    # count attempts upward per (build, then SHA) in time order.
    runs.sort(key=lambda r: r.started_at)
    seen: dict[str, int] = {}
    for r in runs:
        key = r.commit_sha or r.run_id.rsplit("-", 1)[0]
        seen[key] = seen.get(key, 0) + 1
        r.attempt = seen[key]
    return runs
