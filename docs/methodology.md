# Methodology — Treating a Test Suite as a Portfolio

## 1. The accounting metaphor, taken seriously

A test is an asset that was bought once (authoring) and that charges rent forever (execution, maintenance, attention). Like any asset it can become a liability through neglect, duplication, or a changing environment. A portfolio manager does not ask "is this asset green today?" but "what did it cost me this quarter, what did it return, and should I hold, fix, or sell?"

TestLedger books every test as an account over a rolling window (default 90 days).

## 2. Debits — the carrying cost

| Cost | Why it is real | Source of the number |
|---|---|---|
| **CI compute** | Every execution burns runner minutes | duration in the result file × your per-minute rate |
| **Rerun compute** | A red build is re-run; the whole job pays again | executions on attempt > 1 |
| **Triage time** | The largest hidden cost. An engineer opens the failure, reads logs, decides "probably flaky", re-runs | flaky false alarms × minutes per false alarm × fully-loaded hourly rate |
| **Maintenance** | Brittle tests are edited every time the product moves | lines *modified* in existing test files in the window (git; creating a new test is authoring, not maintenance), × minutes per line × hourly rate. Test files are those matching `sources.test_path_patterns` |

Industrial studies put flake rates at 4–16 % of tests and show that teams shift effort from investigation to blind reruns precisely because triage is expensive; TestLedger makes that cost visible per test.

Defaults (override in `testledger.yml`): engineer $75/h fully loaded, CI $0.008/min, 18 minutes per false alarm, 1.5 minutes per changed line.

### Which files are tests

`sources.test_path_patterns` (a list of case-insensitive regexes; a path matching any one is a test, matched with `/` separators on every OS) decides this, and it matters **twice**:

* **Maintenance cost:** only edits to matching files are booked as test churn.
* **Regression credit:** matching files are left out of fix commits, so a commit that only edits tests is not product code.

The default covers common layouts (`test/`, `tests/`, `spec/`, `e2e/`, `__tests__/`, `features/`, `*_test.*`, `*Test.*`, `*.spec.*`, `*.feature`). If your tests live elsewhere, e.g. `src/integrationTest/`, add a pattern such as `(^|/)integrationTest/`. Setting the list replaces the default, so include it if you still need it.

## 3. Credits — the return

A test earns its keep when it **fails on a real defect before production**. TestLedger infers this from two independent signals:

1. the test failed on the **first attempt** of a commit and the failure was **not** cleared by re-running the same commit (so it is not a flake), and
2. within **`thresholds.fix_window_days`** (default **3 days**) a **product-code** commit landed whose subject reads like a fix.

Each such event is worth `regression_value_usd` — your organisation's cost of an escaped defect (debug time, hotfix, customer impact, incident). The default $2,000 is deliberately conservative; many organisations use $5–50k for customer-facing systems.

An optional `risk_coverage_value_usd` lets you recognise a flat "safety net" value for every stable test. It defaults to 0 so the memo stays evidence-based; turn it on if you want to reflect option value.

## 4. Flake detection without instrumenting anything

Most flake detectors need a plugin, a retry wrapper, or a vendor SaaS. TestLedger uses the one signal every CI system already produces: **the same commit was executed more than once.** If a test failed on attempt 1 and passed on attempt 2 of the same SHA, nothing changed but the weather — that is a flake, by definition. GitHub Actions exposes `run_attempt`; Jenkins exposes the built SHA per build; for local folders you encode the SHA and attempt in the directory name.

Flake rate is computed **per commit tested** (flaky commits ÷ first-attempt executions). Counting the reruns themselves in the denominator would dilute the very signal the reruns reveal: a test that fails on 4 of 10 commits is 40 % flaky, not 4 ÷ 14 = 29 %.

## 5. Verdicts

| Verdict | Meaning | Rule |
|---|---|---|
| **Quarantine** | Actively misleading | flake rate ≥ 30 % |
| **Retire** | Pure carrying cost | no regressions caught, net negative, cost above the floor (`retire_min_cost_usd`) |
| **Demote** | Right coverage, wrong layer | UI test whose cost per regression caught is ≥ 3× the median of service/unit tests that also caught regressions |
| **Fix** | Valuable but noisy | caught regressions, but flake ≥ 10 % or average duration ≥ 60 s |
| **Asset** | Earning | ROI ≥ 0.5, or caught a regression at ~zero cost |
| **Hold** | Not enough evidence, or cheap safety net | fewer than 5 executions, or no catches but cheap |

