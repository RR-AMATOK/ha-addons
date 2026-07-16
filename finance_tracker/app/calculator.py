"""Income tax calculation for 2026 (Single filer, Federal + TX/CA).

Pure functions. No I/O. The FastAPI server and any budget-app code can call
`calculate(Inputs(...))` directly.

All monetary inputs are **annual dollars**. Cadence conversion (monthly / per
paycheck) is handled at the UI boundary, not here.
"""

from dataclasses import dataclass, field, replace
from typing import Literal


# ---------- Bracket tables (editable at the UI; these are defaults) ----------

@dataclass(frozen=True)
class Bracket:
    upper: float | None   # None = infinity (top bracket)
    rate: float


# 2026 Federal Single brackets (IRS Rev. Proc. 2025-32).
DEFAULT_FED_BRACKETS: list[Bracket] = [
    Bracket(12_400,  0.10),
    Bracket(50_400,  0.12),
    Bracket(105_700, 0.22),
    Bracket(201_775, 0.24),
    Bracket(256_225, 0.32),
    Bracket(640_600, 0.35),
    Bracket(None,    0.37),
]

# 2026 Federal Married-Filing-Jointly brackets (IRS Rev. Proc. 2025-32).
# Note: the lower five breakpoints are exactly 2× Single, but the 35→37% breakpoint
# is NOT ($768,700, not 2×$640,600) — the top-bracket "marriage penalty".
MFJ_FED_BRACKETS: list[Bracket] = [
    Bracket(24_800,  0.10),
    Bracket(100_800, 0.12),
    Bracket(211_400, 0.22),
    Bracket(403_550, 0.24),
    Bracket(512_450, 0.32),
    Bracket(768_700, 0.35),
    Bracket(None,    0.37),
]

# 2025 California Single brackets (2026 CA release pending; editable).
DEFAULT_CA_BRACKETS: list[Bracket] = [
    Bracket(10_756,  0.010),
    Bracket(25_499,  0.020),
    Bracket(40_245,  0.040),
    Bracket(55_866,  0.060),
    Bracket(70_606,  0.080),
    Bracket(360_659, 0.093),
    Bracket(432_787, 0.103),
    Bracket(721_314, 0.113),
    Bracket(None,    0.123),
]


State = Literal["none", "TX", "CA", "WA"]
FilingStatus = Literal["single", "mfj"]


# ---------- Inputs ----------

