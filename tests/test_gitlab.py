"""GitLab client tests use only an injected fake HTTP session."""

import json
from datetime import UTC, datetime

import pytest

from testledger.ingest.gitlab import GitLabClient, fetch_pipelines
from testledger.models import Outcome

SINCE = datetime(2026, 11, 1, tzinfo=UTC)


class Response:
    def __init__(self, data, status=200):
        self.data, self.status = data, status

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(self.status)


class Session:
    def __init__(self, fixtures):
        self.headers = {}
        self.fixtures = fixtures
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if url.endswith("/pipelines"):
            name = "gitlab_pipelines.json"
        elif url.endswith("/pipelines/11/test_report") or url.endswith("/pipelines/12/test_report"):
            name = "gitlab_report.json"
        elif url.endswith("/pipelines/13/test_report"):
            return Response({"test_suites": []})
        else:
            raise AssertionError(url)
        return Response(json.loads((self.fixtures / "ci" / name).read_text(encoding="utf-8")))


def test_fetch_pipeline_reports_and_reruns(fixtures):
    session = Session(fixtures)
    client = GitLabClient("https://gitlab.example.org/", "secret", session)
    runs = fetch_pipelines("unused", "group/project", since=SINCE, client=client)
    assert [run.attempt for run in runs] == [1, 2]
    assert [run.run_id for run in runs] == ["gitlab-group_project-11", "gitlab-group_project-12"]
    assert session.calls[0][1]["sort"] == "desc"
    assert all(run.source == "gitlab" and run.commit_sha == "abc123" and run.branch == "main" for run in runs)
    assert runs[0].started_at == datetime(2026, 11, 20, 10, tzinfo=UTC)
    assert runs[0].results[0].duration_s == 1.25
    assert runs[0].results[0].test_id == "Shop.passes"
    assert runs[0].results[0].file == "tests/shop.py"
    assert [r.outcome for r in runs[0].results] == [
        Outcome.PASSED,
        Outcome.FAILED,
        Outcome.ERROR,
        Outcome.SKIPPED,
    ]
    assert runs[0].results[1].message == "assertion failed"
    assert session.headers["PRIVATE-TOKEN"] == "secret"
    assert session.calls[0][0] == "https://gitlab.example.org/api/v4/projects/group%2Fproject/pipelines"


def test_unfinished_empty_and_other_ref_pipelines_do_not_use_attempts(fixtures):
    session = Session(fixtures)
    client = GitLabClient("https://gitlab.example.org", session=session)
    runs = fetch_pipelines("unused", "group/project", since=SINCE, client=client)
    fetched = [call[0].rsplit("/", 2)[-2] for call in session.calls if call[0].endswith("/test_report")]
    assert fetched == ["11", "12", "13"]  # running, canceled and timestamp-less pipelines are never fetched
    assert [run.attempt for run in runs] == [1, 2]


def test_pipeline_without_timestamp_is_skipped(fixtures):
    client = GitLabClient("https://gitlab.example.org", session=Session(fixtures))
    runs = fetch_pipelines("unused", "group/project", since=SINCE, client=client)
    assert all(run.run_id != "gitlab-group_project-16" for run in runs)


def test_pagination_and_maximum():
    class Pages:
        headers = {}

        def get(self, url, params=None, timeout=None):
            assert params["per_page"] == 100
            return Response([{"id": i} for i in range(100)] if params["page"] == 1 else [{"id": 100}])

    client = GitLabClient("https://gitlab.example.org", session=Pages())
    assert len(client.pipelines("x", SINCE, 101)) == 101
    assert client.pipelines("x", SINCE, 0) == []


def test_errors_propagate():
    class Broken:
        headers = {}

        def get(self, *args, **kwargs):
            return Response(None, 403)

    client = GitLabClient("https://gitlab.example.org", session=Broken())
    with pytest.raises(RuntimeError, match="403"):
        client.test_report("x", 1)


def test_cli_wires_gitlab_config(monkeypatch):
    from testledger.cli import _load_ci_runs
    from testledger.config import Config

    calls = []

    def fetch(*args, **kwargs):
        calls.append((args, kwargs))
        return []

    monkeypatch.setattr("testledger.ingest.gitlab.fetch_pipelines", fetch)
    cfg = Config.model_validate({"sources": {"gitlab": {"project": "a/b", "max_pipelines": 5}}})
    assert _load_ci_runs(cfg, SINCE) == []
    assert calls[0][0] == ("https://gitlab.com", "a/b")
    assert calls[0][1]["max_pipelines"] == 5
