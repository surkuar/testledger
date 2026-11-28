"""JUnit / TestNG / Surefire XML parser.

Handles the common dialects:
* ``<testsuites><testsuite><testcase/>`` (Surefire, Gradle, pytest, Jest)
* NUnit 3 ``<test-run>`` root (NUnit 2 is rejected explicitly)
* bare ``<testsuite>`` root
* TestNG's ``testng-results.xml`` (``<test-method>`` elements)

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


def _nunit_file(classname: str) -> str | None:
    """NUnit results carry no source path. Approximate one from the class's namespace (C# convention)."""
    if not classname or classname == "UnknownClass":
        return None
    return classname.split("+")[0].replace(".", "/") + ".cs"


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

    if root.tag == "test-results":
        raise ValueError("NUnit 2 XML is unsupported; export NUnit 3 TestResult.xml instead")
    if root.tag == "test-run":
        statuses = {
            "Passed": Outcome.PASSED,
            "Warning": Outcome.PASSED,  # passed, but emitted Assert.Warn / Warn.If
            "Failed": Outcome.FAILED,
            "Skipped": Outcome.SKIPPED,
            "Inconclusive": Outcome.SKIPPED,
        }
        for case in root.iter("test-case"):
            name = case.get("name", "unnamed")
            status = case.get("result", "Inconclusive")
            outcome = statuses.get(status, Outcome.SKIPPED)
            if status == "Failed":
                label = case.get("label")
                if label == "Error":
                    outcome = Outcome.ERROR
                elif label == "Cancelled":  # the run was aborted: the test never really ran
                    outcome = Outcome.SKIPPED
                elif label == "Invalid":  # NotRunnable (bad signature, missing TestCaseSource, ...)
                    outcome = Outcome.ERROR
            classname = case.get("classname", "UnknownClass")
            message = case.findtext("failure/message") or case.findtext("reason/message")
            results.append(
                TestResult(
                    test_id=case.get("fullname") or f"{classname}.{name}",
                    name=name,
                    suite=classname,
                    file=_nunit_file(classname),
                    outcome=outcome,
                    duration_s=float(case.get("duration") or 0.0),
                    message=message.strip()[:300] if message else None,
                )
            )
    elif root.tag == "testng-results":
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
        first_suite = root.find("testsuite")
        raw = (root.get("start-time") if root.tag == "test-run" else root.get("timestamp")) or (
            first_suite.get("timestamp") if first_suite is not None else None
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
