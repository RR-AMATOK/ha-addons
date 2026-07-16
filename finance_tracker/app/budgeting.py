"""Personal-finance budgeting framework calculator.

Pure functions. No I/O. All monetary inputs are **annual dollars**; cadence
conversion (monthly / bi-weekly / per-paycheck) is a UI concern, not ours.

Implements seven frameworks from docs/financial-strategies.md §1:
  50/30/20        (Warren & Tyagi, *All Your Worth*)
  60/20/20        variant — higher Needs allocation
  70/20/10        variant — lower savings target
  80/20           variant — maximum simplicity
  Fidelity 50/15/5  (mixed gross/take-home base)
  Reverse / Pay-Yourself-First
  Conscious Spending Plan  (Ramit Sethi, *I Will Teach You to Be Rich*)

Income base: the correct base for all major frameworks is **after-tax income**:

    after_tax_income = net_take_home
                     + pretax_401k
                     + pretax_hsa
                     + pretax_health_premiums
                     + other_nontax_payroll

Most consumer calculators naively use raw take-home and understate the base.
A ``base_mode="take_home"`` toggle is offered for quick estimates.

Key asymmetry (spec DEC-001):
  Pre-tax 401k / HSA counts *inside* the savings bucket for 50/30/20,
  60/20/20, 70/20/10, 80/20, Fidelity 50/15/5, Reverse, and CSP.
  It is *outside* YNAB / zero-based budgets — that money never reaches
  checking, so those frameworks track only what actually lands in the account.
"""

from dataclasses import dataclass
from typing import Literal


# ---------- Inputs ----------

@dataclass
class Inputs:
    """All inputs for the budgeting module. All monetary values are annual."""

    # ---- Take-home cash ----
    # What actually deposits into checking each year (after all payroll
    # deductions and taxes).
    net_take_home: float = 0.0

    # ---- Pre-tax payroll deductions ----
    # These reduce take-home but are real income; adding them back gives the
    # after-tax base that budgeting frameworks are designed around.
    pretax_401k: float = 0.0            # Traditional 401k employee deferral
    pretax_hsa: float = 0.0             # HSA employee contribution
    pretax_health_premiums: float = 0.0  # Health / dental / vision premiums
    other_nontax_payroll: float = 0.0   # FSA, commuter benefits, etc.

    # ---- Post-tax savings ----
    # Already leaving take-home — Roth IRA, Roth 401k, ESPP contributions,
    # taxable brokerage auto-transfers, etc.
    post_tax_savings: float = 0.0

    # ---- Framework selector ----
    # Controls which framework the UI highlights. All frameworks are computed
    # regardless of this value.
    framework: str = "fiftyThirtyTwenty"

    # ---- Reverse / Pay-Yourself-First rate ----
    # Target savings rate as a decimal (e.g. 0.25 for 25%).
    # Defaults to 0.20 when None.
    savings_rate: float | None = None

    # ---- Zero-based / envelope categories ----
    # Per-category budget map (annual dollars). Provided by the caller for
    # zero-based budgeting; stored here for pass-through. Zero-based budgets
    # only track money that lands in checking, so pre-tax deductions are
    # outside scope and not computed in this module.
    categories: dict[str, float] | None = None

    # ---- Income base mode ----
    # "after_tax" (default): reconstructed after-tax income — the correct base.
    # "take_home": raw net_take_home — fast but underestimates the true base.
    # CSP always uses after_tax_income regardless of this toggle.
    # Fidelity 50/15/5 always uses net_take_home for essentials/short-term.
    base_mode: Literal["after_tax", "take_home"] = "after_tax"

    # ---- Fidelity 50/15/5 gross inputs ----
    # The retirement bucket (15%) is defined on GROSS income including the
    # employer match — distinct from all other frameworks.  If omitted,
    # retirementTarget is returned as None with an explanatory note.
    gross_income: float | None = None
    employer_match: float | None = None


# ---------- Income base helpers ----------

def after_tax_income(i: Inputs) -> float:
    """Reconstruct the canonical after-tax income base (annual dollars).

    after_tax_income = net_take_home
                     + pretax_401k
                     + pretax_hsa
                     + pretax_health_premiums
                     + other_nontax_payroll

    This is the income base specified by Warren & Tyagi's 50/30/20,
    Fidelity 50/15/5, the Reverse budget, and Ramit Sethi's CSP — NOT raw
    take-home, which most consumer calculators (incorrectly) use and which
    systematically understates the budget base for pre-tax savers.
    """
    return (
        i.net_take_home
        + i.pretax_401k
        + i.pretax_hsa
        + i.pretax_health_premiums
        + i.other_nontax_payroll
    )


def _income_base(i: Inputs) -> float:
    """Select the income base for frameworks that respect base_mode."""
    if i.base_mode == "after_tax":
        return after_tax_income(i)
    return i.net_take_home


