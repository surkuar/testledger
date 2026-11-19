# TestLedger — Design Document

*Architecture, data flow, decision logic, and technology choices.*
*All diagrams are Mermaid and render natively on GitHub.*

---

## 1. Problem statement

Quality-engineering leaders cannot answer, with data, three questions their executives ask:

1. **What does our test estate cost us?** — not the CI bill, but the full carrying cost including engineer time lost to flaky failures and test maintenance.
2. **What is it worth?** — which tests have actually stopped a defect reaching production.
3. **Where should the next dollar go?** — retire, rewrite at a cheaper layer, stabilise, or protect.

Existing tools detect flaky tests or ask leaders to type guesses into an ROI calculator. TestLedger computes the answer from data the organisation already has: CI results and git history.

## 2. Goals and non-goals

| Goals | Non-goals (v1) |
|---|---|
| Framework-agnostic (JUnit, TestNG, Cucumber, pytest, Jest …) | Replacing a test-management system |
| CI-agnostic (local folders, GitHub Actions, Jenkins) | Real-time dashboards (the JSON output feeds those) |
| Zero changes to test code or pipelines | Precise accounting — the model is *comparative*, not an invoice |
| Platform-independent (Windows/macOS/Linux, Python ≥ 3.11) | Deep static analysis of test code |
| Executive-readable output | Auto-deleting tests (humans decide; the tool recommends) |
| Every assumption visible and overridable | |

## 3. System context

```mermaid
flowchart TB
  subgraph Sources["Data the organisation already has"]
    J[JUnit / TestNG XML]
    C[Cucumber JSON]
    AL[Allure results]
    GH[GitHub Actions API]
    JK[Jenkins API]
    GIT[(git history)]
    SRC[Test source files]
    MUT[Mutation report<br/>PIT / Stryker / mutmut]
  end
  subgraph TL["TestLedger"]
    ING[Ingest layer]
    ENG[Ledger engine]
    AI[AI-test governance]
    REP[Report builder]
  end
  subgraph Out["Outputs"]
    MEMO[Quality Investment Memo<br/>md + html]
    BS[balance_sheet.json]
    IS[income_statement.json]
    WO[writeoffs.json]
    AIL[ai_liabilities.json]
    LJ[ledger.json]
    PR[PR comment / job summary]
  end
  J & C & AL & GH & JK --> ING
  GIT --> ING
  SRC & MUT --> AI
  ING --> ENG
  AI --> ENG
  ENG --> REP
  REP --> MEMO & BS & IS & WO & AIL & LJ & PR
```

## 4. Component architecture

```
src/testledger/
├── cli.py                 Typer CLI: init · analyze · memo · pr-comment
├── config.py              testledger.yml schema (pydantic) — rates, thresholds, sources
├── models.py              Domain model: TestRun, TestResult, ChurnRecord, FixCommit,
│                          LedgerEntry, TestAccount, Ledger, Verdict
├── ingest/
│   ├── junit.py           JUnit / Surefire / TestNG XML → TestRun
│   ├── cucumber.py        Cucumber JSON → TestRun
│   ├── allure.py          allure-results/ folder → TestRun (+ one per retry round)
│   ├── gitlog.py          git log → churn per test file, fix commits on product code
│   ├── github_actions.py  REST client: runs → artifacts → zip → TestRun (with run_attempt)
│   └── jenkins.py         JSON API: builds → testReport → TestRun (same SHA = rerun)
├── ledger/
│   └── engine.py          Books debits/credits per test, detects flakes, credits
│                          regressions, assigns verdicts
├── aitest/
│   └── detector.py        AI provenance markers, assertion density, mutation-score join
├── reports/
│   └── builder.py         Balance sheet, income statement, write-offs, memo, ledger.json
└── templates/
    ├── memo.md.j2         Markdown memo
    └── memo.html.j2       Self-contained HTML memo
```

