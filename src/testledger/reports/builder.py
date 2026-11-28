"""Report builder: turns a ``Ledger`` into executive artefacts.

* ``balance_sheet``   — assets vs. liabilities by test, by layer and by suite
* ``income_statement``— cost and value totals for the window, by cost category
* ``writeoffs``       — the action list: retire / demote / fix / quarantine
* ``ai_liabilities``  — AI-generated tests failing governance thresholds
* ``memo``            — the one-page Quality Investment Memo (Markdown + HTML)
* ``ledger.json``     — the full book for dashboards and diffing between quarters
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jinja2 import Environment, PackageLoader, select_autoescape

from testledger.config import Config
from testledger.models import EntryKind, Layer, Ledger, TestAccount, Verdict

VERDICT_ACTION = {
    Verdict.RETIRE: "Delete. No regression caught in window; pure carrying cost.",
    Verdict.DEMOTE: "Rewrite at service/unit layer; same coverage at a fraction of the cost.",
    Verdict.FIX: "Stabilise or speed up; the value is real but the signal is noisy.",
    Verdict.QUARANTINE: "Remove from the merge gate immediately; failing it blocks nothing but trust.",
    Verdict.ASSET: "Keep. Protect from churn; use as a template.",
    Verdict.HOLD: "No action; insufficient evidence or break-even.",
}


@dataclass
class ReportBundle:
    summary: dict
    balance_sheet: dict
    income_statement: dict
    writeoffs: list[dict]
    ai_liabilities: list[dict]
    memo_markdown: str
    memo_html: str
    ledger_json: dict
    assumptions: dict = field(default_factory=dict)


def _acc_row(a: TestAccount) -> dict:
    return {
        "test_id": a.test_id,
        "suite": a.suite,
        "layer": a.layer.value,
        "executions": a.executions,
        "failures": a.failures,
        "flake_rate": a.flake_rate,
        "avg_duration_s": a.avg_duration_s,
        "regressions_caught": a.regressions_caught,
        "cost_usd": a.total_cost,
        "value_usd": a.total_value,
        "net_usd": a.net,
        "roi": a.roi,
        "verdict": a.verdict.value,
        "action": VERDICT_ACTION[a.verdict],
        "ai_generated": a.ai_generated,
        "mutation_score": a.mutation_score,
        "assertion_density": a.assertion_density,
    }


def build_reports(ledger: Ledger, cfg: Config) -> ReportBundle:
    accounts = list(ledger.accounts.values())
    assets = sorted((a for a in accounts if a.net > 0), key=lambda a: -a.net)
    liabilities = sorted((a for a in accounts if a.net < 0), key=lambda a: a.net)

    by_layer: dict[str, dict] = defaultdict(
        lambda: {"tests": 0, "cost_usd": 0.0, "value_usd": 0.0, "regressions": 0}
    )
    by_suite: dict[str, dict] = defaultdict(
        lambda: {"tests": 0, "cost_usd": 0.0, "value_usd": 0.0, "regressions": 0}
    )
    by_kind: dict[str, float] = defaultdict(float)
    verdict_counts: dict[str, int] = defaultdict(int)
    for a in accounts:
        for bucket in (by_layer[a.layer.value], by_suite[a.suite or "(no suite)"]):
            bucket["tests"] += 1
            bucket["cost_usd"] = round(bucket["cost_usd"] + a.total_cost, 2)
            bucket["value_usd"] = round(bucket["value_usd"] + a.total_value, 2)
            bucket["regressions"] += a.regressions_caught
        for e in a.entries:
            by_kind[e.kind.value] = round(by_kind[e.kind.value] + e.amount_usd, 2)
        verdict_counts[a.verdict.value] += 1

    writeoffs = [
        _acc_row(a)
        for v in (Verdict.QUARANTINE, Verdict.RETIRE, Verdict.DEMOTE, Verdict.FIX)
        for a in ledger.by_verdict(v)
    ]
    writeoff_savings = round(
        sum(a.total_cost for a in accounts if a.verdict in (Verdict.RETIRE, Verdict.QUARANTINE)), 2
    )
    demote_savings = round(
        sum(
            a.total_cost * (1 - 1 / cfg.thresholds.demote_layer_cost_ratio)
            for a in accounts
            if a.verdict == Verdict.DEMOTE
        ),
        2,
    )

    th = cfg.thresholds
    ai_liabilities = [
        _acc_row(a)
        for a in accounts
        if a.ai_generated
        and (
            (a.mutation_score is not None and a.mutation_score < th.ai_min_mutation_score)
            or (a.assertion_density is not None and a.assertion_density < th.ai_min_assertion_density)
        )
    ]

    summary = {
        "org": cfg.org_name,
        "generated_at": ledger.generated_at.isoformat(timespec="seconds"),
        "window_days": cfg.window_days,
        "runs_analysed": ledger.runs_analysed,
        "tests": len(accounts),
        "total_cost_usd": ledger.total_cost,
        "total_value_usd": ledger.total_value,
        "net_usd": ledger.net,
        "portfolio_roi": round(ledger.net / ledger.total_cost, 3) if ledger.total_cost else None,
        "regressions_caught": sum(a.regressions_caught for a in accounts),
        "asset_tests": len(assets),
        "liability_tests": len(liabilities),
        "verdicts": dict(verdict_counts),
        "annualised_writeoff_savings_usd": round(writeoff_savings * 365 / cfg.window_days, 2),
        "annualised_demote_savings_usd": round(demote_savings * 365 / cfg.window_days, 2),
        "ai_generated_tests": sum(1 for a in accounts if a.ai_generated),
        "ai_unverified_liabilities": len(ai_liabilities),
    }
    balance_sheet: dict[str, Any] = {
        "assets": [_acc_row(a) for a in assets[:50]],
        "liabilities": [_acc_row(a) for a in liabilities[:50]],
        "by_layer": dict(by_layer),
        "by_suite": dict(sorted(by_suite.items(), key=lambda kv: kv[1]["cost_usd"], reverse=True)[:25]),
    }
    income_statement = {
        "costs": {
            k: v
            for k, v in by_kind.items()
            if k
            in {
                e.value
                for e in EntryKind
                if e
                in (
                    EntryKind.CI_COMPUTE,
                    EntryKind.RERUN_COMPUTE,
                    EntryKind.TRIAGE_TIME,
                    EntryKind.MAINTENANCE,
                )
            }
        },
        "value": {
            k: v
            for k, v in by_kind.items()
            if k in (EntryKind.REGRESSION_CAUGHT.value, EntryKind.RISK_COVERAGE.value)
        },
        "net_usd": ledger.net,
    }
    assumptions = cfg.rates.model_dump() | {"thresholds": cfg.thresholds.model_dump()}

    env = Environment(
        loader=PackageLoader("testledger", "templates"), autoescape=select_autoescape(["html", "html.j2"])
    )
    ctx = {
        "s": summary,
        "bs": balance_sheet,
        "inc": income_statement,
        "writeoffs": writeoffs[:40],
        "ai": ai_liabilities[:20],
        "notes": ledger.notes,
        "assumptions": assumptions,
        "Layer": Layer,
        "top_assets": balance_sheet["assets"][:10],
        "top_liabilities": balance_sheet["liabilities"][:10],
    }
    memo_md = env.get_template("memo.md.j2").render(**ctx)
    memo_html = env.get_template("memo.html.j2").render(**ctx, memo_md=memo_md)

    ledger_json = {
        "schema_version": 1,
        "summary": summary,
        "assumptions": assumptions,
        "accounts": [
            _acc_row(a) | {"entries": [e.model_dump(mode="json") for e in a.entries]} for a in accounts
        ],
    }
    return ReportBundle(
        summary,
        balance_sheet,
        income_statement,
        writeoffs,
        ai_liabilities,
        memo_md,
        memo_html,
        ledger_json,
        assumptions,
    )


def write_reports(bundle: ReportBundle, out_dir: str | Path) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    files = {
        "memo.md": bundle.memo_markdown,
        "memo.html": bundle.memo_html,
        "balance_sheet.json": json.dumps(bundle.balance_sheet, indent=2),
        "income_statement.json": json.dumps(bundle.income_statement, indent=2),
        "writeoffs.json": json.dumps(bundle.writeoffs, indent=2),
        "ai_liabilities.json": json.dumps(bundle.ai_liabilities, indent=2),
        "ledger.json": json.dumps(bundle.ledger_json, indent=2),
        "summary.json": json.dumps(bundle.summary, indent=2),
    }
    written: dict[str, Path] = {}
    for name, content in files.items():
        p = out / name
        p.write_text(content, encoding="utf-8")
        written[name] = p
    return written