def actual_savings_rate(i: Inputs) -> float:
    """True total savings rate (pre-tax + post-tax) as a decimal.

    Numerator:   pretax_401k + pretax_hsa + post_tax_savings
    Denominator: after_tax_income  (always; base_mode does not affect this)

    Returns 0.0 when after_tax_income is zero or negative.  This rate lets
    all budgeting frameworks compare on equal footing regardless of which
    income base they each use internally.
    """
    base = after_tax_income(i)
    if base <= 0:
        return 0.0
    total_savings = i.pretax_401k + i.pretax_hsa + i.post_tax_savings
    return total_savings / base


# ---------- Per-framework helpers (private) ----------

def _fifty_thirty_twenty(base: float) -> dict:
    """50/30/20 — Warren & Tyagi, *All Your Worth* (2005).

    Needs 50% / Wants 30% / Savings 20%.
    After-tax base (or take-home if base_mode overridden).
    Health insurance premiums sit inside Needs (they are a necessary expense).
    Pre-tax 401k / HSA counts inside the Savings bucket — the money left to
    cover Needs and Wants is therefore take-home minus post-tax savings.
    """
    return {
        "needs": base * 0.50,
        "wants": base * 0.30,
        "savings": base * 0.20,
        "base": base,
    }


def _sixty_twenty_twenty(base: float) -> dict:
    """60/20/20 variant — higher Needs allocation for HCOL areas or families.

    Needs 60% / Wants 20% / Savings 20%.  After-tax base.
    Pre-tax savings count inside the 20% Savings bucket.
    """
    return {
        "needs": base * 0.60,
        "wants": base * 0.20,
        "savings": base * 0.20,
        "base": base,
    }


def _seventy_twenty_ten(base: float) -> dict:
    """70/20/10 variant — minimalist savings target for tight budgets.

    Needs 70% / Wants 20% / Savings 10%.  After-tax base.
    Pre-tax savings count inside the 10% Savings bucket.
    """
    return {
        "needs": base * 0.70,
        "wants": base * 0.20,
        "savings": base * 0.10,
        "base": base,
    }


def _eighty_twenty(base: float) -> dict:
    """80/20 variant — maximum simplicity: save 20%, spend the rest.

    No Wants/Needs split — one number to track.  After-tax base.
    Pre-tax savings count inside the 20% Savings bucket.
    """
    return {
        "needs": base * 0.80,
        "savings": base * 0.20,
        "base": base,
    }


def _fidelity_50_15_5(i: Inputs) -> dict:
    """Fidelity 50/15/5 — mixed-base framework.

    Essentials ≤50% and Short-term savings 5% use **net_take_home**.
    Retirement 15% uses **gross income + employer match** — Fidelity's actual
    definition; omitting the match understates the retirement target.

    gross_income is required for the 15% bucket. If omitted, retirementTarget
    is returned as None and a note explains what is missing.
    """
    take_home = i.net_take_home

    essentials_max = take_home * 0.50
    short_term_savings = take_home * 0.05

    if i.gross_income is not None:
        match_amt = i.employer_match if i.employer_match is not None else 0.0
        gross_base: float | None = i.gross_income + match_amt
        retirement_target: float | None = gross_base * 0.15
        note = (
            f"Retirement 15% target computed on gross + employer match "
            f"(${gross_base:,.0f}/yr)."
        )
    else:
        gross_base = None
        retirement_target = None
        note = (
            "gross_income is required to compute the Fidelity 15% retirement "
            "target. Provide gross_income (and optionally employer_match) to "
            "unlock this bucket."
        )

    return {
        "essentials": essentials_max,
        "retirementTarget": retirement_target,
        "shortTermSavings": short_term_savings,
        "grossBase": gross_base,
        "takeHomeBase": take_home,
        "note": note,
    }


def _reverse(base: float, savings_rate: float | None) -> dict:
    """Reverse / Pay-Yourself-First budget.

    Automate a fixed savings rate first; the remainder is free to spend
    without category tracking.  Pre-tax 401k / HSA counts toward the target.
    Default rate is 20% when savings_rate is not provided.
    """
    rate = savings_rate if savings_rate is not None else 0.20
    pay_yourself_first = base * rate
    free_to_spend = base - pay_yourself_first
    return {
        "payYourselfFirst": pay_yourself_first,
        "freeToSpend": free_to_spend,
        "rate": rate,
        "base": base,
    }