Thresholds are opinions. They are printed in the memo so they can be argued with.

## 6. Why "demote" is the most valuable verdict

A UI test that catches a real regression is not a bad test — it is an expensive way to get a signal that a service- or unit-level test could usually give for a fraction of the cost and with far less flakiness. "Demote" is the lever that moves a test estate down the pyramid *based on evidence* rather than dogma: only tests that have proven their value, and whose cost per catch is far above the cheaper layer's, are nominated.

## 7. AI-generated tests and "test theater"

AI assistants generate tests quickly, and those tests often pass because their assertions mirror the implementation rather than check it. Line coverage cannot see this; **mutation score** can — if a test cannot kill injected faults it would not catch a real one either. TestLedger marks tests as AI-generated from provenance markers (`@ai-generated`, generator trailers, co-author trailers), computes assertion density from source, joins mutation scores from PIT/Stryker/mutmut reports, and lists AI tests below the governance thresholds as **unverified liabilities** — carrying cost with no evidence of value.

## 8. Reading the memo

1. **Bottom line** — cost, value, net, ROI for the window.
2. **Reallocation** — counts and annualised dollar impact per verdict. This is the slide.
3. **Where the money goes** — if triage dominates, flakiness is your problem; if maintenance dominates, brittleness is; if CI compute dominates, parallelisation is.
4. **Top liabilities / assets** — the ten tests to act on and the ten to protect.
5. **AI governance** — unverified AI tests.
6. **Assumptions** — every rate, so finance can challenge inputs rather than conclusions.

## 9. Known biases and how to correct them

* **Under-counting value** when fixes are not labelled as fixes → adopt conventional commits or set `sources.fix_commit_pattern` (e.g. `^INC-\d+` for incident-ticket prefixes).
* **Over-counting value** when a fix commit coincides with an unrelated genuine failure → the fix window (`thresholds.fix_window_days`) and one-fix-per-test rule limit this; use `ledger.json` to audit.
* **Shared files** spread maintenance cost evenly across the tests in them → split files by behaviour if you want precision.
* **No history** (shallow clone) → churn and fixes are empty; use `fetch-depth: 0`.

## 10. Optional AI narrative — what is sent and why

With `analyze --narrative` (off by default) the memo gains a short plain-language paragraph written by an LLM. The rules that keep this safe:

* **What is sent:** the `summary.json` figures (totals, ROI, verdict counts, annualised savings), the top write-off rows (test id, verdict, flake rate, cost, value, action — at most `narrative.max_writeoffs`), and the assumptions (rates and thresholds). Nothing else.
* **What is never sent:** test source code, failure messages or stack traces, repository content, file contents, tokens. The prompt is assembled from the report data only, and with the flag off no narrative code is imported and no request is made.
* **Where it goes:** either a local OpenAI-compatible endpoint you run (default `http://localhost:11434/v1`, e.g. Ollama — data never leaves your machine) or the hosted OpenAI API. Model and endpoint come from the `narrative:` config block; the API key comes only from the environment variable named by `narrative.api_key_env` (default `OPENAI_API_KEY`) and is never written to any file.
* **Traceability check:** the reply is rejected when it contains a dollar figure or percentage that is not in the input payload (ratios count in their ×100 percentage form, e.g. a flake rate of 0.35 allows "35%"). On rejection — or on any provider error — the memo silently falls back to the plain template with a warning on the console.
* **Labelling:** the paragraph is marked as AI-generated and names the model, in both `memo.md` and `memo.html`.

The narrative only restates figures a human can verify in the same memo; every number in it must trace back to an input, like every other number TestLedger prints.

## 11. References

* Cost of flaky tests in CI — industrial case study (TUM / ICST 2024): compiles cost factors from research and practice into a cost model.
* Google and Microsoft reports of 4–16 % flaky tests and the shift from investigation to reruns.
* SmartTest (GMU, 2020): tests fall into rerun / no-rerun / change / delete — and deletion rarely happens when done by hand.
* Mutation testing as the quality gate for AI-generated tests (various, 2025–26): high coverage with low mutation score is the signature of assertions that mirror the implementation.
