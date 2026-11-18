"""JUnit / TestNG / Surefire / NUnit XML parser.

Handles the common dialects:
* ``<testsuites><testsuite><testcase/>`` (Surefire, Gradle, pytest, Jest)
* bare ``<testsuite>`` root
* TestNG's ``testng-results.xml`` (``<test-method>`` elements)
* NUnit 3's ``TestResult.xml`` (``<test-run>`` / ``<test-case>``)

Run metadata (commit, attempt, timestamp) is not inside JUnit XML, so callers
pass it in, typically derived from the file path or the CI API.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path

from testledger.models import Outcome, TestResult, TestRun


def _outcome(case: ET.Element) -> tuple[Outcome, str | None]:
    for tag, outcome in (("failure", Outcome.FAILED), ("error", Outcome.ERROR), ("skipped", Outcome.SKIPPED)):
        node = case.find(tag)
        if node is not None:
            return outcome, (node.get("message") or (node.text or "").strip()[:300] or None)
    return Outcome.PASSED, None


def _testng_outcome(method: ET.Element) -> tuple[Outcome, str | None]:
    status = (method.get("status") or "PASS").upper()
    if status == "FAIL":
        exc = method.find("exception")
        return Outcome.FAILED, (exc.get("class") if exc is not None else None)
    if status == "SKIP":
        return Outcome.SKIPPED, None
    return Outcome.PASSED, None


def _nunit_outcome(case: ET.Element) -> tuple[Outcome, str | None]:
    result = (case.get("result") or "Passed").lower()
    label = (case.get("label") or "").lower()
    if result == "passed":
        return Outcome.PASSED, None
    if result in ("skipped", "ignored", "inconclusive"):
        reason = case.find("reason")
        msg = None
        if reason is not None:
            msg_node = reason.find("message")
            msg = (msg_node.text or "").strip()[:300] if msg_node is not None and msg_node.text else None
        return Outcome.SKIPPED, msg
    if result == "failed":
        failure = case.find("failure")
        msg = None
        if failure is not None:
            msg_node = failure.find("message")
            msg = (msg_node.text or "").strip()[:300] if msg_node is not None and msg_node.text else None
        if label == "error":
            return Outcome.ERROR, msg
        return Outcome.FAILED, msg
    # Default to passed for unknown results to avoid dropping tests
    return Outcome.PASSED, None


def parse_junit_xml(
    path: str | Path,
    *,
    run_id: str | None = None,
    started_at: datetime | None = None,
    commit_sha: str | None = None,
    branch: str | None = None,
    attempt: int = 1,
) -> TestRun:
    p = Path(path)
    tree = ET.parse(p)
    root = tree.getroot()
    results: list[TestResult] = []

    if root.tag == "testng-results":
        for cls in root.iter("class"):
            cls_name = cls.get("name", "UnknownClass")
            for method in cls.iter("test-method"):
                if method.get("is-config") == "true":
                    continue
                name = method.get("name", method.get("signature", "unnamed").split("(")[0])
                outcome, msg = _testng_outcome(method)
                results.append(
                    TestResult(
                        test_id=f"{cls_name}.{name}",
                        name=name,
                        suite=cls_name,
                        file=None,
                        outcome=outcome,
                        duration_s=float(method.get("duration-ms", 0)) / 1000.0,
                        message=msg,
                    )
                )
    elif root.tag == "test-run":
        # NUnit 3
        for case in root.iter("test-case"):
            fullname = case.get("fullname") or case.get("name", "unnamed")
            classname = case.get("classname") or ""
            name = case.get("name") or fullname.split(".")[-1]
            outcome, msg = _nunit_outcome(case)
            results.append(
                TestResult(
                    test_id=fullname,
                    name=name,
                    suite=classname,
                    file=None,
                    outcome=outcome,
                    duration_s=float(case.get("duration") or 0.0),
                    message=msg,
                )
            )
    elif root.tag == "test-results":
        # NUnit 2 – not fully supported; reject clearly rather than silently returning zero tests
        raise ValueError(
            "NUnit 2 TestResult.xml (root <test-results>) is not supported. "
            "Please use NUnit 3 format (root <test-run>) or convert the results."
        )
    else:
        suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
        for suite in suites:
            suite_name = suite.get("name", p.stem)
            for case in suite.findall("testcase"):
                classname = case.get("classname") or suite_name
                name = case.get("name", "unnamed")
                outcome, msg = _outcome(case)
                results.append(
                    TestResult(
                        test_id=f"{classname}.{name}",
                        name=name,
                        suite=classname,
                        file=case.get("file"),
                        outcome=outcome,
                        duration_s=float(case.get("time") or 0.0),
                        message=msg,
                    )
                )

    ts = started_at
    if ts is None:
        raw = (
            root.get("timestamp")
            or root.get("start-time")
            or (root.find("testsuite").get("timestamp") if root.find("testsuite") is not None else None)
        )
        try:
            ts = datetime.fromisoformat(raw) if raw else None
        except ValueError:
            ts = None
    if ts is None:
        ts = datetime.fromtimestamp(p.stat().st_mtime, tz=UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)

    return TestRun(
        run_id=run_id or p.stem,
        started_at=ts,
        commit_sha=commit_sha,
        branch=branch,
        attempt=attempt,
        source="junit",
        results=results,
    )