@dataclass
class Inputs:
    # Compensation (annual, cash)
    salary: float = 0.0

    # Pre-tax payroll deductions
    trad_401k: float = 0.0         # Fed-exempt, NOT FICA-exempt, CA-exempt
    hsa: float = 0.0               # Fed-exempt, FICA-exempt, NOT CA-exempt
    employer_hsa: float = 0.0      # Employer HSA (§125): Fed/FICA-exempt, added to CA wages, no cash

    # Pre-tax §125 cafeteria-plan health/dental/vision premiums.
    # CA conforms to §125 for these (unlike §223 HSA) → premiums reduce
    # federal wages, FICA wages, AND CA wages (SDI follows FICA wages).
    medical: float = 0.0           # Medical insurance premium (§125, annual)
    dental: float = 0.0            # Dental insurance premium (§125, annual)
    vision: float = 0.0            # Vision insurance premium (§125, annual)

    # Post-tax payroll deductions (reduce cash, no tax benefit)
    roth_401k: float = 0.0
    ee_stock: float = 0.0          # ESPP contribution
    roth_ira: float = 0.0          # IRA contribution from net take-home

    # Non-cash taxable additions (flow into Box 1 / 3 / 5)
    er_stock: float = 0.0          # employer stock match, immediately taxable
    gtli: float = 0.0              # §79 imputed income on GTLI > $50k

    # Jurisdiction
    state: State = "CA"
    # Filing status. Informational on a directly-constructed Inputs (whose defaults are
    # Single); use Inputs.for_filing_status("mfj", ...) to also swap in the MFJ brackets,
    # standard deduction, NIIT / Add'l-Medicare / LTCG thresholds, and Roth MAGI band.
    filing_status: FilingStatus = "single"

    # HSA legal limits (2026 IRS projections; editable if IRS finalizes different).
    # TODO: HSA 55+ catch-up (+$1,000) — not modeled yet.
    hsa_coverage: Literal["self", "family"] = "self"
    hsa_limit_self: float = 4_400.0
    hsa_limit_family: float = 8_750.0

    # Roth IRA contribution + MAGI phase-out (Single filer, tax year 2026).
    # Source: IRS 2026 retirement-limit release. Editable via the Advanced panel;
    # the canonical figures live in tax_data/2026.json (kept current by
    # scripts/update_tax_values.py).
    roth_ira_limit: float = 7_500.0
    roth_ira_phase_in: float = 153_000.0   # full contribution allowed at or below
    roth_ira_phase_out: float = 168_000.0  # zero contribution allowed at or above

    # Backdoor Roth IRA: when True, the phase-out is bypassed entirely and the
    # full roth_ira_limit is allowed regardless of MAGI.
    backdoor_roth: bool = False

    # Bonus / supplemental income.  Taxed at marginal rates stacked on top of
    # regular wages.  Never changes take-home, total comp, or any wage base.
    bonus: float = 0.0

    # Mega-backdoor Roth source (after-tax 401k).  Post-tax payroll deduction:
    # reduces take-home (like roth_401k) but does NOT reduce any wage base.
    after_tax_401k: float = 0.0

    # Employer 401(k) plan contributions (match / profit-sharing / non-elective).
    # Counts toward the §415(c) annual-additions limit. NOT er_stock (that is
    # taxable stock comp into Box 1, not a qualified-plan addition).
    employer_401k_match: float = 0.0
    # §415(c) overall defined-contribution annual-additions limit (2026).
    sec415c_limit: float = 72_000.0

    # ----- Investment income (taxed at FILING, isolated from the paycheck) -----
    # Net figures after intra-category offsets; loss-netting / $3k deduction not modeled.
    long_term_gains: float = 0.0       # net LTCG (Schedule D line 15 if > 0)
    short_term_gains: float = 0.0      # net STCG (taxed as ordinary)
    qualified_dividends: float = 0.0   # 1099-DIV box 1b (subset of ordinary_dividends)
    ordinary_dividends: float = 0.0    # 1099-DIV box 1a (total, incl. qualified)
    taxable_interest: float = 0.0      # 1099-INT box 1 (taxed as ordinary)
    # LTCG / qualified-dividend preferential-rate breakpoints (Single, 2026).
    ltcg_0pct_upper: float = 49_450.0  # total taxable income at/below which LTCG = 0%
    ltcg_15pct_upper: float = 545_500.0  # ... below which LTCG = 15% (20% above)
    # Net Investment Income Tax (§1411). Threshold is statutory, NOT inflation-indexed.
    niit_threshold: float = 200_000.0  # single filer MAGI threshold
    niit_rate: float = 0.038
    # Supplemental-wage flat federal withholding rate (bonus/RSU vests under $1M).
    supplemental_withholding_rate: float = 0.22

    # ----- ESPP disposition (opt-in; only enter if shares were SOLD this year) -----
    espp_shares_sold: float = 0.0
    espp_purchase_price_per_share: float = 0.0    # discounted price actually paid
    espp_purchase_fmv_per_share: float = 0.0      # FMV on the purchase/exercise date
    espp_grant_fmv_per_share: float = 0.0         # FMV on the grant/offering date
    espp_sale_price_per_share: float = 0.0
    espp_qualifying: bool = True                  # True = qualifying, False = disqualifying
    espp_disq_gain_long_term: bool = False        # disqualifying only: was the cap gain LT?
    # ----- RSU disposition (vest was already W-2; this is the SALE only) -----
    rsu_shares_sold: float = 0.0
    rsu_vest_fmv_per_share: float = 0.0           # cost basis = FMV at vest
    rsu_sale_price_per_share: float = 0.0
    rsu_long_term: bool = True                    # True = LTCG, False = STCG

    # ----- Safe harbor / estimated tax (opt-in: enter prior-year total federal tax) -----
    prior_year_fed_tax: float = 0.0
    safe_harbor_rate: float = 1.10                # 110% of prior-year (AGI > $150k); else 1.00

    # Federal
    fed_std_deduction: float = 16_100.0
    fed_brackets: list[Bracket] = field(default_factory=lambda: list(DEFAULT_FED_BRACKETS))

    # FICA (2026)
    ss_wage_base: float = 184_500.0
    ss_rate: float = 0.062
    medicare_rate: float = 0.0145
    addl_medicare_threshold: float = 200_000.0    # single filer
    addl_medicare_rate: float = 0.009

    # California
    ca_std_deduction: float = 5_540.0
    ca_brackets: list[Bracket] = field(default_factory=lambda: list(DEFAULT_CA_BRACKETS))
    ca_sdi_rate: float = 0.012                    # uncapped since 2024
    ca_mhst_threshold: float = 1_000_000.0
    ca_mhst_rate: float = 0.01

    # Washington — no wage income tax; two payroll deductions plus a capital-gains
    # excise at filing (2026 values, sources in state_compare.py / TODO-224).
    wa_pfml_total_rate: float = 0.0113            # PFML premium; wages capped at ss_wage_base
    wa_pfml_employee_share: float = 0.7143        # employer covers the rest
    wa_cares_rate: float = 0.0058                 # WA Cares LTC; NO wage cap, 100% employee
    wa_ltcg_deduction: float = 278_000.0          # TY2025 figure; 2026 unpublished as of 2026-07
    wa_ltcg_rate: float = 0.07
    wa_ltcg_surcharge_rate: float = 0.029         # +2.9% (9.9% total) above the $1M tier
    wa_ltcg_surcharge_threshold: float = 1_000_000.0

    @classmethod
    def for_filing_status(cls, filing_status: FilingStatus = "single", **overrides) -> "Inputs":
        """Build Inputs with filing-status-correct federal defaults.

        ``single`` uses the dataclass defaults (the single source of truth that
        tax_data/<year>.json overlays). ``mfj`` layers FILING_STATUS_OVERRIDES["mfj"]
        on top, then any caller ``overrides`` win over both. FICA (SS wage base,
        Medicare) is per-individual and identical across statuses, so it is untouched.
        """
        base = dict(FILING_STATUS_OVERRIDES.get(filing_status, {}))
        return cls(filing_status=filing_status, **{**base, **overrides})