```mermaid
classDiagram
  class TestRun {
    +str run_id
    +datetime started_at
    +str commit_sha
    +str branch
    +int attempt
    +str source
    +list results
  }
  class TestResult {
    +str test_id
    +str name
    +str suite
    +str file
    +Outcome outcome
    +float duration_s
    +str message
    +list tags
  }
  class ChurnRecord {
    +str file
    +int commits
    +int lines_changed
    +int authors
  }
  class FixCommit {
    +str sha
    +datetime timestamp
    +str message
    +list files
  }
  class LedgerEntry {
    +str test_id
    +EntryKind kind
    +float amount_usd
    +float quantity
    +str unit
  }
  class TestAccount {
    +str test_id
    +Layer layer
    +int executions
    +float flake_rate
    +int regressions_caught
    +Verdict verdict
    +bool ai_generated
    +float mutation_score
    +total_cost() float
    +total_value() float
    +net() float
    +roi() float
  }
  class Ledger {
    +datetime window_start
    +datetime window_end
    +int runs_analysed
    +dict accounts
    +total_cost() float
    +by_verdict(v) list
  }
  TestRun "1" --> "*" TestResult
  Ledger "1" --> "*" TestAccount
  TestAccount "1" --> "*" LedgerEntry
  ChurnRecord ..> TestAccount : maintenance cost
  FixCommit ..> TestAccount : regression credit
```

## 5. End-to-end pipeline

```mermaid
flowchart TD
  S([testledger analyze]) --> CFG[Load testledger.yml<br/>defaults if absent]
  CFG --> LOC[Glob local JUnit / Cucumber reports<br/>and Allure results folders]
  LOC --> META{Folder name has<br/>date + sha + attempt?}
  META -- yes --> RUN1[TestRun with sha / attempt / date]
  META -- no --> RUN2[TestRun with mtime, no rerun detection]
  CFG --> CI{sources.github /<br/>sources.jenkins set?}
  CI -- yes --> API[Fetch run history + reports via API]
  CI -- no --> SKIP1[skip]
  CFG --> GITQ{git_repo set and<br/>git available?}
  GITQ -- yes --> CHURN[git log --numstat on test paths<br/>sources.test_path_patterns<br/>→ ChurnRecord per file]
  GITQ -- yes --> FIX[git log on product paths<br/>subject matches fix pattern<br/>→ FixCommit list]
  GITQ -- yes --> SCAN[Scan test sources:<br/>AI markers + assertion density]
  SCAN --> MUTJ{--mutation given?}
  MUTJ -- yes --> JOIN[Join mutation scores<br/>FooTest ↔ Foo]
  RUN1 & RUN2 & API --> ENG[[Ledger engine]]
  CHURN & FIX & JOIN --> ENG
  ENG --> REP[[Report builder]]
  REP --> OUT[(testledger-out/)]
  OUT --> SUMM[Rich summary table on stdout]
  OUT --> EXIT{--fail-on-quarantine<br/>and quarantine > 0?}
  EXIT -- yes --> E2([exit 2])
  EXIT -- no --> E0([exit 0])
```

## 6. Ledger engine logic

### 6.1 Flake detection (no framework changes needed)

A flake is a **same-commit fail → pass transition**. CI systems rerun the same SHA (GitHub `run_attempt`, Jenkins "rebuild"); that rerun is the only ground truth that nothing changed.

```mermaid
flowchart LR
  A[Group runs by commit_sha] --> B[Sort by attempt, then time]
  B --> C[For each test: sequence of outcomes]
  C --> D{fail followed by pass<br/>within same SHA?}
  D -- yes --> E[flake += 1<br/>mark sha,test as flaky pair]
  D -- no --> F[not a flake]
```

### 6.2 Regression credit

```mermaid
flowchart LR
  A[Failure on attempt 1] --> B{sha,test in<br/>flaky pairs?}
  B -- yes --> X[Discard: false alarm]
  B -- no --> C[Genuine failure at time t]
  C --> D{Fix commit on product code<br/>with t ≤ t_fix ≤ t + fix_window_days?}
  D -- no --> Y[No credit]
  D -- yes --> E[regressions_caught += 1<br/>credit regression_value_usd<br/>consume that fix commit for this test]
```

