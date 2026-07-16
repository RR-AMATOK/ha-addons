"""House-payment affordability engine (TODO-225, DEC-019).

Pure functions, no I/O. All figures are MONTHLY dollars unless suffixed _yr.

Two modes, both anchored to the user's real budget buckets:

  payment_last  — every bucket keeps its allocation; what's left of income
                  after protected needs, investing, flexible spending, and
                  (household) partner debt becomes the all-in affordable
                  house payment, converted to an implied price.
  payment_first — the user pins the payment they WANT; protected needs and
                  investing are held, and the flexible buckets (wants +
                  every custom kind that isn't 'need'/'investment')
                  compress pro-rata to make room. The pinned payment also
                  converts to an implied price.

The caller passes `current_housing` (rent/mortgage lines detected in the
budget) separately from `protected_needs` — buying replaces it, so it is
freed in both modes and never counted against income.

Price ↔ payment: an all-in payment H covers principal & interest on the
loan (price × (1-d) × k, k = monthly payment factor), property tax
(price × rate/12), fixed insurance and HOA, and PMI (0.5%/yr of the loan)
when the down payment is under 20%. Solving for price:

    price = (H - ins_mo - hoa_mo)
            / ((1-d)·k + tax_rate/12 + [d < 0.2] · (1-d)·pmi_rate/12)
"""

from __future__ import annotations

import finance_math as _fm

__all__ = ["afford", "PMI_ANNUAL_RATE"]

PMI_ANNUAL_RATE = 0.005          # of loan balance, while down payment < 20%
_PMI_THRESHOLD = 0.20


def _split_for_price(price: float, down: float, k: float, tax_rate: float,
                     insurance_yr: float, hoa_mo: float) -> dict:
    loan = price * (1.0 - down)
    pmi_mo = loan * PMI_ANNUAL_RATE / 12.0 if down < _PMI_THRESHOLD else 0.0
    return {
        "pi": round(loan * k, 2),
        "taxMo": round(price * tax_rate / 12.0, 2),
        "insMo": round(insurance_yr / 12.0, 2),
        "pmiMo": round(pmi_mo, 2),
        "hoaMo": round(hoa_mo, 2),
    }


def _price_from_payment(h: float, down: float, k: float, tax_rate: float,
                        insurance_yr: float, hoa_mo: float) -> float:
    """Largest price whose all-in monthly cost is h. 0 when h can't even
    cover the fixed insurance + HOA."""
    fixed = insurance_yr / 12.0 + hoa_mo
    if h <= fixed:
        return 0.0
    denom = (1.0 - down) * k + tax_rate / 12.0
    if down < _PMI_THRESHOLD:
        denom += (1.0 - down) * PMI_ANNUAL_RATE / 12.0
    return (h - fixed) / denom


def afford(
    mode: str,
    your_take_home: float,
    *,
    household: bool = False,
    partner_take_home: float = 0.0,
    partner_debt: float = 0.0,
    protected_needs: float = 0.0,       # needs EXCLUDING current housing
    investing: float = 0.0,
    current_housing: float = 0.0,       # freed when buying (informational)
    flex_buckets: list[dict] | None = None,   # [{key, label, now}]
    desired_payment: float = 0.0,       # payment_first only
    down_pct: float = 20.0,
    rate_pct: float = 6.5,
    term_years: float = 30.0,
    prop_tax_rate_pct: float = 1.65,
    insurance_yr: float = 2_400.0,
    hoa_mo: float = 0.0,
) -> dict:
    if mode not in ("payment_first", "payment_last"):
        raise ValueError(f"mode must be payment_first or payment_last, got {mode!r}")
    for name, v in (("your_take_home", your_take_home), ("partner_take_home", partner_take_home),
                    ("partner_debt", partner_debt), ("protected_needs", protected_needs),
                    ("investing", investing), ("current_housing", current_housing),
                    ("desired_payment", desired_payment), ("insurance_yr", insurance_yr),
                    ("hoa_mo", hoa_mo)):
        if not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"{name} must be a number >= 0, got {v!r}")
    if not 0 <= down_pct < 100:
        raise ValueError(f"down_pct must be in [0, 100), got {down_pct!r}")
    if rate_pct < 0 or term_years <= 0:
        raise ValueError("rate_pct must be >= 0 and term_years > 0")
    if prop_tax_rate_pct < 0:
        raise ValueError("prop_tax_rate_pct must be >= 0")
    if mode == "payment_first" and desired_payment <= 0:
        raise ValueError("payment_first requires desired_payment > 0")

    flex = [dict(b) for b in (flex_buckets or [])]
    for b in flex:
        if not isinstance(b.get("now"), (int, float)) or b["now"] < 0:
            raise ValueError(f"flex bucket {b.get('key')!r} needs now >= 0")
    flex_total = sum(b["now"] for b in flex)

    income = your_take_home + (partner_take_home if household else 0.0)
    debt = partner_debt if household else 0.0
    down = down_pct / 100.0
    tax_rate = prop_tax_rate_pct / 100.0
    k = _fm.payment_factor(rate_pct / 100.0, int(round(term_years * 12)))

    if mode == "payment_last":
        leftover = income - protected_needs - investing - flex_total - debt
        payment = max(0.0, leftover)
        price = _price_from_payment(payment, down, k, tax_rate, insurance_yr, hoa_mo)
        verdict = "nothing_left" if leftover <= 0 else "ok"
        flex_after = [{**b, "after": round(b["now"], 2), "delta": 0.0} for b in flex]
        flex_factor = 1.0
        surplus_or_short = round(leftover, 2)
    else:
        payment = desired_payment
        price = _price_from_payment(payment, down, k, tax_rate, insurance_yr, hoa_mo)
        flex_avail = income - protected_needs - investing - debt - payment
        if flex_avail >= flex_total:
            flex_factor = 1.0
            surplus_or_short = round(flex_avail - flex_total, 2)
            verdict = "comfortable"
        elif flex_avail >= 0:
            flex_factor = 0.0 if flex_total == 0 else flex_avail / flex_total
            surplus_or_short = round(flex_avail - flex_total, 2)   # negative = compression
            verdict = "tight"
        else:
            flex_factor = 0.0
            surplus_or_short = round(flex_avail, 2)                # short even at zero flex
            verdict = "over"
        flex_after = [{**b, "after": round(b["now"] * flex_factor, 2),
                       "delta": round(b["now"] * flex_factor - b["now"], 2)} for b in flex]

    loan = price * (1.0 - down)
    return {
        "mode": mode,
        "household": household,
        "income": round(income, 2),
        "payment": round(payment, 2),
        "impliedPrice": round(price, 2),
        "loan": round(loan, 2),
        "downAmt": round(price * down, 2),
        "split": _split_for_price(price, down, k, tax_rate, insurance_yr, hoa_mo),
        "flexAfter": flex_after,
        "flexFactor": round(flex_factor, 4),
        "flexTotalNow": round(flex_total, 2),
        "surplusOrShort": surplus_or_short,
        "currentHousingFreed": round(current_housing, 2),
        "verdict": verdict,
    }
