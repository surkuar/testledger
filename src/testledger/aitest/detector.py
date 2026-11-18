"""AI-generated test governance ("test theater" detection).

Three signals, each cheap and language-agnostic:

1. **Provenance** — the test file contains a configured marker
   (``@ai-generated``, ``Generated-By:``, Copilot/Claude co-author trailers)
   or its last commit message does.
2. **Assertion density** — assertions per test function. Tests with density
   < threshold are likely "smoke only" and prove little. Detects common
   assertion idioms across Java (JUnit/TestNG/AssertJ/Hamcrest), Python,
   JavaScript/TypeScript, Ruby (RSpec/Minitest), C#, Go.
3. **Mutation score** — read from PIT (Java), Stryker (JS/TS/C#),
   mutmut/cosmic-ray (Python) reports. A high line-coverage, low
   mutation-score test is the signature of an assertion that mirrors the
   implementation rather than checking it.

Output is a mapping ``{file_or_test_id: {"ai_generated": bool,
"assertion_density": float, "mutation_score": float | None}}`` that the ledger
engine merges into each account. The memo lists AI-generated tests that fall
below the governance thresholds as **unverified liabilities**.
"""

from __future__ import annotations

import json
import re
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

TEST_FUNC_PATTERNS = [
    re.compile(r"@Test\b"),  # JUnit / TestNG
    re.compile(r"^\s*def\s+test_\w+", re.M),  # pytest / unittest
    re.compile(r"\b(it|test)\s*\(\s*['\"`]", re.M),  # jest / mocha / vitest
    re.compile(r"^\s*(it|scenario|specify)\s+['\"]", re.M),  # rspec
    re.compile(r"\[(Fact|Theory|Test|TestMethod)\]"),  # xUnit / NUnit / MSTest
    re.compile(r"^func\s+Test\w+\s*\(", re.M),  # go
]
ASSERTION_PATTERNS = [
    re.compile(r"\bassert\w*\s*\("),  # assertEquals(, assertTrue(, assert(
    re.compile(r"\bassertThat\s*\("),
    re.compile(r"^\s*assert\s+\S", re.M),  # python bare assert
    re.compile(r"\bexpect\s*\("),  # jest / rspec / chai
    re.compile(r"\bshould\b"),
    re.compile(r"\bAssert\.\w+\s*\("),  # C#
    re.compile(r"\bverify\s*\("),  # mockito interaction verification
    re.compile(r"\.(toBe|toEqual|toMatch|toHaveLength|toThrow|isEqualTo|hasSize|contains|isTrue|isFalse)\b"),
    re.compile(r"\bt\.(Errorf|Fatalf|Error|Fatal)\s*\("),  # go
]
SOURCE_EXT = {".java", ".kt", ".py", ".js", ".ts", ".tsx", ".jsx", ".rb", ".cs", ".go", ".scala"}


def assertion_density(text: str) -> tuple[int, int, float]:
    tests = sum(len(p.findall(text)) for p in TEST_FUNC_PATTERNS)
    asserts = sum(len(p.findall(text)) for p in ASSERTION_PATTERNS)
    density = round(asserts / tests, 2) if tests else 0.0
    return tests, asserts, density


def _last_commit_message(repo: Path, file: Path) -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%B", "--", str(file.relative_to(repo))],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        return out.stdout if out.returncode == 0 else ""
    except (OSError, ValueError):
        return ""


def scan_sources(
    repo: str | Path, markers: list[str], *, test_dirs: list[str] | None = None
) -> dict[str, dict]:
    """Walk test source files and compute provenance + assertion density."""
    repo = Path(repo)
    test_dirs = test_dirs or ["test", "tests", "src/test", "spec", "features", "e2e", "__tests__"]
    signals: dict[str, dict] = {}
    seen: set[Path] = set()
    for d in test_dirs:
        base = repo / d
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.suffix.lower() not in SOURCE_EXT or not path.is_file() or path in seen:
                continue
            seen.add(path)
            text = path.read_text(encoding="utf-8", errors="replace")
            tests, asserts, density = assertion_density(text)
            if tests == 0:
                continue
            commit_msg = _last_commit_message(repo, path)
            ai = any(m in text or m in commit_msg for m in markers)
            rel = path.relative_to(repo).as_posix()
            signals[rel] = {
                "ai_generated": ai,
                "test_functions": tests,
                "assertions": asserts,
                "assertion_density": density,
                "mutation_score": None,
            }
    return signals


def load_mutation_report(path: str | Path) -> dict[str, float]:
    """Return {source_file: mutation_score} from PIT XML, Stryker JSON or mutmut JSON."""
    p = Path(path)
    if not p.exists():
        return {}
    if p.suffix.lower() == ".xml":  # PIT mutations.xml
        root = ET.parse(p).getroot()
        totals: dict[str, list[int]] = {}
        for m in root.iter("mutation"):
            src = m.findtext("sourceFile") or "unknown"
            killed = (m.get("status") or "").upper() in ("KILLED", "TIMED_OUT")
            t = totals.setdefault(src, [0, 0])
            t[0] += 1
            t[1] += int(killed)
        return {k: round(v[1] / v[0], 3) for k, v in totals.items() if v[0]}
    data = json.loads(p.read_text(encoding="utf-8"))
    if "files" in data:  # Stryker mutation-report schema
        out: dict[str, float] = {}
        for file, info in data["files"].items():
            muts = info.get("mutants", [])
            if not muts:
                continue
            n_killed = sum(1 for m in muts if m.get("status") in ("Killed", "Timeout"))
            out[file] = round(n_killed / len(muts), 3)
        return out
    if isinstance(data, dict) and all(isinstance(v, (int, float)) for v in data.values()):
        return {k: float(v) for k, v in data.items()}  # simple {file: score}
    return {}


def attach_mutation_scores(signals: dict[str, dict], mutation: dict[str, float]) -> None:
    """Best-effort join: a test file 'FooTest.java' is matched to source 'Foo.java'."""
    if not mutation:
        return
    by_stem = {Path(k).stem.lower(): v for k, v in mutation.items()}
    for test_file, sig in signals.items():
        stem = Path(test_file).stem.lower()
        for suffix in ("test", "tests", "spec", "_test", "_spec", ".test", ".spec"):
            if stem.endswith(suffix):
                candidate = stem[: -len(suffix)].rstrip("_.")
                if candidate in by_stem:
                    sig["mutation_score"] = by_stem[candidate]
                    break
        if sig["mutation_score"] is None and stem.startswith("test_") and stem[5:] in by_stem:
            sig["mutation_score"] = by_stem[stem[5:]]
