"""Build the narrative prompt, call the provider, validate the reply."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from testledger.config import Narrative

# A dollar figure ($1,234.56, $2k) or a percentage (12.5%).
_FIGURE = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*([kKmMbB])?|(\d+(?:\.\d+)?)\s*%")

_SYSTEM = (
    "You write one short paragraph (3-5 sentences) for the top of a Quality Investment Memo. "
    "Use only the JSON figures given to you: restate the bottom line, name the biggest cost "
    "driver and the recommended reallocation. Never invent a number, a percentage or a cause. "
    "Plain executive language, no bullet points."
)


class NarrativeError(RuntimeError):
    """The provider could not produce a usable narrative."""


def _scaled(value: float, suffix: str | None) -> float:
    factor = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get((suffix or "").lower(), 1)
    return value * factor


def _figures(text: str) -> list[float]:
    """Numeric values of every dollar figure and percentage in ``text``."""
    out = []
    for m in _FIGURE.finditer(text):
        if m.group(1) is not None:
            out.append(_scaled(float(m.group(1).replace(",", "")), m.group(2)))
        else:
            out.append(float(m.group(3)))
    return out


def _payload_numbers(payload: Any) -> set[float]:
    """Every number in the input payload, plus the ×100 percentage form of ratios.

    A flake rate of 0.35 is the same fact as "35%", so both are traceable.
    """
    numbers: set[float] = set()

    def walk(value: Any) -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            numbers.add(round(value, 4))
            if 0 < value < 1:
                numbers.add(round(value * 100, 4))
        elif isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    walk(payload)
    return numbers


def untraced_figures(text: str, payload: Any) -> list[float]:
    """Dollar/percentage figures in ``text`` that do not appear in the payload."""
    allowed = _payload_numbers(payload)
    return [v for v in _figures(text) if round(v, 4) not in allowed]


def build_payload(summary: dict, writeoffs: list[dict], assumptions: dict, max_writeoffs: int) -> dict:
    """The only data a provider ever sees: summary, top write-offs, assumptions."""
    keep_summary = {
        k: summary.get(k)
        for k in (
            "org",
            "window_days",
            "tests",
            "runs_analysed",
            "total_cost_usd",
            "total_value_usd",
            "net_usd",
            "portfolio_roi",
            "regressions_caught",
            "verdicts",
            "annualised_writeoff_savings_usd",
            "annualised_demote_savings_usd",
        )
    }
    keep_writeoffs = [
        {k: row.get(k) for k in ("test_id", "verdict", "flake_rate", "cost_usd", "value_usd", "action")}
        for row in writeoffs[:max_writeoffs]
    ]
    return {"summary": keep_summary, "top_writeoffs": keep_writeoffs, "assumptions": assumptions}


def generate_narrative(
    settings: Narrative,
    summary: dict,
    writeoffs: list[dict],
    assumptions: dict,
    complete: Callable[[str, str, Narrative], str] | None = None,
) -> str | None:
    """Return a validated narrative paragraph, or None to fall back to the plain memo.

    ``complete`` is the provider call ``(system, prompt, settings) -> str``; injectable so
    tests never touch the network. The default is the configured provider client.
    """
    from testledger.narrative.providers import chat_completion

    complete = complete or chat_completion
    payload = build_payload(summary, writeoffs, assumptions, settings.max_writeoffs)
    prompt = json.dumps(payload, indent=2)
    text = (complete(_SYSTEM, prompt, settings) or "").strip()
    if not text:
        return None
    bad = untraced_figures(text, payload)
    if bad:
        raise NarrativeError(
            "narrative rejected: figure(s) not present in the input data: " + ", ".join(f"{v:g}" for v in bad)
        )
    return text