def _conscious_spending(base: float) -> dict:
    """Conscious Spending Plan — Ramit Sethi, *I Will Teach You to Be Rich*.

    Base is **always after_tax_income** — Sethi's "take-home plus 401k back"
    is equivalent to the after-tax reconstruction, so base_mode is ignored.
    Pre-tax 401k / HSA naturally occupies the Investments bucket.

    Buckets are min/max ranges (Sethi presents them as guidelines, not rules):
      Fixed costs:    50–60%   (rent, utilities, insurance, min-debt payments)
      Investments:    10%      (pre-tax 401k/HSA fill this slot)
      Savings goals:   5–10%  (emergency fund, vacation, car fund)
      Guilt-free:    20–35%   (dining, entertainment, hobbies — zero guilt)
    """
    return {
        "fixedCosts": {"min": base * 0.50, "max": base * 0.60},
        "investments": {"min": base * 0.10, "max": base * 0.10},
        "savingsGoals": {"min": base * 0.05, "max": base * 0.10},
        "guiltFree": {"min": base * 0.20, "max": base * 0.35},
        "base": base,
    }


# ---------- Public aggregates ----------

def frameworks(i: Inputs) -> dict:
    """Compute all seven framework allocations and return them in one dict.

    All amounts are annual dollars with camelCase keys for direct JS consumption.

    Base selection:
      - 50/30/20 variants and Reverse:  respect i.base_mode.
      - Fidelity 50/15/5:               net_take_home for essentials/short-term;
                                         gross + match for retirement (15%).
      - Conscious Spending Plan:         always after_tax_income (ignores base_mode).
    """
    base = _income_base(i)
    csp_base = after_tax_income(i)   # CSP always uses after-tax income

    return {
        "fiftyThirtyTwenty": _fifty_thirty_twenty(base),
        "sixtyTwentyTwenty": _sixty_twenty_twenty(base),
        "seventyTwentyTen": _seventy_twenty_ten(base),
        "eightyTwenty": _eighty_twenty(base),
        "fidelity50155": _fidelity_50_15_5(i),
        "reverse": _reverse(base, i.savings_rate),
        "consciousSpending": _conscious_spending(csp_base),
    }


def calculate(i: Inputs) -> dict:
    """Top-level entry point mirroring calculator.calculate() in shape.

    Returns a nested dict ready for JSON serialisation. All monetary values are
    annual dollars. All dict keys are camelCase for JS frontend consumption.

    hcolWarning fires when:
        (net_take_home − post_tax_savings) / after_tax_income < 0.50

    This signals that the cash available for actual spending is less than half
    of after-tax income — a HCOL area or high savings burden that makes the
    50/30/20 Needs target (50% of ATI) unrealistic. The UI should nudge toward
    the Conscious Spending Plan or zero-based budgeting in this case.
    """
    ati = after_tax_income(i)
    base = _income_base(i)

    # Tight-budget / HCOL signal. We can only assess this honestly when the user
    # supplies their actual planned spending (`categories`). If committed spending
    # exceeds 80% of the income base, less than 20% is left to save and the
    # 50/30/20 targets won't fit — what a high cost of living (or over-spending)
    # causes. We deliberately do NOT infer cost pressure from a high *voluntary*
    # savings rate: that is a choice, not a squeeze, and flagging it would falsely
    # alarm disciplined savers. Without categories we cannot observe "needs", so
    # the signal stays off rather than guessing.
    hcol_warning = False
    if i.categories:
        committed = sum(i.categories.values())
        hcol_warning = base > 0 and committed > 0.80 * base

    _base_label = "after-tax income" if i.base_mode == "after_tax" else "take-home"
    notes: list[str] = [
        f"Income base: {_base_label} → ${base:,.0f}/yr "
        f"(after-tax income = ${ati:,.0f}/yr).",
        "Pre-tax 401k/HSA is inside the savings bucket for 50/30/20, "
        "variants, Fidelity 50/15/5, Reverse, and CSP. "
        "It is outside YNAB/zero-based budgets (never reaches checking).",
    ]
    if hcol_warning:
        notes.append(
            "Tight-budget / HCOL alert: your planned spending uses more than 80% "
            "of income, leaving under 20% to save — the 50/30/20 targets won't "
            "fit. Common in high-cost-of-living areas; consider the Conscious "
            "Spending Plan or zero-based budgeting."
        )

    return {
        "inputs": {
            "netTakeHome": i.net_take_home,
            "pretax401k": i.pretax_401k,
            "pretaxHsa": i.pretax_hsa,
            "pretaxHealthPremiums": i.pretax_health_premiums,
            "otherNontaxPayroll": i.other_nontax_payroll,
            "postTaxSavings": i.post_tax_savings,
            "framework": i.framework,
            "savingsRate": i.savings_rate,
            "baseMode": i.base_mode,
            "grossIncome": i.gross_income,
            "employerMatch": i.employer_match,
        },
        "base": {
            "afterTaxIncome": ati,
            "netTakeHome": i.net_take_home,
            "incomeBaseUsed": i.base_mode,
            "baseAmount": base,
        },
        "frameworks": frameworks(i),
        "savingsRate": actual_savings_rate(i),
        "notes": notes,
        "hcolWarning": hcol_warning,
    }


__all__ = [
    "Inputs",
    "after_tax_income",
    "actual_savings_rate",
    "frameworks",
    "calculate",
]