# Federal constants that differ by filing status. ``single`` is the Inputs() default,
# so only the MFJ deltas are listed here (2026, IRS Rev. Proc. 2025-32). FICA is
# per-person and unchanged; CA figures are independent of federal filing status.
FILING_STATUS_OVERRIDES: dict[str, dict] = {
    "mfj": {
        "fed_std_deduction": 32_200.0,
        "fed_brackets": list(MFJ_FED_BRACKETS),
        "addl_medicare_threshold": 250_000.0,   # §3101(b)(2) MFJ liability threshold
        "niit_threshold": 250_000.0,            # §1411 MFJ MAGI threshold
        "ltcg_0pct_upper": 98_900.0,
        "ltcg_15pct_upper": 613_700.0,
        "roth_ira_phase_in": 242_000.0,
        "roth_ira_phase_out": 252_000.0,
    },
}


# ---------- Bracket math ----------

def apply_brackets(income: float, brackets: list[Bracket]) -> float:
    if income <= 0:
        return 0.0
    tax = 0.0
    lower = 0.0
    for b in brackets:
        upper = b.upper if b.upper is not None else float("inf")
        if income <= lower:
            break
        slice_amt = min(income, upper) - lower
        if slice_amt > 0:
            tax += slice_amt * b.rate
        if income <= upper:
            break
        lower = upper
    return tax


def find_marginal(income: float, brackets: list[Bracket]) -> float:
    for b in brackets:
        upper = b.upper if b.upper is not None else float("inf")
        if income <= upper:
            return b.rate
    return brackets[-1].rate


# ---------- Core ----------

