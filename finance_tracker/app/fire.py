"""FIRE (Financial Independence, Retire Early) planning engine.

Pure functions. No I/O. No datetime side effects. All monetary inputs and
outputs are in TODAY'S real (inflation-adjusted) dollars.

Core references:
  - SWR / 4%-rule: Bengen (1994), Trinity Study (1998, 2009)
  - Coast FIRE: growing a lump sum until it compounds to fireNumber unaided
  - Lean/Fat FIRE: spending-multiplier variants bracketing standard spending

Math lives entirely in real space: realReturn drives all trajectory
calculations; fireNumber is the real-dollar portfolio target at the SWR.
Nominal figures (for display) are intentionally excluded from this engine —
the UI layer may multiply any value by (1 + inflation)^years if desired.
"""

from __future__ import annotations

import math

import finance_math as _fm
from investing import _years_to_net_worth


__all__ = ["compute_fire"]


def compute_fire(
    current_net_worth: float,
    annual_spend: float,
    current_age: float,
    target_fi_age: float,
    annual_savings: float,
    swr: float = 0.035,
    nominal_return: float = 0.07,
    inflation: float = 0.03,
    lean_mult: float = 0.7,
    fat_mult: float = 1.5,
    band: float = 0.02,
    income: float | None = None,
    current_year: int = 2026,
) -> dict:
    """Compute a full FIRE analysis.  Returns a camelCase dict.

    All monetary inputs are today's real dollars.  The engine works in real
    (inflation-adjusted) space throughout: ``realReturn`` drives every
    trajectory; ``fireNumber = annual_spend / swr`` is the real-dollar target.

    Edge cases handled cleanly:
    - ``annual_spend == 0`` or ``swr == 0``: treated as already-FI
      (``fireNumber = 0``, all ``pctToFi`` values ≥ 1, ``yearsToFiBand``
      entries = 0.0, ``isCoastFi = True``).
    - ``annual_savings <= 0`` and compounding can't bridge the gap:
      ``yearsToFiBand`` entries are ``None``; ``lowCaseReachesFi`` is
      ``False``.
    - Negative or coincident ages: coast scan window collapses to 0 or 1
      iteration without crashing.
    - ``realReturn - band <= 0``: ``_years_to_net_worth`` handles the
      zero/negative-rate branch (pure linear if savings > 0, else None).
    """
    # ── Real return ─────────────────────────────────────────────────────────
    real_return: float = (1.0 + nominal_return) / (1.0 + inflation) - 1.0

    # ── FIRE number ─────────────────────────────────────────────────────────
    already_fi_trivially: bool = annual_spend <= 0.0 or swr <= 0.0
    fire_number: float = 0.0 if already_fi_trivially else (annual_spend / swr)

    # ── pctToFi (guarded against div-by-zero) ───────────────────────────────
    pct_to_fi: float = (current_net_worth / fire_number) if fire_number > 0.0 else 1.0

    # ── Internal helper: years-to-FI band for any fire-number target ─────────
    def _band_years(target: float) -> dict[str, float | None]:
        if already_fi_trivially or current_net_worth >= target:
            return {"fast": 0.0, "mid": 0.0, "slow": 0.0}
        return {
            "fast": _years_to_net_worth(target, current_net_worth, annual_savings, real_return + band),
            "mid":  _years_to_net_worth(target, current_net_worth, annual_savings, real_return),
            "slow": _years_to_net_worth(target, current_net_worth, annual_savings, real_return - band),
        }

    years_band = _band_years(fire_number)

    # ── lowCaseReachesFi ─────────────────────────────────────────────────────
    if already_fi_trivially or current_net_worth >= fire_number:
        low_case_reaches_fi: bool = True
    else:
        slow_50 = _years_to_net_worth(
            fire_number,
            current_net_worth,
            annual_savings,
            real_return - band,
            cap=50.0,
        )
        low_case_reaches_fi = slow_50 is not None

    # ── Lean / Fat fire numbers ───────────────────────────────────────────────
    lean_fire_number: float = fire_number * lean_mult
    fat_fire_number: float = fire_number * fat_mult
    lean_band = _band_years(lean_fire_number)
    fat_band  = _band_years(fat_fire_number)

    # ── Coast FIRE ───────────────────────────────────────────────────────────
    years_to_fi_age: float = max(0.0, target_fi_age - current_age)

    if fire_number <= 0.0:
        # Trivially FI — coast is also immediately met
        coast_number: float = 0.0
        pct_to_coast: float = 1.0
        is_coast_fi: bool = True
        coast_ready_age: float | None = current_age
    else:
        # Coast number: the lump needed *today* so that, compounding at
        # realReturn alone, it reaches fireNumber by target_fi_age.
        if years_to_fi_age <= 0.0:
            coast_number = fire_number
        else:
            coast_number = fire_number / ((1.0 + real_return) ** years_to_fi_age)

        pct_to_coast = (current_net_worth / coast_number) if coast_number > 0.0 else 1.0
        is_coast_fi = current_net_worth >= coast_number

        # coastReadyAge: first integer age at which projected NW ≥ rising coast bar.
        # coast bar at future age a = fireNumber / (1+rr)^(target_fi_age - a).
        # NW trajectory at offset t years = FV_lump(nw, rr, t) + FV_series(savings, rr, t).
        scan_limit: int = math.ceil(years_to_fi_age)
        coast_ready_age = None
        for t in range(scan_limit + 1):
            a = current_age + t
            remaining = target_fi_age - a
            coast_bar = (
                fire_number / ((1.0 + real_return) ** remaining)
                if remaining > 0.0
                else fire_number
            )
            trajectory = (
                _fm.future_value_lump(current_net_worth, real_return, t)
                + _fm.future_value_series(annual_savings, real_return, t)
            )
            if trajectory >= coast_bar:
                coast_ready_age = float(a)
                break

    # ── Savings rate ──────────────────────────────────────────────────────────
    savings_rate: float | None = (
        (annual_savings / income) if (income is not None and income > 0.0) else None
    )

    # ── fiWindowYears — coarse calendar range for display ─────────────────────
    fast_y = years_band["fast"]
    slow_y = years_band["slow"]
    fi_window: dict[str, int | None] = {
        "fromYear": (current_year + round(fast_y)) if fast_y is not None else None,
        "toYear":   (current_year + round(slow_y)) if slow_y is not None else None,
    }

    # ── Helper: round a band dict at the output boundary ─────────────────────
    def _round_band(b: dict[str, float | None]) -> dict[str, float | None]:
        return {k: (round(v, 2) if v is not None else None) for k, v in b.items()}

    # ── Variant builder ───────────────────────────────────────────────────────
    def _variant(fire_num: float, band_yrs: dict[str, float | None]) -> dict:
        pct = (current_net_worth / fire_num) if fire_num > 0.0 else 1.0
        return {
            "fireNumber": round(fire_num, 2),
            "pctToFi": round(pct, 6),
            "yearsToFiBand": _round_band(band_yrs),
        }

    # ── Notes ─────────────────────────────────────────────────────────────────
    notes: list[str] = [
        "All figures are in today's real (inflation-adjusted) dollars; your actual portfolio "
        "balance at retirement will be higher by cumulative inflation.",
        "SWR is a historical estimate (Bengen/Trinity); your actual safe rate depends on "
        "asset allocation, expense ratios, and sequence-of-returns luck at retirement.",
        "Pre-65 healthcare gap: no Medicare until 65 — budget for ACA marketplace premiums "
        "or COBRA continuation during early retirement years.",
        "Withdrawals from tax-deferred accounts are ordinary income; after-tax spending "
        "power will be lower unless your model accounts for specific retirement tax brackets.",
        "Sequence-of-returns risk: a severe early-retirement downturn can permanently impair "
        "a portfolio even when the long-run average return is healthy.",
        "Not financial, tax, or legal advice. Consult a CFP/CPA before making decisions.",
    ]

    # ── Assemble and return ───────────────────────────────────────────────────
    return {
        "fireNumber":        round(fire_number, 2),
        "pctToFi":           round(pct_to_fi, 6),
        "yearsToFiBand":     _round_band(years_band),
        "lowCaseReachesFi":  low_case_reaches_fi,
        "coastNumber":       round(coast_number, 2),
        "pctToCoast":        round(pct_to_coast, 6),
        "isCoastFi":         is_coast_fi,
        "coastReadyAge":     coast_ready_age,
        "savingsRate":       (round(savings_rate, 4) if savings_rate is not None else None),
        "leanFireNumber":    round(lean_fire_number, 2),
        "fatFireNumber":     round(fat_fire_number, 2),
        "variants": {
            "standard": _variant(fire_number,      years_band),
            "lean":     _variant(lean_fire_number, lean_band),
            "fat":      _variant(fat_fire_number,  fat_band),
        },
        "fiWindowYears": fi_window,
        "inputs": {
            "currentNetWorth": current_net_worth,
            "annualSpend":     annual_spend,
            "currentAge":      current_age,
            "targetFiAge":     target_fi_age,
            "annualSavings":   annual_savings,
            "swr":             swr,
            "nominalReturn":   nominal_return,
            "inflation":       inflation,
            "leanMult":        lean_mult,
            "fatMult":         fat_mult,
            "band":            band,
            "income":          income,
            "currentYear":     current_year,
        },
        "notes": notes,
    }
