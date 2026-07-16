"""Scenario planner engine (TODO-219, DEC-017).

Pure functions. No I/O, no DB, no datetime side effects. Float dollars in and
out (the scenario payload is opaque JSON end to end — cents conversion never
applies inside it).

Two jobs:
  - ``catchup_plan``: the TRANSIENT rest-of-year schedule — given per-account
    annual targets, what was already contributed while on the old salary, and
    the scenario's activation month, derive the per-month and per-paycheck
    pace that still hits every target by year end (plus simple debt
    extra-principal goals: target $ by a date -> per-month, no amortization).
    This is a time-axis sprint rendered separately in the UI; it is NEVER
    folded into the level budget-plan baseline (the "no time axis" lesson;
    DEC-017 #4).
  - ``budget_plan_delta``: a pure diff of two ``window.__budgetPlan``-shaped
    dicts (current vs scenario) for the compare view.

Month math is month-boundary only (DEC-017 #5): activation happens at the
start of a month; spans count whole months inclusive of both endpoints.
"""

from __future__ import annotations

import re

__all__ = ["catchup_plan", "budget_plan_delta"]

_YM_RE = re.compile(r"^(\d{4})-(\d{2})(?:-\d{2})?$")


def _ym(value: str) -> tuple[int, int]:
    """Parse 'YYYY-MM' (or 'YYYY-MM-DD', day ignored) -> (year, month).
    Raises ValueError on anything else — the endpoint layer validates formats
    before calling, so a raise here means a caller bug, not user input."""
    m = _YM_RE.match(str(value or ""))
    if not m or not (1 <= int(m.group(2)) <= 12):
        raise ValueError(f"expected 'YYYY-MM' or 'YYYY-MM-DD', got {value!r}")
    return int(m.group(1)), int(m.group(2))


def _months_span(start: str, end: str) -> int:
    """Whole months from the start month through the end month, inclusive of
    both. Clamped to >= 1 (a start after the end still leaves one month of
    runway rather than a zero/negative divisor)."""
    sy, sm = _ym(start)
    ey, em = _ym(end)
    return max(1, (ey - sy) * 12 + (em - sm) + 1)


def _money(x) -> float:
    return round(float(x or 0.0), 2)


