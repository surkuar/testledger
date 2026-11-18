"""GitHub Actions ingester.

Pulls workflow-run history through the REST API, downloads each run's test
report artifacts (JUnit XML or Cucumber JSON inside a zip), and yields
``TestRun`` objects with real commit SHA, attempt number and timestamps.
Re-runs of the same SHA (``run_attempt`` > 1) are how flakiness is detected.

Authentication: a token in the environment variable named by
``sources.github.token_env`` (default ``GITHUB_TOKEN``) with ``actions:read``.
"""

from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

import requests

from testledger.ingest.cucumber import parse_cucumber_json
from testledger.ingest.junit import parse_junit_xml
from testledger.models import TestRun

API = "https://api.github.com"


class GitHubActionsClient:
    def __init__(self, repo: str, token: str | None = None, session: requests.Session | None = None):
        self.repo = repo
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "testledger",
            }
        )
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def list_runs(self, workflow: str | None, since: datetime, max_runs: int = 200) -> list[dict]:
        url = f"{API}/repos/{self.repo}/actions/{'workflows/' + workflow + '/' if workflow else ''}runs"
        runs: list[dict] = []
        page = 1
        while len(runs) < max_runs:
            params: dict[str, str | int] = {
                "per_page": 100,
                "page": page,
                "created": f">={since.date().isoformat()}",
            }
            resp = self.session.get(
                url,
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            batch = resp.json().get("workflow_runs", [])
            if not batch:
                break
            runs.extend(batch)
            page += 1
        return runs[:max_runs]

    def list_artifacts(self, run_id: int) -> list[dict]:
        resp = self.session.get(f"{API}/repos/{self.repo}/actions/runs/{run_id}/artifacts", timeout=30)
        resp.raise_for_status()
        return resp.json().get("artifacts", [])

    def download_artifact(self, artifact: dict) -> bytes:
        resp = self.session.get(artifact["archive_download_url"], timeout=120)
        resp.raise_for_status()
        return resp.content


def _parse_reports_in_zip(
    blob: bytes, *, run_id: str, started_at: datetime, sha: str, branch: str, attempt: int
) -> TestRun | None:
    merged: TestRun | None = None
    with TemporaryDirectory() as tmp, zipfile.ZipFile(io.BytesIO(blob)) as zf:
        zf.extractall(tmp)
        for path in Path(tmp).rglob("*"):
            if not path.is_file():
                continue
            run: TestRun | None = None
            if path.suffix.lower() == ".xml":
                try:
                    run = parse_junit_xml(
                        path,
                        run_id=run_id,
                        started_at=started_at,
                        commit_sha=sha,
                        branch=branch,
                        attempt=attempt,
                    )
                except Exception:  # noqa: BLE001 — unrelated XML in the artifact
                    continue
            elif path.suffix.lower() == ".json" and "cucumber" in path.name.lower():
                try:
                    run = parse_cucumber_json(
                        path,
                        run_id=run_id,
                        started_at=started_at,
                        commit_sha=sha,
                        branch=branch,
                        attempt=attempt,
                    )
                except Exception:  # noqa: BLE001
                    continue
            if run and run.results:
                if merged is None:
                    merged = run
                else:
                    merged.results.extend(run.results)
    return merged


def fetch_runs(
    repo: str,
    *,
    workflow: str | None,
    since: datetime,
    token_env: str = "GITHUB_TOKEN",
    max_runs: int = 200,
    artifact_name_contains: str = "test",
    client: GitHubActionsClient | None = None,
) -> list[TestRun]:
    client = client or GitHubActionsClient(repo, token=os.environ.get(token_env))
    runs: list[TestRun] = []
    for wr in client.list_runs(workflow, since, max_runs):
        if wr.get("status") != "completed":
            continue
        for art in client.list_artifacts(wr["id"]):
            if artifact_name_contains.lower() not in art["name"].lower() or art.get("expired"):
                continue
            blob = client.download_artifact(art)
            run = _parse_reports_in_zip(
                blob,
                run_id=f"gha-{wr['id']}-{wr.get('run_attempt', 1)}",
                started_at=datetime.fromisoformat(wr["run_started_at"].replace("Z", "+00:00")),
                sha=wr["head_sha"],
                branch=wr.get("head_branch") or "",
                attempt=int(wr.get("run_attempt", 1)),
            )
            if run:
                run.source = "github-actions"
                runs.append(run)
    return runs
