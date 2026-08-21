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

import datetime as _dt
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
    current_year: int | None = None,
    coast_target_age: float | None = None,
    variant_specs: list[dict] | None = None,
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
    # ── Anchor year ──────────────────────────────────────────────────────
    # `fiWindowYears` is the ONLY output anchored to a wall-clock year, and it was a
    # literal 2026 here, in server.py's FireModel, and in the client request body — three
    # copies that would all have gone silently stale on 1 Jan 2027 while still rendering a
    # confident date range. None means "ask the clock"; callers that need determinism
    # (every test in tests/test_fire.py that asserts a year) pass the year explicitly.
    if current_year is None:
        current_year = _dt.date.today().year

    # ── Coast horizon, separate from the retirement date ──────────────────
    # One field used to answer two different questions. CoastFIRE's canonical framing is *stop
    # saving now, retire at a traditional age*; "when can I fully retire?" is a different
    # question, and a reader asking each got an answer computed off the same number.
    #
    # It matters because the coast horizon is a bigger lever than the return assumption: for a
    # $60k spender at 3.5%, coasting to 45 needs $1,171,164 and coasting to 65 needs $546,621 --
    # a 2.1x spread across ages that are all defensible, with no principled way to pick one for
    # somebody else. Defaults to target_fi_age, so every existing caller is unaffected.
    if coast_target_age is None:
        coast_target_age = target_fi_age

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

    # ── Helper: round a band dict at the output boundary ─────────────────────
    def _round_band(b: dict[str, float | None]) -> dict[str, float | None]:
        return {k: (round(v, 2) if v is not None else None) for k, v in b.items()}

    # ── Variants ──────────────────────────────────────────────────────────────
    # Every named FIRE variant is a parameterisation of three axes: what spend counts, what rate
    # is safe, and part-time income offsetting the draw. Building them as data rather than as
    # three hardcoded multiplications is what lets chubby/barista/custom exist at all.
    #
    # The multiplier form was not merely incomplete, it was MIS-LABELLED. The community defines
    # lean/chubby/fat by ABSOLUTE spending (leanFIRE under ~$40k/yr, chubby ~$100-200k, fat
    # $100k floor at the low convention and $200k+ commonly); `fat_mult = 1.5` on a $40,966
    # spend yields "Fat FIRE" at $61,449/yr, which is below every published fat floor and
    # squarely inside ordinary FIRE. Both modes are therefore supported and the CALLER chooses:
    # a threshold is a value judgement no engine can make for someone.
    #
    # `variant_specs=None` keeps the legacy lean/standard/fat trio, so every existing caller --
    # including anything hitting /api/fire that predates this -- is untouched.
    def _resolve_variant(spec: dict) -> dict:
        key = str(spec.get("key") or "custom")
        # Absolute spend wins over a multiplier when both are given: it is the more specific
        # statement, and silently averaging them would be inventing a number nobody asked for.
        if spec.get("spend") is not None:
            gross_spend = float(spec["spend"])
        else:
            gross_spend = annual_spend * float(spec.get("mult", 1.0))

        # BaristaFIRE: part-time work covers part of the spend, so the PORTFOLIO only has to
        # cover the remainder. Clamped at zero -- earning more than you spend does not make the
        # target negative, it makes it nil.
        part_time = max(0.0, float(spec.get("partTimeIncome") or 0.0))
        net_spend = max(0.0, gross_spend - part_time)

        # A variant may carry its own withdrawal rate. Horizon is what should move the rate, and
        # a fat plan is usually a longer plan -- "fat FIRE, but at 3% because I am retiring at
        # 45" was previously unsayable, since lean/fat reused the standard rate.
        v_swr = spec.get("swr")
        v_swr = float(v_swr) if v_swr is not None else swr

        number = 0.0 if (net_spend <= 0.0 or v_swr <= 0.0) else (net_spend / v_swr)
        pct = (current_net_worth / number) if number > 0.0 else 1.0
        return {
            "key": key,
            "label": str(spec.get("label") or key.title()),
            "fireNumber": round(number, 2),
            "pctToFi": round(pct, 6),
            "yearsToFiBand": _round_band(_band_years(number)),
            # Echoed so the UI never has to re-derive what it asked for, and so the reason one
            # variant differs from another is legible in the response itself.
            "annualSpend": round(gross_spend, 2),
            "partTimeIncome": round(part_time, 2),
            "netAnnualSpend": round(net_spend, 2),
            "swr": v_swr,
        }

    if variant_specs is None:
        variant_specs = [
            {"key": "standard", "label": "Full FI", "mult": 1.0},
            {"key": "lean",     "label": "Lean FI", "mult": lean_mult},
            {"key": "fat",      "label": "Fat FI",  "mult": fat_mult},
        ]

    variants: dict[str, dict] = {}
    for spec in variant_specs:
        v = _resolve_variant(spec)
        variants[v["key"]] = v
    # "standard" is what pctToFi, the coast calculation and fiWindowYears are all measured
    # against, so it must exist even if the caller forgot to ask for it.
    if "standard" not in variants:
        variants["standard"] = _resolve_variant({"key": "standard", "label": "Full FI", "mult": 1.0})

    # Legacy top-level mirrors. The client reads r.leanFireNumber / r.fatFireNumber directly, and
    # those must follow the caller's OWN lean/fat definition when it gave one -- falling back to
    # the multipliers only when it did not, rather than always recomputing from lean_mult/fat_mult
    # and quietly disagreeing with the variant of the same name sitting beside it.
    lean_fire_number: float = (variants["lean"]["fireNumber"] if "lean" in variants
                               else round(fire_number * lean_mult, 2))
    fat_fire_number: float = (variants["fat"]["fireNumber"] if "fat" in variants
                              else round(fire_number * fat_mult, 2))

    # ── Coast FIRE ───────────────────────────────────────────────────────────
    # Measured to coast_target_age (which defaults to target_fi_age) -- see the note above.
    years_to_fi_age: float = max(0.0, coast_target_age - current_age)

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
            remaining = coast_target_age - a
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
        "variants": variants,
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
            "coastTargetAge":  coast_target_age,
        },
        "notes": notes,
    }