Design choices:
* **Only attempt-1 failures count.** A failure that only shows on a rerun is noise.
* **A fix commit can be consumed once per test** so a single fix does not credit the same test repeatedly, but *can* credit several tests that all failed on it (they all provided signal).
* **Fix window** is `thresholds.fix_window_days` in `testledger.yml` (default 3 days, the typical fix lead time on a red main branch). Raise it for weekly release trains, lower it if fixes land within hours. It is printed in the memo's assumptions.
* **Fix pattern** is `sources.fix_commit_pattern` in `testledger.yml`, a case-insensitive regex matched against the commit subject. The default is `\b(fix|fixes|fixed|bug|hotfix|regression|defect|patch)\b`, so conventional-commit `fix:` prefixes match naturally. Teams using incident-ticket prefixes can set e.g. `^INC-\d+`. An invalid regex is rejected when the config loads.

### 6.3 Booking entries

| Kind | Side | Formula |
|---|---|---|
| `ci_compute` | debit | Σ duration (min) × `ci_minute_usd` |
| `rerun_compute` | debit | executions on attempt > 1 × avg duration (min) × `ci_minute_usd` |
| `triage_time` | debit | flakes × `triage_minutes_per_false_alarm` / 60 × `engineer_hourly_usd` |
| `maintenance` | debit | (lines changed in test file ÷ tests sharing the file) × `maintenance_minutes_per_line` / 60 × `engineer_hourly_usd` |
| `regression_caught` | credit | regressions_caught × `regression_value_usd` |
| `risk_coverage` | credit | optional flat `risk_coverage_value_usd` for stable tests (default 0) |

### 6.4 Verdict decision tree

```mermaid
flowchart TD
  A{executions ≥<br/>min_executions_for_verdict?} -- no --> HOLD1[HOLD<br/>insufficient data]
  A -- yes --> B{flake_rate ≥<br/>quarantine_flake_rate?}
  B -- yes --> Q[QUARANTINE]
  B -- no --> C{regressions_caught = 0?}
  C -- yes --> D{net < 0 and<br/>cost ≥ retire_min_cost_usd?}
  D -- yes --> R[RETIRE]
  D -- no --> HOLD2[HOLD<br/>cheap safety net]
  C -- no --> E{layer = UI and<br/>cost/catch ≥ ratio × service median?}
  E -- yes --> DM[DEMOTE<br/>rewrite lower in pyramid]
  E -- no --> F{flake ≥ fix_flake_rate<br/>or avg duration ≥ slow?}
  F -- yes --> FX[FIX]
  F -- no --> G{value > 0 and<br/>roi ≥ asset_roi or cost ≈ 0?}
  G -- yes --> AS[ASSET]
  G -- no --> HOLD3[HOLD]
```

### 6.5 Layer classification

Substring rules in `testledger.yml → layers` map a test id or path to `unit | service | ui`. Defaults recognise `unit`, `/test/`, `api`, `integration`, `contract`, `ui`, `e2e`, `selenium`, `playwright`, `cucumber`, `features/`. UI is checked first because UI paths often also contain "test".

## 7. AI-generated test governance

```mermaid
flowchart LR
  SRC[Walk test dirs<br/>java kt py js ts rb cs go scala] --> T[Count test functions]
  SRC --> AS[Count assertion idioms]
  T & AS --> DEN[assertion density = assertions / tests]
  SRC --> MK{File or last commit<br/>contains AI marker?}
  MK -- yes --> AIG[ai_generated = true]
  MUT[Mutation report] --> JOIN[Join FooTest ↔ Foo]
  JOIN --> MS[mutation_score]
  AIG & DEN & MS --> ACC[Attach to TestAccount]
  ACC --> LIAB{ai_generated and<br/>mutation < 0.5 or density < 0.8?}
  LIAB -- yes --> L[Unverified liability<br/>listed in memo §6]
```

Rationale: high line coverage with low mutation score is the signature of an assertion that mirrors the implementation ("test theater"). A test that cannot fail on a real defect is pure carrying cost; AI generators produce these at scale, so they get their own section in the memo.

## 8. Report builder

```mermaid
flowchart LR
  L[(Ledger)] --> S[summary: totals, ROI, verdict counts,<br/>annualised savings]
  L --> BS[balance sheet: assets / liabilities,<br/>by layer, by suite]
  L --> IS[income statement: cost by kind vs value]
  L --> WO[write-offs: quarantine → retire → demote → fix]
  L --> AIL[ai liabilities]
  S & BS & IS & WO & AIL --> J2[Jinja2 templates]
  J2 --> MD[memo.md] & HTML[memo.html]
  S & BS & IS & WO & AIL --> JSON[*.json + ledger.json]
```

