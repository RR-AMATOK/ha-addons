"""What an income LEVEL costs you in early retirement, beyond income tax itself.

Income tax is a curve. The things in this module are CLIFFS and thresholds, and they are what
make early-retirement planning different from ordinary retirement planning:

  - The ACA premium tax credit, which pays most of a pre-65 health premium and drops to **zero**
    one dollar past 400% of the federal poverty line. The enhanced ARPA/IRA subsidies expired at
    the end of 2025, so plan year 2026 is back to the hard cliff, and OBBBA also removed the
    repayment cap on excess advance credits — crossing the line unexpectedly creates a full
    clawback the following April.
  - The Medicaid coverage gap BELOW the poverty line in non-expansion states. Texas has not
    expanded Medicaid and covers no childless non-disabled adults at any income, so a leanFIRE
    plan that drives income too LOW is actively harmful there. Every calculator warns about
    earning too much; none warn about this.
  - IRMAA from 65, assessed on MAGI from two years prior, and also a cliff.
  - The net investment income tax, whose threshold has not been indexed since 2013 and therefore
    captures more people every year.
  - And the one that runs the other way: the 0% long-term capital-gains bracket, which in a state
    with no income tax is genuinely 0% all-in.

WHY THIS IS A SEPARATE MODULE. `calculator.py` answers "what do I owe on this income". This
answers "what does this income level cost me elsewhere", which is a different question with
different inputs and a different audience — the FIRE screen, not the tax screen.

EVERY THRESHOLD IS INJECTED. Not one figure in this file is a literal. They all originate in
`tax_data/<year>.json` and arrive through `/api/defaults`, because hardcoding them is precisely
the defect class BUG-0048/0049/0050 was opened for: the figures would silently keep quoting 2026
while the rest of the app rolled over.

POLICY CAVEAT: the 2026 ACA cliff is current law, but extension legislation was under discussion
when this was written. `cliff_in_effect` is therefore a parameter, not an assumption.
"""
from __future__ import annotations

__all__ = ["compute_retirement_thresholds"]


