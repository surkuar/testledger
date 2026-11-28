"""CI client tests run fully offline using stub HTTP sessions."""

from __future__ import annotations

import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from testledger.ingest.azure_devops import AzureDevOpsClient
from testledger.ingest.azure_devops import fetch_builds as fetch_azdo_builds
from testledger.ingest.github_actions import GitHubActionsClient, fetch_runs
from testledger.ingest.jenkins import JenkinsClient, fetch_builds
from testledger.models import Outcome


class _Resp:
    def __init__(self, payload, status=200, raw: bytes | None = None):
        self._payload, self.status_code, self.content = payload, status, raw or b""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _GHSession:
    headers: dict = {}

    def __init__(self, fixtures: Path):
        self.fixtures = fixtures
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.write(fixtures / "junit/surefire.xml", "TEST-results.xml")
            zf.write(fixtures / "cucumber/cucumber.json", "cucumber.json")
        self.zip = buf.getvalue()

    def get(self, url, params=None, timeout=None):
        if url.endswith("/runs"):
            if params.get("page", 1) > 1:
                return _Resp({"workflow_runs": []})
            return _Resp(json.loads((self.fixtures / "ci/gha_runs.json").read_text(encoding="utf-8")))
        if url.endswith("/artifacts"):
            return _Resp(
                {
                    "artifacts": [
                        {
                            "id": 1,
                            "name": "test-results",
                            "expired": False,
                            "archive_download_url": "https://x/zip",
                        }
                    ]
                }
            )
        if url.endswith("/zip"):
            return _Resp(None, raw=self.zip)
        raise AssertionError(url)


def test_github_actions_fetch_runs_detects_attempts(fixtures: Path):
    client = GitHubActionsClient("acme/shop", token="t", session=_GHSession(fixtures))
    runs = fetch_runs("acme/shop", workflow="ci.yml", since=datetime(2026, 9, 1, tzinfo=UTC), client=client)
    assert len(runs) == 2
    assert {r.attempt for r in runs} == {1, 2} and all(r.commit_sha == "abc1234def" for r in runs)
    assert all(r.source == "github-actions" for r in runs)
    assert len(runs[0].results) == 15  # 12 junit + 3 cucumber merged


class _JSession:
    headers: dict = {}
    auth = None

    def __init__(self, fixtures: Path):
        self.fixtures = fixtures

    def get(self, url, params=None, timeout=None):
        if url.endswith("/job/acme-main/api/json"):
            return _Resp(json.loads((self.fixtures / "ci/jenkins_job.json").read_text(encoding="utf-8")))
        if "/11/testReport" in url:
            return _Resp(
                json.loads((self.fixtures / "ci/jenkins_report_fail.json").read_text(encoding="utf-8"))
            )
        if "/12/testReport" in url:
            return _Resp(
                json.loads((self.fixtures / "ci/jenkins_report_pass.json").read_text(encoding="utf-8"))
            )
        return _Resp(None, status=404)


def test_jenkins_fetch_builds_counts_same_sha_as_rerun(fixtures: Path):
    client = JenkinsClient("https://ci.acme.com", session=_JSession(fixtures))
    runs = fetch_builds(
        "https://ci.acme.com", "acme-main", since=datetime(2026, 9, 1, tzinfo=UTC), client=client
    )
    assert [r.attempt for r in runs] == [1, 2]
    first, second = runs
    assert first.failed == 1 and second.failed == 0
    assert first.branch == "origin/main" and first.source == "jenkins"


class _AZSession:
    headers: dict = {}
    auth = None

    def __init__(self, fixtures: Path):
        self.fixtures = fixtures

    def get(self, url, params=None, timeout=None):
        params = params or {}
        if url.endswith("/_apis/build/builds"):
            assert params.get("api-version") == "7.1"
            return _Resp(json.loads((self.fixtures / "ci/azdo_builds.json").read_text(encoding="utf-8")))
        if url.endswith("/_apis/test/runs"):
            build_uri = params.get("buildUri", "")
            if build_uri.endswith("/101"):
                return _Resp(
                    json.loads((self.fixtures / "ci/azdo_runs_101.json").read_text(encoding="utf-8"))
                )
            if build_uri.endswith("/102"):
                return _Resp(
                    json.loads((self.fixtures / "ci/azdo_runs_102.json").read_text(encoding="utf-8"))
                )
            return _Resp({"value": []})
        for run_id in (5001, 5002, 5003):
            if url.endswith(f"/_apis/test/Runs/{run_id}/results"):
                return _Resp(
                    json.loads((self.fixtures / f"ci/azdo_results_{run_id}.json").read_text(encoding="utf-8"))
                )
        raise AssertionError(url)


def test_azure_devops_fetch_builds_maps_outcomes_and_attempts(fixtures: Path):
    client = AzureDevOpsClient("https://dev.azure.com/acme", "shop", session=_AZSession(fixtures))
    runs = fetch_azdo_builds(
        "https://dev.azure.com/acme",
        "shop",
        since=datetime(2026, 9, 1, tzinfo=UTC),
        client=client,
    )
    assert len(runs) == 3
    # Two test runs on build 101 (same SHA) count as attempts 1 and 2;
    # build 102 is a different SHA, so its run is attempt 1 again.
    assert [(r.run_id, r.attempt) for r in runs] == [
        ("azdo-101-5001", 1),
        ("azdo-101-5002", 2),
        ("azdo-102-5003", 1),
    ]
    first = runs[0]
    assert first.source == "azure-devops"
    assert first.commit_sha == "aaaaaaa1111" and first.branch == "refs/heads/main"
    by_id = {r.test_id: r for r in first.results}
    assert by_id["Shop.Tests.CartTest.adds_item"].outcome == Outcome.FAILED
    assert by_id["Shop.Tests.CartTest.adds_item"].duration_s == 1.5
    assert by_id["Shop.Tests.CartTest.adds_item"].message == "Expected 2 but was 1"
    assert by_id["Shop.Tests.CartTest.adds_item"].suite == "Shop.Tests.CartTest"
    assert by_id["Shop.Tests.CartTest.empties"].outcome == Outcome.PASSED
    assert by_id["Shop.Tests.CartTest.guest_checkout"].outcome == Outcome.SKIPPED
    assert first.failed == 1 and runs[1].failed == 0


def test_azure_devops_client_sends_pat_as_basic_auth(fixtures: Path):
    session = _AZSession(fixtures)
    client = AzureDevOpsClient("https://dev.azure.com/acme", "shop", token="secret-pat", session=session)
    assert session.auth == ("", "secret-pat")
    assert client.base == "https://dev.azure.com/acme/shop"
