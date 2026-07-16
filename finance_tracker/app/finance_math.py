"""Shared financial primitives for the budgeting / investing / auto modules.

Pure functions. No I/O. All rates are **annual decimals** (0.065 == 6.5%), all
amounts are dollars, all terms are in **months** unless named otherwise.

These are the low-level building blocks (amortization, present/future value,
lease money-factor conversion) that the domain modules import so the math stays
consistent in one place.
"""

from __future__ import annotations

from dataclasses import dataclass


# ---------- Periodic rate ----------

def monthly_rate(annual_rate: float) -> float:
    """Annual nominal rate -> monthly rate (simple /12 convention used by lenders)."""
    return annual_rate / 12.0


# ---------- Mortgage / loan amortization ----------

def payment_factor(annual_rate: float, term_months: int) -> float:
    """Monthly payment per $1 of loan principal (the amortization factor `k`).

    k = i(1+i)^n / ((1+i)^n - 1), with the 0% edge case = 1/n.
    """
    if term_months <= 0:
        raise ValueError("term_months must be positive")
    i = monthly_rate(annual_rate)
    if i == 0:
        return 1.0 / term_months
    g = (1 + i) ** term_months
    return i * g / (g - 1)


def mortgage_payment(principal: float, annual_rate: float, term_months: int) -> float:
    """Level monthly payment to amortize `principal` over `term_months`."""
    if principal <= 0:
        return 0.0
    return principal * payment_factor(annual_rate, term_months)


def loan_principal_from_payment(payment: float, annual_rate: float, term_months: int) -> float:
    """Inverse of mortgage_payment: max loan a given monthly `payment` supports."""
    if payment <= 0:
        return 0.0
    k = payment_factor(annual_rate, term_months)
    return payment / k


def remaining_balance(principal: float, annual_rate: float, term_months: int,
                      months_elapsed: int) -> float:
    """Outstanding loan balance after `months_elapsed` level payments."""
    if months_elapsed <= 0:
        return float(principal)
    if months_elapsed >= term_months:
        return 0.0
    i = monthly_rate(annual_rate)
    pmt = mortgage_payment(principal, annual_rate, term_months)
    if i == 0:
        return max(0.0, principal - pmt * months_elapsed)
    g = (1 + i) ** months_elapsed
    return principal * g - pmt * (g - 1) / i


@dataclass(frozen=True)
class AmortRow:
    month: int
    payment: float
    interest: float
    principal: float
    balance: float


def amortization_schedule(principal: float, annual_rate: float,
                          term_months: int) -> list[AmortRow]:
    """Full month-by-month amortization schedule."""
    rows: list[AmortRow] = []
    if principal <= 0 or term_months <= 0:
        return rows
    i = monthly_rate(annual_rate)
    pmt = mortgage_payment(principal, annual_rate, term_months)
    bal = float(principal)
    for m in range(1, term_months + 1):
        interest = bal * i
        prin = pmt - interest
        bal = max(0.0, bal - prin)
        rows.append(AmortRow(m, pmt, interest, prin, bal))
    return rows


def total_interest(principal: float, annual_rate: float, term_months: int) -> float:
    """Total interest paid over the life of a fully-amortized loan."""
    if principal <= 0:
        return 0.0
    return mortgage_payment(principal, annual_rate, term_months) * term_months - principal


# ---------- Time value of money ----------

def future_value_lump(present_value: float, annual_rate: float, years: float) -> float:
    """FV of a single sum compounded annually."""
    return present_value * (1 + annual_rate) ** years


def future_value_series(payment: float, annual_rate: float, years: int,
                        *, due: bool = False) -> float:
    """FV of a series of equal **annual** payments.

    due=False -> ordinary annuity (end of period); due=True -> annuity-due.
    """
    if years <= 0:
        return 0.0
    r = annual_rate
    if r == 0:
        fv = payment * years
    else:
        fv = payment * (((1 + r) ** years - 1) / r)
        if due:
            fv *= (1 + r)
    return fv


# ---------- Auto lease ----------

# A lease's money factor is its interest rate in disguise: APR = MF * 2400.
MONEY_FACTOR_TO_APR = 2400.0


def money_factor_to_apr(money_factor: float) -> float:
    """Lease money factor -> equivalent APR (decimal). MF 0.00125 -> 0.03 (3%)."""
    return money_factor * MONEY_FACTOR_TO_APR / 100.0


def apr_to_money_factor(apr: float) -> float:
    """Equivalent APR (decimal) -> lease money factor. 0.03 -> 0.00125."""
    return apr * 100.0 / MONEY_FACTOR_TO_APR


__all__ = [
    "monthly_rate",
    "payment_factor",
    "mortgage_payment",
    "loan_principal_from_payment",
    "remaining_balance",
    "AmortRow",
    "amortization_schedule",
    "total_interest",
    "future_value_lump",
    "future_value_series",
    "money_factor_to_apr",
    "apr_to_money_factor",
    "MONEY_FACTOR_TO_APR",
]