def catchup_plan(
    accounts: list[dict],
    activation_month: str,
    pay_freq: int,
    year_end: str,
    *,
    extra_principal_goals: list[dict] | None = None,
    net_per_paycheck: float | None = None,
) -> dict:
    """Rest-of-year catch-up schedule from the activation month onward.

    ``accounts``: ``[{key, label, annualTarget, alreadyIn}]`` — payroll-deferred
    annual caps (401k Trad/Roth, HSA, Roth IRA, mega-backdoor...); ``alreadyIn``
    is the MANUAL year-to-date amount (DEC-017 decision #3).
    ``extra_principal_goals``: ``[{label, targetAmount, alreadyIn, targetDate}]``
    — simple debt sprints (decision #4, no amortization).
    ``net_per_paycheck``: the scenario's hypothetical take-home per paycheck
    (from /api/calculate); enables the feasibility flag when provided.

    Numeric edge cases never raise (fire.py ethos): over-contributed accounts
    floor at 0 remaining; empty lists produce zeroed totals; an activation
    month after ``year_end`` clamps to a 1-month sprint; ``pay_freq <= 0`` is
    treated as 1 paycheck.
    """
    pay_freq = int(pay_freq) if pay_freq and int(pay_freq) > 0 else 1
    months_left = _months_span(activation_month, year_end)
    periods_left = max(1, round(pay_freq * months_left / 12))

    acct_rows = []
    for a in accounts or []:
        target = _money(a.get("annualTarget"))
        already = _money(a.get("alreadyIn"))
        remaining = max(0.0, round(target - already, 2))
        acct_rows.append({
            "key": a.get("key"),
            "label": a.get("label") or a.get("key") or "",
            "annualTarget": target,
            "alreadyIn": already,
            "remaining": remaining,
            "perMonth": round(remaining / months_left, 2),
            "perPaycheck": round(remaining / periods_left, 2),
        })

    goal_rows = []
    for g in extra_principal_goals or []:
        target = _money(g.get("targetAmount"))
        already = _money(g.get("alreadyIn"))
        remaining = max(0.0, round(target - already, 2))
        months_to_target = _months_span(activation_month, g.get("targetDate") or year_end)
        goal_rows.append({
            "label": g.get("label") or "",
            "targetAmount": target,
            "alreadyIn": already,
            "remaining": remaining,
            "targetDate": g.get("targetDate") or year_end,
            "monthsToTarget": months_to_target,
            "perMonth": round(remaining / months_to_target, 2),
        })

    accounts_remaining = round(sum(r["remaining"] for r in acct_rows), 2)
    accounts_per_paycheck = round(sum(r["perPaycheck"] for r in acct_rows), 2)
    accounts_per_month = round(sum(r["perMonth"] for r in acct_rows), 2)
    goals_remaining = round(sum(r["remaining"] for r in goal_rows), 2)
    goals_per_month = round(sum(r["perMonth"] for r in goal_rows), 2)

    cant = None
    if net_per_paycheck is not None:
        cant = accounts_per_paycheck > float(net_per_paycheck)

    return {
        "activationMonth": activation_month,
        "monthsLeft": months_left,
        "periodsLeft": periods_left,
        "accounts": acct_rows,
        "extraPrincipalGoals": goal_rows,
        "totals": {
            "accountsRemaining": accounts_remaining,
            "accountsPerPaycheck": accounts_per_paycheck,
            "accountsPerMonth": accounts_per_month,
            "extraPrincipalRemaining": goals_remaining,
            "extraPrincipalPerMonth": goals_per_month,
            "remaining": round(accounts_remaining + goals_remaining, 2),
            "perMonth": round(accounts_per_month + goals_per_month, 2),
        },
        "cantCatchUpFromPayroll": cant,
        "inputs": {
            "activationMonth": activation_month,
            "payFreq": pay_freq,
            "yearEnd": year_end,
            "netPerPaycheck": None if net_per_paycheck is None else _money(net_per_paycheck),
            "accountCount": len(acct_rows),
            "goalCount": len(goal_rows),
        },
    }


def _delta(cur, scn, digits: int = 2) -> dict:
    cur = round(float(cur or 0.0), digits)
    scn = round(float(scn or 0.0), digits)
    return {"current": cur, "scenario": scn, "delta": round(scn - cur, digits)}


def budget_plan_delta(current: dict, scenario: dict) -> dict:
    """Pure diff of two ``window.__budgetPlan``-shaped dicts for the compare
    view: per-bucket monthly amounts (union of keys, mirroring plan_vs_actual),
    incomeBase, savingsTarget (a rate, 4dp), and partner splits (union of keys;
    either side may omit ``partnerSplits`` entirely). ``totals`` sums bucket
    outflow only — income is not an outflow."""
    current = current or {}
    scenario = scenario or {}
    cur_k = current.get("kindMonthly") or {}
    scn_k = scenario.get("kindMonthly") or {}
    buckets = {b: _delta(cur_k.get(b), scn_k.get(b)) for b in sorted(set(cur_k) | set(scn_k))}

    cur_p = current.get("partnerSplits") or {}
    scn_p = scenario.get("partnerSplits") or {}
    partner = {k: _delta(cur_p.get(k), scn_p.get(k)) for k in sorted(set(cur_p) | set(scn_p))}

    cur_out = round(sum(float(v or 0.0) for v in cur_k.values()), 2)
    scn_out = round(sum(float(v or 0.0) for v in scn_k.values()), 2)
    return {
        "buckets": buckets,
        "incomeBase": _delta(current.get("incomeBase"), scenario.get("incomeBase")),
        "savingsTarget": _delta(current.get("savingsTarget"), scenario.get("savingsTarget"), digits=4),
        "partner": partner,
        "totals": {
            "currentOutflow": cur_out,
            "scenarioOutflow": scn_out,
            "delta": round(scn_out - cur_out, 2),
        },
    }