def calculate(i: Inputs) -> dict:
    taxable_additions = i.er_stock + i.gtli

    # §125 medical/dental/vision premiums: reduce all three wage bases.
    mdv = i.medical + i.dental + i.vision

    # Wage bases (annual).
    # Employer HSA is excluded from Federal Box 1 and FICA Box 3/5 under
    # §125 + §223; California does NOT conform to §223 → it is added to CA wages.
    # MDV premiums (§125) are exempt from Federal, FICA, AND CA (CA conforms to §125).
    fed_wages = max(0.0, i.salary + taxable_additions - i.trad_401k - i.hsa - mdv)
    fica_wages = max(0.0, i.salary + taxable_additions - i.hsa - mdv)
    ca_wages = max(0.0, i.salary + taxable_additions - i.trad_401k + i.employer_hsa - mdv)

    # Federal income tax
    fed_taxable = max(0.0, fed_wages - i.fed_std_deduction)
    fed_tax = apply_brackets(fed_taxable, i.fed_brackets)

    # FICA
    ss_tax = min(fica_wages, i.ss_wage_base) * i.ss_rate
    med_tax = fica_wages * i.medicare_rate
    addl_med_tax = max(0.0, fica_wages - i.addl_medicare_threshold) * i.addl_medicare_rate

    # State
    state_tax = 0.0
    sdi_tax = 0.0
    state_taxable = 0.0
    wa_pfml_tax = 0.0
    wa_cares_tax = 0.0
    if i.state == "CA":
        state_taxable = max(0.0, ca_wages - i.ca_std_deduction)
        state_tax = apply_brackets(state_taxable, i.ca_brackets)
        if state_taxable > i.ca_mhst_threshold:
            state_tax += (state_taxable - i.ca_mhst_threshold) * i.ca_mhst_rate
        sdi_tax = fica_wages * i.ca_sdi_rate
    elif i.state == "WA":
        # No wage income tax. PFML rides the FICA wage concept capped at the SS wage
        # base (the caps track each other by law); WA Cares is uncapped.
        # DOCUMENTED APPROXIMATION: fica_wages excludes §125 cafeteria amounts (HSA/MDV),
        # while real PFML/Cares wages generally do NOT exclude them — an HSA contributor
        # is modeled a few dollars light (~1.39% of the cafeteria amount). Accepted for
        # consistency with the CA SDI base; revisit if payroll-exact figures ever matter.
        wa_pfml_tax = min(fica_wages, i.ss_wage_base) * i.wa_pfml_total_rate * i.wa_pfml_employee_share
        wa_cares_tax = fica_wages * i.wa_cares_rate

    total_tax = fed_tax + ss_tax + med_tax + addl_med_tax + state_tax + sdi_tax + wa_pfml_tax + wa_cares_tax

    # Cash flow: ER stock and GTLI are non-cash → they add to tax but not to take-home
    # MDV premiums are a cash pre-tax deduction (like trad_401k / hsa).
    # after_tax_401k is a post-tax payroll deduction (no tax benefit, but reduces take-home).
    cash_pre_tax = i.trad_401k + i.hsa + mdv
    cash_post_tax = i.roth_401k + i.ee_stock + i.roth_ira + i.after_tax_401k
    take_home = i.salary - cash_pre_tax - cash_post_tax - total_tax

    # Employer HSA is non-cash compensation; the bonus is also compensation, so the
    # total-comp / gross / earnings figures AGGREGATE the bonus (take-home and the
    # regular paycheck do NOT). Effective rate (below) includes the bonus tax too.
    total_comp = i.salary + taxable_additions + i.employer_hsa + i.bonus
    marginal = find_marginal(fed_taxable, i.fed_brackets)
    # All-in marginal on the next earned wage dollar: fed bracket + FICA (+ CA when applicable).
    # This is the true wedge on a dollar of pay; the fed-only figure understates it.
    marginal_all_in = marginal + i.medicare_rate
    if fica_wages >= i.addl_medicare_threshold:
        marginal_all_in += i.addl_medicare_rate
    if fica_wages < i.ss_wage_base:
        marginal_all_in += i.ss_rate
    if i.state == "CA":
        marginal_all_in += find_marginal(state_taxable, i.ca_brackets) + i.ca_sdi_rate
        if state_taxable > i.ca_mhst_threshold:
            marginal_all_in += i.ca_mhst_rate
    elif i.state == "WA":
        marginal_all_in += i.wa_cares_rate
        if fica_wages < i.ss_wage_base:
            marginal_all_in += i.wa_pfml_total_rate * i.wa_pfml_employee_share

    hsa_legal = i.hsa_limit_family if i.hsa_coverage == "family" else i.hsa_limit_self
    hsa_employee_cap = max(0.0, hsa_legal - i.employer_hsa)

    # Roth IRA MAGI phase-out (Single). MAGI ≈ Federal Box 1 wages for a simple
    # W-2 employee with no above-the-line add-backs — good enough for this tool.
    magi = fed_wages
    if magi <= i.roth_ira_phase_in:
        roth_ira_max = i.roth_ira_limit
    elif magi >= i.roth_ira_phase_out:
        roth_ira_max = 0.0
    else:
        frac = (i.roth_ira_phase_out - magi) / (i.roth_ira_phase_out - i.roth_ira_phase_in)
        roth_ira_max = i.roth_ira_limit * frac

    # Backdoor Roth IRA bypass: treat the phase-out as if it does not exist.
    if i.backdoor_roth:
        roth_ira_max = i.roth_ira_limit

    # ----- Bonus: incremental (marginal) taxes stacked on top of regular wages -----
    # The bonus is NOT reduced by any pre-tax deduction and does NOT change any
    # wage base, take-home, total_comp, or the regular tax section above.
    bonus_fed = apply_brackets(fed_taxable + i.bonus, i.fed_brackets) - fed_tax
    bonus_ss = (
        (min(fica_wages + i.bonus, i.ss_wage_base) - min(fica_wages, i.ss_wage_base))
        * i.ss_rate
    )
    bonus_med = i.bonus * i.medicare_rate
    bonus_addl = (
        max(0.0, fica_wages + i.bonus - i.addl_medicare_threshold)
        - max(0.0, fica_wages - i.addl_medicare_threshold)
    ) * i.addl_medicare_rate
    bonus_state = 0.0
    bonus_sdi = 0.0
    bonus_wa_pfml = 0.0
    bonus_wa_cares = 0.0
    if i.state == "CA":
        bonus_state = (
            apply_brackets(state_taxable + i.bonus, i.ca_brackets)
            - apply_brackets(state_taxable, i.ca_brackets)
            + (
                max(0.0, state_taxable + i.bonus - i.ca_mhst_threshold)
                - max(0.0, state_taxable - i.ca_mhst_threshold)
            ) * i.ca_mhst_rate
        )
        bonus_sdi = i.bonus * i.ca_sdi_rate
    elif i.state == "WA":
        bonus_wa_pfml = (
            min(fica_wages + i.bonus, i.ss_wage_base) - min(fica_wages, i.ss_wage_base)
        ) * i.wa_pfml_total_rate * i.wa_pfml_employee_share
        bonus_wa_cares = i.bonus * i.wa_cares_rate
    bonus_total = bonus_fed + bonus_ss + bonus_med + bonus_addl + bonus_state + bonus_sdi + bonus_wa_pfml + bonus_wa_cares
    bonus_net = i.bonus - bonus_total
    bonus_eff = bonus_total / i.bonus if i.bonus > 0 else 0.0
    # Supplemental-wage withholding gap: a bonus/RSU vest has federal income tax WITHHELD
    # at the flat 22% supplemental rate, but the true liability is the stacked-marginal
    # bonus_fed — so a high earner under-withholds and owes the difference at filing.
    bonus_withheld_flat = i.bonus * i.supplemental_withholding_rate
    bonus_under_withholding = bonus_fed - bonus_withheld_flat

    # ----- ESPP / RSU dispositions (share SALES) — isolated, owed at filing -----
    # ESPP qualifying: ordinary/share = min(sale−price, grant_fmv−price) (lesser-of); rest LTCG.
    # ESPP disqualifying: ordinary/share = min(purchase_fmv−price, sale−price); rest cap gain.
    # RSU sale: pure capital gain over the vest-date basis (vest was already W-2). The ESPP
    # ordinary portion is COMP income (ordinary rate) but NOT FICA at sale and NOT in NII.
    espp_n = i.espp_shares_sold
    espp_spread = i.espp_sale_price_per_share - i.espp_purchase_price_per_share
    if i.espp_qualifying:
        espp_ord_ps = max(0.0, min(espp_spread, i.espp_grant_fmv_per_share - i.espp_purchase_price_per_share))
        espp_cap_long = True
    else:
        espp_ord_ps = max(0.0, min(i.espp_purchase_fmv_per_share - i.espp_purchase_price_per_share, espp_spread))
        espp_cap_long = i.espp_disq_gain_long_term
    espp_ordinary = espp_ord_ps * espp_n
    espp_cap_gain = (espp_spread - espp_ord_ps) * espp_n          # signed (negative = loss)
    rsu_cap_gain = (i.rsu_sale_price_per_share - i.rsu_vest_fmv_per_share) * i.rsu_shares_sold
    disp_ordinary = espp_ordinary
    disp_st_gain = (0.0 if espp_cap_long else espp_cap_gain) + (0.0 if i.rsu_long_term else rsu_cap_gain)
    disp_lt_gain = (espp_cap_gain if espp_cap_long else 0.0) + (rsu_cap_gain if i.rsu_long_term else 0.0)
    disp_nii = espp_cap_gain + rsu_cap_gain                        # cap gains only (ESPP comp excluded from NII)

    # ----- Investment income: capital gains, qualified dividends, interest, NIIT -----
    # ISOLATED like the bonus — investment tax settles at FILING (it is not withheld),
    # so NOTHING here touches take_home / total_tax / total_comp / any wage base. The
    # Roth-IRA MAGI above is deliberately left as fed_wages (a documented under-count);
    # NIIT uses its OWN MAGI so the two are never conflated.
    qual_div = min(i.qualified_dividends, i.ordinary_dividends)   # qualified ⊆ ordinary
    non_qual_div = max(0.0, i.ordinary_dividends - qual_div)
    ltcg = max(0.0, i.long_term_gains)
    stcg = max(0.0, i.short_term_gains)
    interest = max(0.0, i.taxable_interest)
    # Ordinary-rate income stacks on ordinary taxable income (incl. ESPP comp + short-term disposition gains).
    ordinary_additions = stcg + non_qual_div + interest + disp_ordinary + disp_st_gain
    inv_ordinary_tax = apply_brackets(fed_taxable + ordinary_additions, i.fed_brackets) - fed_tax
    # Preferential income (LTCG + qualified dividends + long-term disposition gains) stacks ABOVE,
    # taxed at 0/15/20% by where it lands in the total-taxable-income stack.
    pref_income = ltcg + qual_div + disp_lt_gain
    oti = fed_taxable + ordinary_additions
    pti_end = oti + pref_income
    ltcg_at_0 = max(0.0, min(pti_end, i.ltcg_0pct_upper) - oti)
    ltcg_at_15 = max(0.0, min(pti_end, i.ltcg_15pct_upper) - max(oti, i.ltcg_0pct_upper))
    ltcg_at_20 = max(0.0, pti_end - max(oti, i.ltcg_15pct_upper))
    ltcg_tax = ltcg_at_15 * 0.15 + ltcg_at_20 * 0.20
    # NIIT (§1411): 3.8% on the LESSER of net investment income or (MAGI − threshold).
    net_investment_income = ltcg + stcg + i.ordinary_dividends + interest + disp_nii
    magi_niit = fed_wages + net_investment_income + disp_ordinary   # ESPP comp raises MAGI but not NII
    niit = max(0.0, min(net_investment_income, max(0.0, magi_niit - i.niit_threshold)) * i.niit_rate)
    # WA capital-gains excise (filing-time, like the rest of this section): LONG-TERM
    # gains only — STCG, dividends, interest, and W-2 stock comp are out of scope.
    wa_capgains_tax = 0.0
    if i.state == "WA":
        wa_taxable_ltcg = max(0.0, (ltcg + disp_lt_gain) - i.wa_ltcg_deduction)
        wa_capgains_tax = (
            i.wa_ltcg_rate * min(wa_taxable_ltcg, i.wa_ltcg_surcharge_threshold)
            + (i.wa_ltcg_rate + i.wa_ltcg_surcharge_rate)
            * max(0.0, wa_taxable_ltcg - i.wa_ltcg_surcharge_threshold)
        )
    investment_tax = inv_ordinary_tax + ltcg_tax + niit + wa_capgains_tax
    total_investment_income = ordinary_additions + pref_income
    inv_eff = investment_tax / total_investment_income if total_investment_income > 0 else 0.0

    # ----- §415(c) overall annual-additions limit (mega-backdoor headroom) -----
    # Counts elective deferrals + after-tax + employer plan contributions. Excludes HSA,
    # Roth IRA, age-50 catch-up, and er_stock (taxable stock comp, not a plan addition).
    sec415c_additions = i.trad_401k + i.roth_401k + i.after_tax_401k + i.employer_401k_match
    sec415c_room = i.sec415c_limit - sec415c_additions
    sec415c_overage = max(0.0, sec415c_additions - i.sec415c_limit)

    # ----- Safe harbor / estimated tax (only meaningful once prior_year_fed_tax is entered) -----
    # Target the 110%-of-prior-year safe harbor (reliable; doesn't depend on current-year
    # completeness — see DEC-005 review). Projected federal income tax WITHHELD ≈ wage liability
    # + the 22%-flat bonus withholding. The shortfall is what to pay in quarterly estimates to
    # avoid the §6654 underpayment penalty. The 90%-of-current path is reported only as a caveated
    # secondary (it relies on income this tool may not fully capture).
    # FEDERAL only: the WA capital-gains excise inside investment_tax is a STATE tax and
    # must not inflate the federal safe-harbor target (review finding, 2026-07-09).
    current_total_fed = fed_tax + bonus_fed + (investment_tax - wa_capgains_tax)
    fed_withholding = fed_tax + bonus_withheld_flat
    safe_harbor_target = i.prior_year_fed_tax * i.safe_harbor_rate
    safe_harbor_shortfall = max(0.0, safe_harbor_target - fed_withholding)

    # Effective rate over total comp INCLUDING the bonus and the tax on it.
    effective = (total_tax + bonus_total) / total_comp if total_comp > 0 else 0.0

    return {
        "inputs": {
            "salary": i.salary,
            "trad401k": i.trad_401k,
            "roth401k": i.roth_401k,
            "hsa": i.hsa,
            "employerHsa": i.employer_hsa,
            "medical": i.medical,
            "dental": i.dental,
            "vision": i.vision,
            "hsaCoverage": i.hsa_coverage,
            "eeStock": i.ee_stock,
            "erStock": i.er_stock,
            "gtli": i.gtli,
            "rothIra": i.roth_ira,
            "state": i.state,
            "bonus": i.bonus,
            "afterTax401k": i.after_tax_401k,
            "backdoorRoth": i.backdoor_roth,
        },
        "wages": {
            "fedWages": fed_wages,
            "fedTaxable": fed_taxable,
            "ficaWages": fica_wages,
            "caWages": ca_wages,
            "stateTaxable": state_taxable,
        },
        "taxes": {
            "fedTax": fed_tax,
            "ssTax": ss_tax,
            "medTax": med_tax,
            "addlMedTax": addl_med_tax,
            "stateTax": state_tax,
            "sdiTax": sdi_tax,
            "waPfmlTax": wa_pfml_tax,
            "waCaresTax": wa_cares_tax,
            "totalTax": total_tax,
        },
        "cash": {
            "salary": i.salary,
            "trad401k": i.trad_401k,
            "roth401k": i.roth_401k,
            "hsa": i.hsa,
            "employerHsa": i.employer_hsa,
            "medical": i.medical,
            "dental": i.dental,
            "vision": i.vision,
            "mdv": mdv,
            "eeStock": i.ee_stock,
            "erStock": i.er_stock,
            "gtli": i.gtli,
            "rothIra": i.roth_ira,
            "afterTax401k": i.after_tax_401k,
            "cashPreTax": cash_pre_tax,
            "cashPostTax": cash_post_tax,
            "takeHome": take_home,
            "totalComp": total_comp,
            # Summary lines for the paycheck view:
            "totalGross": i.salary + taxable_additions + i.bonus,   # gross taxable wages incl. bonus
            "totalEarnings": total_comp,                            # full economic value incl. employer HSA + bonus
            "totalDeductions": cash_pre_tax + cash_post_tax,   # all pre-tax + post-tax payroll deductions
        },
        "limits": {
            "hsaLegal": hsa_legal,
            "hsaEmployee": hsa_employee_cap,
            "hsaCoverage": i.hsa_coverage,
            "rothIraLimit": i.roth_ira_limit,
            "rothIraMax": roth_ira_max,
            "rothIraPhaseIn": i.roth_ira_phase_in,
            "rothIraPhaseOut": i.roth_ira_phase_out,
            "rothIraMagi": magi,
            # §415(c) overall annual-additions limit (mega-backdoor headroom).
            "sec415cLimit": i.sec415c_limit,
            "sec415cAdditions": sec415c_additions,
            "sec415cRoom": sec415c_room,            # negative ⇒ over the limit
            "sec415cOverage": sec415c_overage,      # 0 when compliant
        },
        "rates": {
            "effRate": effective,
            "marginal": marginal,
            "marginalAllIn": marginal_all_in,
        },
        # Bonus section: all values are zero when no bonus was provided.
        # These figures are ISOLATED — they do not affect any value above.
        "bonus": {
            "gross": i.bonus,
            "fedTax": bonus_fed,
            "ssTax": bonus_ss,
            "medTax": bonus_med,
            "addlMedTax": bonus_addl,
            "stateTax": bonus_state,
            "sdiTax": bonus_sdi,
            "waPfmlTax": bonus_wa_pfml,
            "waCaresTax": bonus_wa_cares,
            "totalTax": bonus_total,
            "net": bonus_net,
            "effectiveRate": bonus_eff,
            # Supplemental-wage withholding gap (federal income tax only): flat 22%
            # withheld vs. the true stacked-marginal liability → owed at filing.
            "withheldFlat": bonus_withheld_flat,
            "underWithholding": bonus_under_withholding,
        },
        # Investment income: ISOLATED — taxed at filing, NOT withheld. Does not affect
        # take-home, total tax, or any wage base above. All zero when no inputs given.
        "investment": {
            "longTermGains": i.long_term_gains,
            "shortTermGains": i.short_term_gains,
            "qualifiedDividends": i.qualified_dividends,
            "ordinaryDividends": i.ordinary_dividends,
            "taxableInterest": i.taxable_interest,
            "ordinaryAdditions": ordinary_additions,        # STCG + non-qual div + interest
            "preferentialIncome": pref_income,              # LTCG + qualified dividends
            "netInvestmentIncome": net_investment_income,
            "magiNiit": magi_niit,
            "ltcgAt0": ltcg_at_0,
            "ltcgAt15": ltcg_at_15,
            "ltcgAt20": ltcg_at_20,
            "ordinaryPortionTax": inv_ordinary_tax,
            "ltcgTax": ltcg_tax,
            "niit": niit,
            "waCapGainsTax": wa_capgains_tax,
            "totalInvestmentTax": investment_tax,
            "effectiveRate": inv_eff,
        },
        # ESPP / RSU share sales. Amounts feed the investment section above; zero by default.
        "dispositions": {
            "espp": {
                "sharesSold": i.espp_shares_sold,
                "qualifying": i.espp_qualifying,
                "ordinaryIncome": espp_ordinary,       # comp income at ordinary rates (not FICA, not NII)
                "capitalGain": espp_cap_gain,          # signed
                "capitalGainLongTerm": espp_cap_long,
            },
            "rsu": {
                "sharesSold": i.rsu_shares_sold,
                "capitalGain": rsu_cap_gain,           # signed
                "capitalGainLongTerm": i.rsu_long_term,
            },
            "addedOrdinaryIncome": disp_ordinary,
            "addedShortTermGain": disp_st_gain,
            "addedLongTermGain": disp_lt_gain,
        },
        # Safe harbor / estimated tax. Only actionable when prior-year tax is provided.
        "safeHarbor": {
            "priorYearFedTax": i.prior_year_fed_tax,
            "rate": i.safe_harbor_rate,
            "target": safe_harbor_target,              # 110% (or 100%) of prior-year federal tax
            "projectedWithholding": fed_withholding,   # wage liability + 22%-flat bonus withholding
            "shortfall": safe_harbor_shortfall,        # pay this in estimates to be safe-harbored
            "quarterly": safe_harbor_shortfall / 4,
            "currentTotalFed": current_total_fed,
            "ninetyPctCurrent": 0.90 * current_total_fed,   # caveated secondary (incomplete income)
        },
    }


__all__ = [
    "Bracket",
    "Inputs",
    "FilingStatus",
    "DEFAULT_FED_BRACKETS",
    "MFJ_FED_BRACKETS",
    "DEFAULT_CA_BRACKETS",
    "FILING_STATUS_OVERRIDES",
    "apply_brackets",
    "find_marginal",
    "calculate",
]
