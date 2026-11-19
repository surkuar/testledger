"""Ingesters normalise heterogeneous CI/test sources into ``TestRun`` objects."""

from testledger.ingest.allure import parse_allure_results
from testledger.ingest.cucumber import parse_cucumber_json
from testledger.ingest.gitlog import collect_churn, collect_fix_commits
from testledger.ingest.junit import parse_junit_xml

__all__ = [
    "parse_junit_xml",
    "parse_cucumber_json",
    "parse_allure_results",
    "collect_churn",
    "collect_fix_commits",
]
