"""Target-savings goal math (TODO-226, DEC-019).

Pure functions. No I/O, no datetime side effects — `today` is always an
explicit argument so results are reproducible.

A goal is "save TARGET by TARGET_DATE". Progress (saved-so-far) comes from
whatever the caller resolves it to (a linked account's latest balance, or a
manual figure — that resolution lives in tracking_store, not here). This
module answers: how much per month / per paycheck is still needed, and is
the goal on pace (linear expectation from START_DATE to TARGET_DATE)?

Month math mirrors scenarios.py: whole-month span clamped to >= 1 while the
target is in the future; paychecks_left = max(1, round(pay_freq * months/12)).
A goal whose date has passed gets months_left = 0 and needed == remaining
(the honest "you'd have to save it all now" answer).
"""

from __future__ import annotations

from datetime import date

__all__ = ["goal_progress"]


def _parse(d: str | date) -> date:
    if isinstance(d, date):
        return d
    return date.fromisoformat(str(d))


def _months_between(a: date, b: date) -> int:
    """Whole months from a to b (b >= a), rounding partial months UP so a
    goal due mid-month still counts that month as available."""
    m = (b.year - a.year) * 12 + (b.month - a.month)
    if b.day > a.day:
        m += 1
    return m


def goal_progress(
    target: float,
    target_date: str | date,
    saved: float,
    today: str | date,
    *,
    pay_freq: float = 24.0,
    start_date: str | date | None = None,
) -> dict:
    """Compute the state of a save-X-by-Y goal. Returns a camelCase,
    JSON-ready dict. Raises ValueError on bad input."""
    if not isinstance(target, (int, float)) or target <= 0:
        raise ValueError(f"target must be > 0, got {target!r}")
    if not isinstance(saved, (int, float)) or saved < 0:
        raise ValueError(f"saved must be >= 0, got {saved!r}")
    if not isinstance(pay_freq, (int, float)) or pay_freq <= 0:
        raise ValueError(f"pay_freq must be > 0, got {pay_freq!r}")
    t_date = _parse(target_date)
    t_now = _parse(today)
    t_start = _parse(start_date) if start_date is not None else None
    if t_start is not None and t_start > t_date:
        raise ValueError("start_date must be on or before target_date")

    remaining = max(0.0, target - saved)
    done = saved >= target
    overdue = t_date < t_now and not done

    if done or overdue or t_date <= t_now:
        months_left = 0
        paychecks_left = 0
        needed_month = 0.0 if done else remaining
        needed_paycheck = 0.0 if done else remaining
    else:
        months_left = max(1, _months_between(t_now, t_date))
        paychecks_left = max(1, round(pay_freq * months_left / 12.0))
        needed_month = remaining / months_left
        needed_paycheck = remaining / paychecks_left

    # Linear pace from start (goal creation) to the target date: by `today`
    # you "should" have expected = target * elapsed/total. Without a start
    # date there is no pace opinion (expectedByNow/aheadOfPace = None).
    expected = None
    ahead = None
    on_pace = None
    if t_start is not None:
        total_days = (t_date - t_start).days
        if total_days <= 0:
            frac = 1.0
        else:
            frac = min(1.0, max(0.0, (t_now - t_start).days / total_days))
        expected = target * frac
        ahead = saved - expected
        on_pace = ahead >= -0.005

    return {
        "target": round(float(target), 2),
        "saved": round(float(saved), 2),
        "remaining": round(remaining, 2),
        "pct": round(min(1.0, saved / target), 4),
        "targetDate": t_date.isoformat(),
        "monthsLeft": months_left,
        "paychecksLeft": paychecks_left,
        "neededPerMonth": round(needed_month, 2),
        "neededPerPaycheck": round(needed_paycheck, 2),
        "done": done,
        "overdue": overdue,
        "expectedByNow": None if expected is None else round(expected, 2),
        "aheadOfPace": None if ahead is None else round(ahead, 2),
        "onPace": on_pace,
    }
