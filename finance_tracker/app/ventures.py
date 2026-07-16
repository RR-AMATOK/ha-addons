"""Venture ROI math (TODO-228, DEC-020).

Pure functions, no I/O, `today` always explicit. All money in DOLLARS
(the store aggregates cents; the server converts at the edge, like goals).

A venture: upfront INVESTED (typed items — course, equipment) earned back by
the venture's real net profit RECOVERED (revenue − running costs from linked
transactions). Paid back when recovered >= invested.

Everything here is PRE-TAX cash payback (DEC-020): venture profit is
self-employment income (SE + income tax, none withheld) and this engine is
deliberately not a Schedule-C model — the UI carries the caveat.

Pace is a trailing run rate, reported as "~N months at current pace", not a
calendar promise (a young venture's run rate is noise, not signal).
"""

from __future__ import annotations

from datetime import date

__all__ = ["venture_roi"]


def _parse(d: str | date) -> date:
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d))


def _months_spanned(a: date, b: date) -> int:
    """Whole months from a to b, partial months rounding UP, minimum 1."""
    if b <= a:
        return 1
    m = (b.year - a.year) * 12 + (b.month - a.month)
    if b.day > a.day:
        m += 1
    return max(1, m)


def venture_roi(
    invested: float,
    revenue: float,
    costs: float,
    started_on: str | date,
    today: str | date,
    by_month: dict | None = None,   # {"YYYY-MM": {"revenue": x, "cost": y}} dollars
) -> dict:
    """Compute payback state for a venture. JSON-ready camelCase dict."""
    if not isinstance(invested, (int, float)) or invested <= 0:
        raise ValueError(f"invested must be > 0, got {invested!r}")
    for name, v in (("revenue", revenue), ("costs", costs)):
        if not isinstance(v, (int, float)):
            raise ValueError(f"{name} must be a number, got {v!r}")
    start = _parse(started_on)
    now = _parse(today)
    by_month = by_month or {}

    recovered = revenue - costs            # may be negative pre-launch (rent before clients)
    paid_back = recovered >= invested
    remaining = max(0.0, invested - recovered)
    pct = max(0.0, min(1.0, recovered / invested))

    # Pace window: from the venture start OR the earliest linked activity,
    # whichever is earlier (deliberately tagged history is data, not noise).
    first_activity = min(by_month) if by_month else None
    anchor = start
    if first_activity is not None:
        fa = date.fromisoformat(first_activity + "-01")
        if fa < anchor:
            anchor = fa
    months_elapsed = _months_spanned(anchor, now)
    monthly_rate = recovered / months_elapsed

    projected_months = None
    if not paid_back and monthly_rate > 0:
        projected_months = remaining / monthly_rate

    # Actual breakeven month: where the cumulative net first crossed invested.
    paid_back_month = None
    if paid_back and by_month:
        cum = 0.0
        for m in sorted(by_month):
            cum += by_month[m].get("revenue", 0.0) - by_month[m].get("cost", 0.0)
            if cum >= invested:
                paid_back_month = m
                break

    profit = max(0.0, recovered - invested)
    return {
        "invested": round(invested, 2),
        "revenue": round(revenue, 2),
        "costs": round(costs, 2),
        "recovered": round(recovered, 2),
        "remaining": round(remaining, 2),
        "pct": round(pct, 4),
        "monthsElapsed": months_elapsed,
        "monthlyRate": round(monthly_rate, 2),
        "projectedMonthsToPayback": None if projected_months is None else round(projected_months, 1),
        "paidBack": paid_back,
        "paidBackMonth": paid_back_month,
        "profit": round(profit, 2),
        "roiPct": round(profit / invested, 4) if paid_back else None,
        "preRevenue": revenue <= 0,
    }