Annualised savings = (cost of retire + quarantine tests in window) × 365 / window_days; demote savings assume the rewritten test costs 1/`demote_layer_cost_ratio` of the original.

## 9. GitHub Action

Composite action (`action.yml`): sets up Python, installs the package from the action path, runs `analyze`, writes `pr-comment.md`, appends the memo to `$GITHUB_STEP_SUMMARY`, uploads the output folder as an artifact, and upserts one PR comment (found by its heading, so it never spams).

```mermaid
sequenceDiagram
  participant W as Workflow
  participant A as testledger action
  participant G as GitHub API
  W->>A: uses: surkuar/testledger@v0.1.0
  A->>A: pip install . then testledger analyze
  A->>A: testledger pr-comment → pr-comment.md
  A->>W: $GITHUB_STEP_SUMMARY += memo.md
  A->>G: upload-artifact testledger-reports
  alt pull_request
    A->>G: find existing comment, update it or create one
  end
```

## 10. Technology choices

| Concern | Choice | Why |
|---|---|---|
| Language | **Python 3.11+** | Ships on every OS, `pip install` is the lowest-friction adoption path, excellent XML/JSON/subprocess stdlib |
| CLI | **Typer** + **Rich** | Typed options, auto help, readable terminal tables; `CliRunner` makes the CLI unit-testable |
| Validation | **pydantic v2** | Config and domain models validated at the boundary; JSON dump for free |
| Config | **YAML** (PyYAML) | Human-editable, comment-friendly, familiar to CI users |
| Templates | **Jinja2** | Markdown and HTML memo from one context; autoescape for HTML |
| HTTP | **requests** | Minimal, well known; clients accept an injected `session` so tests run offline |
| Git | **`git` CLI via subprocess** | No native dependency, identical behaviour on Windows/macOS/Linux, works with any git version |
| XML | **`xml.etree`** (stdlib) | No lxml build step; JUnit/TestNG dialects are simple |
| Packaging | **setuptools + pyproject** | `pip install git+…` works; wheel built in CI |
| Quality | **pytest**, **ruff**, **mypy**, **CI matrix 3 OS × 3 Python** | Proves platform independence on every push |
| Distribution | PyPI-ready package + composite GitHub Action | Two adoption paths: CLI anywhere, zero-config in Actions |

## 11. Platform independence checklist

* No path string concatenation — `pathlib` everywhere; globs via `glob(recursive=True)`.
* All file I/O specifies `encoding="utf-8"` (Windows default is not UTF-8).
* Git invoked as `git -C <repo> …` with `errors="replace"` so non-UTF-8 commit messages never crash.
* No shell features in subprocess calls (argument lists, `shell=False`).
* Timestamps are timezone-aware UTC throughout; naive timestamps are coerced.
* CI matrix runs the full demo on Windows, macOS and Linux.

## 12. Extension points

| Want to… | Do this |
|---|---|
| Add a CI system (GitLab, Azure DevOps) | New module in `ingest/` returning `list[TestRun]`; wire into `cli._load_ci_runs` |
| Add a result format (Allure, NUnit) | New parser returning `TestRun`; add glob to `Sources` |
| Change the cost model | `ledger/engine.py → build_ledger` books entries; add an `EntryKind` |
| Change verdict rules | `ledger/engine.py → _assign_verdicts`; thresholds in `config.Thresholds` |
| Re-skin the memo | `templates/memo.*.j2` |
| Feed a dashboard | Consume `ledger.json` (stable schema: `summary`, `assumptions`, `accounts[].entries[]`) |

## 13. Security and privacy

* Tokens are read only from environment variables named in config — never stored in files or output.
* Output contains test ids, durations, costs; no source code, no secrets.
* The GitHub Action needs `actions:read` (history) and `pull-requests:write` (comment) only.

## 14. Limitations (honest list)

* Value is credited only when a fix commit is detectable; teams that squash-merge without "fix" wording will under-count value — set `sources.fix_commit_pattern` or use conventional commits.
* Maintenance churn is apportioned evenly across tests in a file.
* Mutation-score join is by file-stem convention (`FooTest` ↔ `Foo`); unusual layouts need a manual `{file: score}` JSON.
* The model is comparative: use it to rank and reallocate, not to invoice.
