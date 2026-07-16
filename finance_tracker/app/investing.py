"""Investment order-of-operations (savings waterfall) for personal-finance planning.

Pure functions. No I/O. All monetary inputs and outputs are **annual dollars**.
Cadence conversion (monthly / per paycheck) is handled at the UI boundary.

Implements the Bogleheads / r/personalfinance / Money Guy FOO savings waterfall
documented in docs/financial-strategies.md §2.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Literal

import calculator as _calc
import finance_math as _fm


__all__ = [
    "Profile",
    "next_dollar_plan",
    "traditional_vs_roth",
    "pro_rata_warning",
    "target_allocation",
    "asset_location_guidance",
    "calculate",
    "project_growth",
]


# ---------- Profile dataclass ----------

@dataclass
class Profile:
    """All inputs for the investing module. Monetary values are annual dollars."""

    # Identity / income
    age: int
    state: Literal["TX", "CA", "none"]
    gross_income: float
    monthly_essential_expenses: float

    # Current savings balance
    ef_balance: float                   # existing emergency-fund balance

    # Debts — each dict: {balance: float, apr: float}  (apr as decimal, e.g. 0.20 = 20%)
    debts: list[dict]

    # Employer match rule: {pct_of_salary: float, match_rate: float}
    # e.g. {pct_of_salary: 0.04, match_rate: 0.50} = 50% match on first 4% of salary
    employer_match: dict

    # Remaining annual contribution room (what's left to fill this year)
    k401_room: float                    # remaining elective deferral (Trad + Roth 401k combined)
    hsa_room: float
    ira_room: float
    aftertax_401k_room: float           # §415(c) margin for after-tax contributions

    # Eligibility / situational flags
    hsa_eligible: bool = True
    roth_magi_over_limit: bool = False  # True → backdoor Roth instead of direct
    pretax_ira_balance: float = 0.0     # pre-tax IRA funds (rollover, deductible contribs)
    mega_available: bool = False        # plan supports after-tax contribs + in-plan Roth conversion
    retire_state_no_tax: bool = False   # plans to retire in TX/FL or other no-income-tax state

    # Retirement planning
    retire_marginal_rate: float = 0.22  # expected federal marginal rate in retirement (fallback)
    retire_income: float = 0.0          # expected annual retirement income → use its EFFECTIVE rate

    # Thresholds (surfaced in Advanced panel / /api/defaults)
    ef_starter_target: float = 1_000.0
    ef_months_target: int = 6
    high_interest_threshold: float = 0.06


# ---------- Private helpers ----------

def _approx_fed_marginal(gross_income: float) -> float:
    """Approximate current-year federal marginal rate from gross income.

    Uses the standard deduction default from calculator.py.  Good enough for
    Traditional-vs-Roth guidance; use the full tax calculator for exact take-home.
    """
    std_ded = _calc.Inputs().fed_std_deduction   # single source of truth (calculator.py)
    taxable = max(0.0, gross_income - std_ded)
    return _calc.find_marginal(taxable, _calc.DEFAULT_FED_BRACKETS)


def _retire_effective_rate(retire_income: float) -> float:
    """Effective federal rate on retirement withdrawals (total tax / income).

    Traditional withdrawals fill the brackets from the bottom, so the AVERAGE
    (effective) rate — not the marginal — is what deferred dollars actually cost
    at withdrawal. Comparing current marginal to retirement *marginal* overstates
    the Roth case (TODO-105); this uses the effective rate instead.
    """
    if retire_income <= 0:
        return 0.0
    std_ded = _calc.Inputs().fed_std_deduction
    return _calc.apply_brackets(max(0.0, retire_income - std_ded), _calc.DEFAULT_FED_BRACKETS) / retire_income


# ---------- Public functions ----------

def traditional_vs_roth(
    now_marginal_rate: float,
    retire_rate: float,
    state: str,
    retire_state_no_tax: bool,
) -> dict:
    """Compare current vs retirement marginal rates and return a Traditional/Roth recommendation.

    Special cases:
    - CA earner retiring in a no-income-tax state: Traditional wins strongly because
      distributions escape CA tax under 4 U.S.C. §114.
    - TX residents: no state income tax means the decision collapses to federal brackets only,
      modestly raising Roth's appeal vs a high-state-tax baseline.

    Returns {"recommend": "traditional"|"roth", "rationale": str}.
    """
    # CA → no-tax retirement: the state-tax arbitrage overrides the federal rate comparison.
    if state == "CA" and retire_state_no_tax:
        return {
            "recommend": "traditional",
            "rationale": (
                "CA state income tax (~9–13%) applies to contributions today; retiring in a "
                "no-income-tax state means traditional 401(k)/IRA distributions escape CA tax "
                "entirely (4 U.S.C. §114 bars the former state from taxing retirement income). "
                "Traditional is strongly favored — defer the tax you will never have to pay."
            ),
        }

    state_note = (
        " TX has no state income tax, so the Traditional vs Roth decision collapses to "
        "federal brackets only (no state-tax layer complicates it; modestly raises Roth's "
        "appeal at equal federal rates)."
    ) if state == "TX" else ""

    if now_marginal_rate > retire_rate:
        return {
            "recommend": "traditional",
            "rationale": (
                f"Current marginal rate ({now_marginal_rate:.0%}) exceeds expected retirement "
                f"rate ({retire_rate:.0%}). Pre-tax deferral saves more tax today than you will "
                f"owe at withdrawal.{state_note}"
            ),
        }

    if now_marginal_rate < retire_rate:
        return {
            "recommend": "roth",
            "rationale": (
                f"Expected retirement rate ({retire_rate:.0%}) exceeds current rate "
                f"({now_marginal_rate:.0%}). Pay tax now at the lower rate; "
                f"growth and withdrawals are then tax-free.{state_note}"
            ),
        }

    # Equal rates: Roth wins on tie-breakers (tax diversification, no RMDs).
    return {
        "recommend": "roth",
        "rationale": (
            f"Rates are equal ({now_marginal_rate:.0%} now vs {retire_rate:.0%} in retirement). "
            "Roth preferred for tax diversification, no required minimum distributions, and "
            f"tax-free compounding over time.{state_note}"
        ),
    }


def pro_rata_warning(profile: Profile) -> dict | None:
    """Return a warning dict if the pro-rata rule will make a backdoor Roth conversion taxable.

    The IRS 'cream-in-coffee' rule treats all traditional IRA funds as one pool.
    If any pre-tax dollars exist, the conversion is partially taxable:
        taxable_fraction = pretax_balance / (pretax_balance + new_contribution)

    Fix: roll the pre-tax balance into a 401(k) before Dec 31, then contribute and convert.
    File Form 8606 each year you make non-deductible contributions.

    Returns None if backdoor Roth is not recommended or no pre-tax balance exists.
    """
    if not profile.roth_magi_over_limit:
        return None
    if profile.pretax_ira_balance <= 0:
        return None

    contribution = max(0.0, profile.ira_room)
    total = profile.pretax_ira_balance + contribution
    taxable_fraction = profile.pretax_ira_balance / total if total > 0 else 0.0

    return {
        "type": "proRata",
        "pretaxIraBalance": profile.pretax_ira_balance,
        "plannedContribution": contribution,
        "taxableFraction": round(taxable_fraction, 4),
        "rationale": (
            f"Pro-rata rule: ${profile.pretax_ira_balance:,.0f} in pre-tax IRA funds will "
            f"make {taxable_fraction:.0%} of any Roth conversion taxable this year. "
            "Fix: roll pre-tax IRA balance into a 401(k) before Dec 31, then contribute "
            "and convert the new non-deductible amount. File Form 8606 annually."
        ),
    }


def target_allocation(age: int, aggressiveness: str = "moderate") -> dict:
    """Glide-path stock/bond split using (base − age) % in stocks.

    Bases by aggressiveness level:
        moderate     → 110 (Bogleheads default)
        aggressive   → 120
        conservative → 100

    Result is clamped to [0, 100].
    Returns {"stocksPct": float, "bondsPct": float}.
    """
    bases: dict[str, int] = {"moderate": 110, "aggressive": 120, "conservative": 100}
    base = bases.get(aggressiveness, 110)
    stocks_pct = float(max(0.0, min(100.0, base - age)))
    return {"stocksPct": stocks_pct, "bondsPct": 100.0 - stocks_pct}


def asset_location_guidance() -> dict:
    """Structured guidance on which asset classes belong in which account type.

    Source: Bogleheads asset location wiki + spec §2.
    """
    return {
        "taxable": {
            "preferred": [
                "US broad-market equity index (e.g. VTI, FSKAX)",
                "International equity index (e.g. VXUS, FZILX)",
                "Municipal bonds (interest federally tax-exempt)",
            ],
            "avoid": [
                "High-yield bonds (interest taxed as ordinary income)",
                "REITs (non-qualified dividends inflate tax drag)",
                "Actively managed funds (turnover generates taxable events)",
            ],
            "rationale": (
                "Tax-efficient equities generate mostly qualified dividends and long-term "
                "capital gains taxed at 0–20%, well below ordinary income rates."
            ),
        },
        "preTax": {
            "preferred": [
                "Bonds (BND, AGG, FXNAX)",
                "REITs (VNQ, FREL)",
                "High-yield bonds (HYG, JNK)",
                "Treasury Inflation-Protected Securities (TIPS)",
            ],
            "avoid": [],
            "rationale": (
                "Interest income and short-term gains are shielded from current tax. "
                "Withdrawals taxed as ordinary income in retirement — the same treatment "
                "bonds would get anyway, so the shelter is most valuable here."
            ),
        },
        "rothHsa": {
            "preferred": [
                "Highest-expected-return equities (small-cap value, international small-cap)",
                "REITs (if not already sheltered in pre-tax accounts)",
                "Aggressive growth index funds",
            ],
            "avoid": [],
            "rationale": (
                "Tax-free growth maximizes compounding on the highest-expected-return assets. "
                "HSA adds a third layer: withdrawals are also tax-free for qualified medical "
                "expenses, making it the most powerful account in the stack."
            ),
        },
    }


def next_dollar_plan(profile: Profile, amount: float) -> list[dict]:
    """Allocate `amount` dollars across the savings waterfall in optimal priority order.

    Waterfall (Bogleheads / r/personalfinance / Money Guy FOO):
        1.  Starter emergency fund   (~$1k; default ef_starter_target)
        2.  401(k) to full employer match  (never leave free money on the table)
        3.  High-interest debt — avalanche (highest APR first; above high_interest_threshold)
        4.  Full emergency fund  (ef_months_target months of essential expenses)
        5.  HSA  (triple-tax-advantaged; if hsa_eligible)
        6.  IRA  (Roth direct | backdoor Roth if over MAGI | Traditional per trad/roth sub-decision)
        7.  Max 401(k) remaining elective deferral
        8.  Mega-backdoor Roth  (if mega_available)
        9.  Taxable brokerage / 529 / low-interest-debt prepayment

    Each step dict: {bucket, amount, accountType, taxTreatment, rationale, roomRemaining}.
    Steps stop when `amount` is exhausted; a step only appears if it receives funds.
    """
    if amount <= 0:
        return []

    remaining = float(amount)
    steps: list[dict] = []

    # Mutable room trackers (local copies — do not mutate the Profile)
    k401_room = float(profile.k401_room)
    ira_room = float(profile.ira_room)

    # Running EF total so step 4 accounts for what step 1 already allocated.
    ef_total = float(profile.ef_balance)

    # Precompute Traditional vs Roth recommendation once.
    now_marginal = _approx_fed_marginal(profile.gross_income)
    # Compare current marginal to the retirement EFFECTIVE rate when a retirement income is
    # given (withdrawals fill brackets from the bottom); else fall back to the marginal (TODO-105).
    retire_rate = _retire_effective_rate(profile.retire_income) if profile.retire_income > 0 else profile.retire_marginal_rate
    trad_roth = traditional_vs_roth(
        now_marginal,
        retire_rate,
        profile.state,
        profile.retire_state_no_tax,
    )

    def _alloc(bucket: str, acct: str, tax: str, rationale: str, cap: float) -> float:
        """Allocate up to `cap` from `remaining`; append step; return amount allocated."""
        nonlocal remaining
        if remaining <= 0 or cap <= 0:
            return 0.0
        alloc = min(remaining, cap)
        remaining -= alloc
        steps.append({
            "bucket": bucket,
            "amount": round(alloc, 2),
            "accountType": acct,
            "taxTreatment": tax,
            "rationale": rationale,
            "roomRemaining": round(cap - alloc, 2),
        })
        return alloc

    # ── 1. Starter emergency fund ────────────────────────────────────────────
    starter_gap = max(0.0, profile.ef_starter_target - ef_total)
    ef_total += _alloc(
        "starterEf", "savings", "none",
        (
            f"Build starter emergency fund to ${profile.ef_starter_target:,.0f}. "
            "First safety net before any investing — covers a small unexpected expense "
            "without derailing the plan."
        ),
        starter_gap,
    )

    # ── 2. 401(k) to full employer match ─────────────────────────────────────
    match_pct = profile.employer_match.get("pct_of_salary", 0.0)
    match_rate = profile.employer_match.get("match_rate", 0.0)
    match_employee_target = match_pct * profile.gross_income
    match_cap = min(match_employee_target, k401_room)

    if match_cap > 0 and match_pct > 0 and match_rate > 0:
        employer_contribution = match_rate * match_employee_target
        matched = _alloc(
            "k401Match", "trad401k", "traditional",
            (
                f"Contribute ${match_employee_target:,.0f} to 401(k) to capture full employer "
                f"match ({match_rate:.0%} on {match_pct:.0%} of salary = "
                f"${employer_contribution:,.0f} free money). "
                "Instant guaranteed return — never skip the match."
            ),
            match_cap,
        )
        k401_room -= matched

    # ── 3. High-interest debt — avalanche (highest APR first) ────────────────
    high_debts = sorted(
        [
            d for d in profile.debts
            if d.get("apr", 0.0) > profile.high_interest_threshold
            and d.get("balance", 0.0) > 0
        ],
        key=lambda d: d["apr"],
        reverse=True,
    )
    for debt in high_debts:
        if remaining <= 0:
            break
        _alloc(
            "highInterestDebt",
            "debtPayoff",
            "none",
            (
                f"Avalanche: pay off {debt['apr'] * 100:.2f}% APR debt "
                f"(above {profile.high_interest_threshold * 100:.0f}% threshold). "
                "Guaranteed after-tax return equal to the interest rate eliminated."
            ),
            float(debt["balance"]),
        )

    # ── 4. Full emergency fund ────────────────────────────────────────────────
    full_ef_target = profile.monthly_essential_expenses * profile.ef_months_target
    full_ef_gap = max(0.0, full_ef_target - ef_total)
    ef_total += _alloc(
        "fullEf", "savings", "none",
        (
            f"Complete {profile.ef_months_target}-month emergency fund "
            f"(target ${full_ef_target:,.0f} = "
            f"${profile.monthly_essential_expenses:,.0f}/mo × {profile.ef_months_target} months). "
            "Fully funded before tax-advantaged investing maximizes risk tolerance."
        ),
        full_ef_gap,
    )

    # ── 5. HSA ────────────────────────────────────────────────────────────────
    if profile.hsa_eligible and profile.hsa_room > 0:
        _alloc(
            "hsa", "hsa", "tripleAdvantaged",
            (
                "HSA: triple-tax-advantaged — deduct contributions, grow tax-free, withdraw "
                "tax-free for qualified medical expenses. Payroll contributions also bypass FICA. "
                "After age 65, unused funds convert gracefully to a Traditional IRA."
            ),
            float(profile.hsa_room),
        )

    # ── 6. IRA ────────────────────────────────────────────────────────────────
    if ira_room > 0:
        if profile.roth_magi_over_limit:
            ira_acct = "backdoorRothIra"
            ira_tax = "roth"
            ira_rationale = (
                "Backdoor Roth IRA: contribute non-deductible to Traditional IRA, then "
                "immediately convert to Roth. File Form 8606 annually."
            )
            if profile.pretax_ira_balance > 0:
                ira_rationale += " WARNING: pro-rata rule applies — see warnings."
        elif trad_roth["recommend"] == "roth":
            ira_acct = "rothIra"
            ira_tax = "roth"
            ira_rationale = f"Direct Roth IRA contribution. {trad_roth['rationale']}"
        else:
            ira_acct = "traditionalIra"
            ira_tax = "traditional"
            ira_rationale = f"Deductible Traditional IRA. {trad_roth['rationale']}"

        ira_alloc = _alloc("ira", ira_acct, ira_tax, ira_rationale, ira_room)
        ira_room -= ira_alloc

    # ── 7. Max 401(k) remaining elective deferral ────────────────────────────
    if k401_room > 0:
        if trad_roth["recommend"] == "traditional":
            k401_acct = "trad401k"
            k401_tax = "traditional"
        else:
            k401_acct = "roth401k"
            k401_tax = "roth"

        k401_alloc = _alloc(
            "k401Max", k401_acct, k401_tax,
            f"Max remaining 401(k) elective deferral. {trad_roth['rationale']}",
            k401_room,
        )
        k401_room -= k401_alloc

    # ── 8. Mega-backdoor Roth ─────────────────────────────────────────────────
    if profile.mega_available and profile.aftertax_401k_room > 0:
        _alloc(
            "megaBackdoor", "afterTax401k", "roth",
            (
                f"Mega-backdoor Roth: after-tax 401(k) contribution "
                f"(§415(c) room = ${profile.aftertax_401k_room:,.0f}) with immediate "
                "in-plan Roth conversion. Requires plan support for both after-tax "
                "contributions and in-plan Roth rollover."
            ),
            float(profile.aftertax_401k_room),
        )

    # ── 9. Taxable brokerage ──────────────────────────────────────────────────
    if remaining > 0:
        _alloc(
            "taxable", "brokerage", "taxable",
            (
                "Taxable brokerage: no contribution limits. Qualified dividends and long-term "
                "capital gains taxed at 0–20%. Use tax-efficient broad index funds; "
                "see asset location guidance. Also consider 529 (education) or "
                "prepaying any remaining low-interest debt."
            ),
            remaining,
        )

    return steps


def calculate(profile: Profile, amount: float) -> dict:
    """Entry point: run the full investing analysis for `amount` dollars.

    Returns a nested dict:
        inputs          — echo of key profile fields (camelCase)
        plan            — list of allocation steps from next_dollar_plan()
        targetAllocation— {stocksPct, bondsPct} glide-path recommendation
        assetLocation   — structured guidance by account type
        warnings        — list of warning dicts (proRata, secure2CatchUp, …)
        notes           — list of disclaimer / caveat strings
    """
    plan = next_dollar_plan(profile, amount)
    allocation = target_allocation(profile.age)
    asset_loc = asset_location_guidance()
    # Traditional vs Roth (TODO-105): compare current marginal to the retirement EFFECTIVE
    # rate when a retirement income is given (withdrawals fill brackets from the bottom);
    # otherwise fall back to the supplied marginal estimate.
    now_marginal = _approx_fed_marginal(profile.gross_income)
    retire_rate = _retire_effective_rate(profile.retire_income) if profile.retire_income > 0 else profile.retire_marginal_rate
    trad_roth = traditional_vs_roth(now_marginal, retire_rate, profile.state, profile.retire_state_no_tax)

    warnings: list[dict] = []

    pro_rata = pro_rata_warning(profile)
    if pro_rata is not None:
        warnings.append(pro_rata)

    # SECURE 2.0 §603: age-50+ catch-up must be Roth when prior-year FICA wages > $145k.
    if profile.age >= 50 and profile.gross_income > 145_000:
        warnings.append({
            "type": "secure2CatchUp",
            "rationale": (
                "SECURE 2.0 §603: if prior-year FICA wages exceed $145,000, age-50+ "
                "catch-up contributions to a 401(k) must be designated Roth (not pre-tax). "
                "Verify with your plan administrator; the rule applies starting 2026."
            ),
        })

    notes = [
        "All monetary inputs are annual dollars; the UI handles cadence conversion.",
        "Contribution limits (401k, HSA, IRA) are 2026 projections — verify with IRS before filing.",
        "Traditional vs Roth recommendation compares your current marginal rate to an "
        "assumed retirement rate — genuinely uncertain; treat as 'consider', not advice.",
        "Roth IRA eligibility depends on your EXACT MAGI and phases out for high earners. "
        "If you are near or over the limit, use a backdoor Roth — an excess DIRECT "
        "contribution incurs a 6% IRS excise tax every year until corrected. Verify the "
        "current-year phase-out before contributing.",
        "Not financial, tax, or legal advice. Consult a CFP/CPA before making decisions.",
    ]

    return {
        "inputs": {
            "age": profile.age,
            "state": profile.state,
            "grossIncome": profile.gross_income,
            "monthlyEssentialExpenses": profile.monthly_essential_expenses,
            "efBalance": profile.ef_balance,
            "debtCount": len(profile.debts),
            "totalDebt": round(sum(d.get("balance", 0.0) for d in profile.debts), 2),
            "k401Room": profile.k401_room,
            "hsaRoom": profile.hsa_room,
            "iraRoom": profile.ira_room,
            "aftertax401kRoom": profile.aftertax_401k_room,
            "hsaEligible": profile.hsa_eligible,
            "rothMagiOverLimit": profile.roth_magi_over_limit,
            "pretaxIraBalance": profile.pretax_ira_balance,
            "megaAvailable": profile.mega_available,
            "retireStateNoTax": profile.retire_state_no_tax,
            "efStarterTarget": profile.ef_starter_target,
            "efMonthsTarget": profile.ef_months_target,
            "highInterestThreshold": profile.high_interest_threshold,
        },
        "plan": plan,
        "targetAllocation": allocation,
        "assetLocation": asset_loc,
        "tradVsRoth": {**trad_roth, "nowMarginal": now_marginal, "retireRate": retire_rate,
                       "retireRateBasis": "effective" if profile.retire_income > 0 else "marginal"},
        "warnings": warnings,
        "notes": notes,
    }


def project_growth(
    contributions: dict,
    balances: dict,
    annual_return: float,
    years: int,
) -> dict:
    """Project portfolio growth across multiple account types.

    contributions : annual dollars added per account type,
                    e.g. {"trad401k": 24500, "roth": 7500, "hsa": 4400, "taxable": 12000}
    balances      : current balance per account type (same keys or any subset)
    annual_return : expected annual return as a decimal (e.g. 0.07 for 7%)
    years         : projection horizon in whole years

    Contributions are modelled as an ordinary annuity (end-of-year) using
    finance_math.future_value_series.  Existing balances are grown with
    finance_math.future_value_lump.

    Per-account formula:
        futureValue = future_value_lump(start, annual_return, years)
                    + future_value_series(contrib, annual_return, years)
        totalContributed = contrib * years
        growth           = futureValue - start - totalContributed

    Edge cases:
        years <= 0  : returns current start balances as totals, empty yearByYear.
        empty dicts : returns all-zero structure with yearByYear of length `years`.
        0% return   : future_value_* handle correctly (no special-casing needed here).

    Returns a camelCase dict::

        {
            "byAccount": {
                "<type>": {
                    "startBalance":       float,
                    "annualContribution": float,
                    "totalContributed":   float,
                    "futureValue":        float,
                    "growth":             float,
                },
                ...
            },
            "totals": {
                "startBalance":     float,
                "totalContributed": float,
                "futureValue":      float,
                "growth":           float,
            },
            "yearByYear": [
                {"year": int, "value": float, "contributed": float},
                ...  # one entry per year 1..years
            ],
            "inputs": {"annualReturn": float, "years": int},
        }

    "value" at year t is the total portfolio value; "contributed" is the
    cumulative cost basis (all start balances + annual contributions × t).
    """
    all_accounts: list[str] = sorted(set(balances) | set(contributions))
    inputs_out: dict = {"annualReturn": annual_return, "years": years}

    # ── years <= 0: snapshot of current state, no growth ────────────────────
    if years <= 0:
        by_account: dict = {}
        for acct in all_accounts:
            start = float(balances.get(acct, 0.0))
            contrib = float(contributions.get(acct, 0.0))
            by_account[acct] = {
                "startBalance": round(start, 2),
                "annualContribution": round(contrib, 2),
                "totalContributed": 0.0,
                "futureValue": round(start, 2),
                "growth": 0.0,
            }
        total_start = sum(float(balances.get(a, 0.0)) for a in all_accounts)
        return {
            "byAccount": by_account,
            "totals": {
                "startBalance": round(total_start, 2),
                "totalContributed": 0.0,
                "futureValue": round(total_start, 2),
                "growth": 0.0,
            },
            "yearByYear": [],
            "inputs": inputs_out,
        }

    # ── empty inputs ─────────────────────────────────────────────────────────
    if not all_accounts:
        return {
            "byAccount": {},
            "totals": {
                "startBalance": 0.0,
                "totalContributed": 0.0,
                "futureValue": 0.0,
                "growth": 0.0,
            },
            "yearByYear": [
                {"year": t, "value": 0.0, "contributed": 0.0}
                for t in range(1, years + 1)
            ],
            "inputs": inputs_out,
        }

    # ── per-account computation (unrounded for precision) ────────────────────
    acct_data: dict[str, dict] = {}
    for acct in all_accounts:
        start = float(balances.get(acct, 0.0))
        contrib = float(contributions.get(acct, 0.0))
        fv = (
            _fm.future_value_lump(start, annual_return, years)
            + _fm.future_value_series(contrib, annual_return, years)
        )
        total_contributed = contrib * years
        growth = fv - start - total_contributed
        acct_data[acct] = {
            "start": start,
            "contrib": contrib,
            "fv": fv,
            "total_contributed": total_contributed,
            "growth": growth,
        }

    # ── byAccount (rounded at output boundary) ────────────────────────────────
    by_account = {
        acct: {
            "startBalance": round(d["start"], 2),
            "annualContribution": round(d["contrib"], 2),
            "totalContributed": round(d["total_contributed"], 2),
            "futureValue": round(d["fv"], 2),
            "growth": round(d["growth"], 2),
        }
        for acct, d in acct_data.items()
    }

    # ── totals (summed from unrounded values, rounded once) ──────────────────
    total_start = sum(d["start"] for d in acct_data.values())
    total_contributed = sum(d["total_contributed"] for d in acct_data.values())
    total_fv = sum(d["fv"] for d in acct_data.values())
    total_growth = total_fv - total_start - total_contributed

    # ── year-by-year portfolio value ─────────────────────────────────────────
    total_annual_contrib = sum(d["contrib"] for d in acct_data.values())
    year_by_year: list[dict] = []
    for t in range(1, years + 1):
        value_t = sum(
            _fm.future_value_lump(d["start"], annual_return, t)
            + _fm.future_value_series(d["contrib"], annual_return, t)
            for d in acct_data.values()
        )
        contributed_t = total_start + total_annual_contrib * t
        year_by_year.append({
            "year": t,
            "value": round(value_t, 2),
            "contributed": round(contributed_t, 2),
        })

    return {
        "byAccount": by_account,
        "totals": {
            "startBalance": round(total_start, 2),
            "totalContributed": round(total_contributed, 2),
            "futureValue": round(total_fv, 2),
            "growth": round(total_growth, 2),
        },
        "yearByYear": year_by_year,
        "inputs": inputs_out,
    }


def _years_to_net_worth(target: float, current: float, annual: float, rate: float, cap: float = 80.0) -> float | None:
    """Continuous years until net worth first reaches `target`, using the same FV model as
    project_net_worth: NW(t) = current*(1+r)^t + annual*((1+r)^t - 1)/r. None if unreachable within cap."""
    if current >= target:
        return 0.0
    if rate <= 0:
        if annual <= 0:
            return None
        t = (target - current) / annual
        return t if t <= cap else None
    base = current + annual / rate
    if base <= 0:
        return None
    g = (target + annual / rate) / base
    if g <= 1:
        return None
    t = math.log(g) / math.log(1 + rate)
    return t if t <= cap else None


def project_net_worth(
    current_net_worth: float,
    annual_contribution: float,
    years: int,
    return_rate: float = 0.07,
    band: float = 0.02,
    targets: list[float] | None = None,
) -> dict:
    """Project net worth from today forward: a starting balance plus level annual
    contributions, with a low / expected / high **confidence cone** (return_rate ± band).

    Year 0 anchors at today's net worth (all three bands equal). Each later year is the
    future value of the starting lump plus an ordinary (year-end) annuity of contributions,
    evaluated at the low/mid/high return. Contributions are assumed constant in nominal
    dollars; figures are nominal (not inflation-adjusted). Returns are an editable estimate.
    """
    rates = {
        "low": max(0.0, return_rate - band),
        "mid": return_rate,
        "high": return_rate + band,
    }

    def fv(rate: float, y: int) -> float:
        return _fm.future_value_lump(current_net_worth, rate, y) + _fm.future_value_series(
            annual_contribution, rate, y
        )

    year_by_year = [
        {
            "year": y,
            "low": round(fv(rates["low"], y), 2),
            "mid": round(fv(rates["mid"], y), 2),
            "high": round(fv(rates["high"], y), 2),
        }
        for y in range(0, int(years) + 1)
    ]
    final = year_by_year[-1]
    total_contributed = annual_contribution * int(years)

    # Milestones: continuous time to each target, with a fast/slow range from the high/low
    # return scenarios. Targets default to a round-number ladder above today's net worth, but
    # the caller can pass their own. Already-passed targets report reached=True (0 yr); targets
    # unreachable within ~80 yr report years=None (kept for user targets, dropped from the ladder).
    if targets:
        target_list = sorted({float(t) for t in targets if t and t > 0})[:12]
        ladder_mode = False
    else:
        target_list = [t for t in (250_000, 500_000, 1_000_000, 2_000_000, 3_000_000, 5_000_000, 10_000_000)
                       if t > current_net_worth][:6]
        ladder_mode = True
    milestones = []
    for tgt in target_list:
        mid_t = _years_to_net_worth(tgt, current_net_worth, annual_contribution, rates["mid"])
        if mid_t is None and ladder_mode:
            continue
        fast_t = _years_to_net_worth(tgt, current_net_worth, annual_contribution, rates["high"])
        slow_t = _years_to_net_worth(tgt, current_net_worth, annual_contribution, rates["low"])
        milestones.append({
            "target": tgt,
            "reached": tgt <= current_net_worth,
            "years": round(mid_t, 1) if mid_t is not None else None,
            "fastYears": round(fast_t, 1) if fast_t is not None else None,
            "slowYears": round(slow_t, 1) if slow_t is not None else None,
        })

    return {
        "inputs": {
            "currentNetWorth": current_net_worth,
            "annualContribution": annual_contribution,
            "years": int(years),
            "returnRate": return_rate,
            "band": band,
        },
        "rates": rates,
        "yearByYear": year_by_year,
        "final": {"low": final["low"], "mid": final["mid"], "high": final["high"]},
        "milestones": milestones,
        "totalContributed": round(total_contributed, 2),
        "totalGrowthMid": round(final["mid"] - current_net_worth - total_contributed, 2),
    }
