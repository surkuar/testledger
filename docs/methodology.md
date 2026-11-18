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
| **Maintenance** | Brittle tests are edited every time the product moves | lines *modified* in existing test files in the window (git; creating a new test is authoring, not maintenance), × minutes per line × hourly rate |

Industrial studies put flake rates at 4–16 % of tests and show that teams shift effort from investigation to blind reruns precisely because triage is expensive; TestLedger makes that cost visible per test.

Defaults (override in `testledger.yml`): engineer $75/h fully loaded, CI $0.008/min, 18 minutes per false alarm, 1.5 minutes per changed line.

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

* **Under-counting value** when fixes are not labelled as fixes → adopt conventional commits or widen `FIX_PATTERN`.
* **Over-counting value** when a fix commit coincides with an unrelated genuine failure → the fix window (`thresholds.fix_window_days`) and one-fix-per-test rule limit this; use `ledger.json` to audit.
* **Shared files** spread maintenance cost evenly across the tests in them → split files by behaviour if you want precision.
* **No history** (shallow clone) → churn and fixes are empty; use `fetch-depth: 0`.

## 10. References

* Cost of flaky tests in CI — industrial case study (TUM / ICST 2024): compiles cost factors from research and practice into a cost model.
* Google and Microsoft reports of 4–16 % flaky tests and the shift from investigation to reruns.
* SmartTest (GMU, 2020): tests fall into rerun / no-rerun / change / delete — and deletion rarely happens when done by hand.
* Mutation testing as the quality gate for AI-generated tests (various, 2025–26): high coverage with low mutation score is the signature of assertions that mirror the implementation.