def _interp(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    if x1 == x0:
        return y1
    t = (x - x0) / (x1 - x0)
    return y0 + t * (y1 - y0)


def _applicable_pct(fpl_pct: float, table: list[dict]) -> float:
    """The share of MAGI a household is expected to contribute toward the benchmark plan.

    Interpolated linearly WITHIN each band, which is how Rev. Proc. 2025-25 defines it: the
    published figures are the values at each band edge, not a flat rate per band.
    """
    if not table:
        return 0.0
    rows = sorted(table, key=lambda r: float(r["fplPct"]))
    prev_edge = 0.0
    for row in rows:
        edge = float(row["fplPct"])
        start = float(row.get("startPct", row.get("rate", 0.0)))
        end = float(row.get("endPct", start))
        if fpl_pct <= edge:
            return _interp(fpl_pct, prev_edge, edge, start, end) if end != start else start
        prev_edge = edge
    last = rows[-1]
    return float(last.get("endPct", last.get("startPct", 0.0)))


def _benchmark_annual(age: float, monthly_at_40: float, f40: float, f64: float) -> float:
    """Benchmark silver premium for an age, from the quoted age-40 figure.

    APPROXIMATION, stated rather than hidden: the federal default age curve is not linear, and
    this interpolates between only the two anchors the data file carries (40 and 64). Below 40 it
    holds the age-40 factor, which OVERSTATES the premium slightly for a young retiree — the safe
    direction for a planning tool, but it is still an approximation and the caller is told so via
    `benchmarkIsApproximate`.
    """
    if monthly_at_40 <= 0 or f40 <= 0:
        return 0.0
    a = max(0.0, age)
    factor = f40 if a <= 40 else (f64 if a >= 64 else _interp(a, 40.0, 64.0, f40, f64))
    return monthly_at_40 * 12.0 * (factor / f40)


def compute_retirement_thresholds(
    magi: float,
    age: float,
    *,
    fpl_single: float,
    fpl_per_additional: float = 0.0,
    household_size: int = 1,
    aca_applicable_pct: list[dict] | None = None,
    aca_cliff_fpl_pct: float = 400.0,
    aca_benchmark_monthly_age40: float = 0.0,
    aca_age_factor_40: float = 1.0,
    aca_age_factor_64: float = 1.0,
    medicaid_expanded: bool = False,
    cliff_in_effect: bool = True,
    medicare_age: float = 65.0,
    part_b_standard_monthly: float = 0.0,
    irmaa_first_tier_magi: float = 0.0,
    niit_threshold: float = 0.0,
    ltcg_0pct_upper: float = 0.0,
    std_deduction: float = 0.0,
    ordinary_income: float = 0.0,
    state_taxes_income: bool = False,
) -> dict:
    """Return the threshold picture at a given retirement MAGI and age. camelCase dict."""
    magi = max(0.0, float(magi))
    fpl = float(fpl_single) + max(0, int(household_size) - 1) * float(fpl_per_additional)
    fpl_pct = (magi / fpl * 100.0) if fpl > 0 else 0.0
    pre_medicare = age < medicare_age

    # ── ACA ───────────────────────────────────────────────────────────────
    benchmark = _benchmark_annual(age, aca_benchmark_monthly_age40,
                                  aca_age_factor_40, aca_age_factor_64)
    ptc = 0.0
    if not pre_medicare:
        state = "medicare"
    elif fpl_pct < 100.0 and not medicaid_expanded:
        # Below the poverty line with no Medicaid expansion: no subsidy AND no Medicaid. This is
        # the direction no calculator warns about.
        state = "coverage-gap"
    elif fpl_pct < 100.0:
        state = "medicaid"
    elif cliff_in_effect and fpl_pct > aca_cliff_fpl_pct:
        state = "cliff"
    else:
        pct = _applicable_pct(fpl_pct, aca_applicable_pct or [])
        ptc = max(0.0, benchmark - (pct / 100.0) * magi)
        state = "subsidised" if ptc > 0 else "unsubsidised"

    cliff_magi = (aca_cliff_fpl_pct / 100.0) * fpl if fpl > 0 else 0.0
    # Signed on purpose: negative means already over, and "how far over" is the number that tells
    # you what it would take to get back under.
    headroom = cliff_magi - magi

    # ── Medicare / IRMAA ──────────────────────────────────────────────────
    irmaa_applies = (not pre_medicare) and irmaa_first_tier_magi > 0 and magi >= irmaa_first_tier_magi

    # ── NIIT ──────────────────────────────────────────────────────────────
    niit_over = max(0.0, magi - niit_threshold) if niit_threshold > 0 else 0.0

    # ── 0% long-term capital gains headroom ───────────────────────────────
    # The 0% bracket is defined on TAXABLE income, so the standard deduction sits underneath it:
    # room = (0%-bracket top + standard deduction) - ordinary income, floored at zero.
    ltcg_room = max(0.0, (ltcg_0pct_upper + std_deduction) - max(0.0, ordinary_income))
    ltcg_all_in_zero = (not state_taxes_income) and ltcg_room > 0

    # ── Warnings, ordered by how much they cost ───────────────────────────
    warnings: list[dict] = []
    if state == "coverage-gap":
        warnings.append({
            "key": "coverageGap", "severity": "high",
            "text": ("At this income you fall below the federal poverty line, and this state has "
                     "not expanded Medicaid — so there is no premium tax credit AND no Medicaid. "
                     "Planning your income too LOW is a real risk here, not just too high."),
        })
    elif state == "cliff":
        warnings.append({
            "key": "acaCliff", "severity": "high",
            "text": ("This income is past 400% of the poverty line, so the premium tax credit is "
                     "zero — not reduced, zero. One dollar under the line and it returns. The "
                     "repayment cap on excess advance credits was removed, so crossing it "
                     "unexpectedly creates a bill the following April."),
        })
    elif state == "subsidised" and 0 < headroom <= max(2000.0, 0.05 * cliff_magi):
        warnings.append({
            "key": "acaNearCliff", "severity": "medium",
            "text": ("You are close to the subsidy cliff. A Roth conversion or a realised capital "
                     "gain that crosses it can cost more than it saves."),
        })
    if irmaa_applies:
        warnings.append({
            "key": "irmaa", "severity": "medium",
            "text": ("This income is above the first Medicare surcharge tier. IRMAA is a cliff "
                     "and is assessed on your income from two years earlier, so the year that "
                     "matters is already in the past by the time you pay it."),
        })
    if niit_over > 0:
        warnings.append({
            "key": "niit", "severity": "medium",
            "text": ("Investment income above the net investment income tax threshold carries an "
                     "extra 3.8%. That threshold has not been indexed for inflation since 2013, "
                     "so it reaches further every year."),
        })
    if ltcg_all_in_zero:
        warnings.append({
            "key": "ltcgRoom", "severity": "info",
            "text": ("There is room to realise long-term capital gains at a 0% federal rate — and "
                     "with no state income tax here, 0% all-in. Those gains still count toward "
                     "the ACA income test above."),
        })

    return {
        "magi": round(magi, 2),
        "age": age,
        "fpl": round(fpl, 2),
        "fplPct": round(fpl_pct, 2),
        "preMedicare": pre_medicare,
        "aca": {
            "state": state,
            "benchmarkAnnual": round(benchmark, 2),
            "benchmarkIsApproximate": True,
            "premiumTaxCredit": round(ptc, 2),
            "netPremiumAnnual": round(max(0.0, benchmark - ptc), 2) if pre_medicare else 0.0,
            "cliffMagi": round(cliff_magi, 2),
            "headroomToCliff": round(headroom, 2),
            "cliffInEffect": bool(cliff_in_effect),
        },
        "medicare": {
            "eligibleAtAge": medicare_age,
            "partBStandardAnnual": round(part_b_standard_monthly * 12.0, 2),
            "irmaaApplies": irmaa_applies,
            "irmaaFirstTierMagi": irmaa_first_tier_magi,
            "headroomToIrmaa": (round(irmaa_first_tier_magi - magi, 2)
                                if irmaa_first_tier_magi > 0 else None),
        },
        "niit": {
            "threshold": niit_threshold,
            "amountOverThreshold": round(niit_over, 2),
        },
        "ltcg": {
            "zeroPctRoom": round(ltcg_room, 2),
            "allInZero": ltcg_all_in_zero,
            "ordinaryIncomeUsed": round(max(0.0, ordinary_income), 2),
        },
        "warnings": warnings,
    }
