from __future__ import annotations

from datetime import UTC, datetime

from testledger.ingest import parse_cucumber_json, parse_junit_xml
from testledger.models import Outcome


def test_parse_surefire_xml(fixtures):
    run = parse_junit_xml(fixtures / "junit/surefire.xml", run_id="r1", commit_sha="abc", attempt=1)
    assert run.run_id == "r1" and run.commit_sha == "abc" and run.source == "junit"
    assert len(run.results) == 12
    ids = {r.test_id for r in run.results}
    assert "com.acme.unit.PriceCalculatorTest.applies_discount" in ids
    failed = [r for r in run.results if r.outcome.is_failure]
    assert len(failed) == 2 and all(r.message for r in failed)
    assert all(r.file for r in run.results)


def test_parse_testng_xml(fixtures):
    run = parse_junit_xml(fixtures / "junit/testng-results.xml")
    by = {r.test_id: r for r in run.results}
    assert set(by) == {
        "com.acme.LoginTest.validLogin",
        "com.acme.LoginTest.lockedAccount",
        "com.acme.LoginTest.sso",
    }
    assert by["com.acme.LoginTest.lockedAccount"].outcome == Outcome.FAILED
    assert by["com.acme.LoginTest.sso"].outcome == Outcome.SKIPPED
    assert by["com.acme.LoginTest.validLogin"].duration_s == 1.2


def test_parse_cucumber_json(fixtures):
    ts = datetime(2026, 9, 15, tzinfo=UTC)
    run = parse_cucumber_json(fixtures / "cucumber/cucumber.json", started_at=ts, commit_sha="abc", attempt=2)
    assert run.source == "cucumber" and run.attempt == 2 and run.started_at == ts
    assert len(run.results) == 3
    checkout = [r for r in run.results if r.suite == "Checkout"]
    assert len(checkout) == 2 and all("@ui" in r.tags for r in checkout)
    assert all(r.file == "features/checkout.feature" for r in checkout)
    assert all(r.duration_s > 0 for r in run.results)


def test_parse_nunit3_xml(fixtures):
    run = parse_junit_xml(fixtures / "nunit/nunit3.xml")
    assert run.source == "junit"
    assert len(run.results) == 5
    by = {r.test_id: r for r in run.results}
    assert "NUnit.Tests.CalculatorTests.Add_PositiveNumbers" in by
    assert by["NUnit.Tests.CalculatorTests.Add_PositiveNumbers"].outcome == Outcome.PASSED
    assert by["NUnit.Tests.CalculatorTests.Add_PositiveNumbers"].duration_s == 0.1
    assert by["NUnit.Tests.CalculatorTests.Divide_ByZero"].outcome == Outcome.ERROR
    assert "DivideByZeroException" in (by["NUnit.Tests.CalculatorTests.Divide_ByZero"].message or "")
    assert by["NUnit.Tests.CalculatorTests.Subtract_NegativeResult"].outcome == Outcome.FAILED
    assert by["NUnit.Tests.CalculatorTests.IgnoredTest"].outcome == Outcome.SKIPPED
    assert by["NUnit.Tests.CalculatorTests.Add_Parameterized(1,2,3)"].outcome == Outcome.PASSED
    # timestamp from start-time
    assert run.started_at.year == 2026 and run.started_at.month == 9
