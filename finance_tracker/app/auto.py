"""Auto financing module: lease vs buy vs finance a car.

Pure functions. No I/O. All monetary inputs are dollars; rates are annual
decimals (0.065 == 6.5%); term inputs are months unless named otherwise.

Reuses finance_math.mortgage_payment for loan amortization (identical formula
to a home loan) and the ×2400 money-factor↔APR helpers so the math stays
consistent in one place.
"""

from __future__ import annotations

import finance_math as fm

__all__ = [
    "DEFAULT_RETAINED_VALUE",
    "EV_RETAINED_VALUE",
    "DEFAULT_HORIZONS",
    "lease_payment",
    "finance_payment",
    "tco_compare",
    "twenty_four_ten_check",
    "calculate",
]

# ---------- Module-level defaults (editable via /api/defaults) ----------

# Retained-value fractions by vehicle age (year → fraction of MSRP remaining).
# Source: industry average ICE vehicle; luxury/EV residuals vary significantly. [verify]
DEFAULT_RETAINED_VALUE: dict[int, float] = {
    1: 0.80,
    2: 0.69,
    3: 0.58,
    5: 0.40,
    7: 0.30,
    10: 0.20,
}

# EVs / luxury vehicles depreciate faster early (battery + tech obsolescence, incentives on
# new units, richer lease subvention). Steeper early drop than the ICE curve. [verify]
EV_RETAINED_VALUE: dict[int, float] = {
    1: 0.70,
    2: 0.56,
    3: 0.45,
    5: 0.30,
    7: 0.21,
    10: 0.13,
}

DEFAULT_HORIZONS: list[int] = [3, 5, 10]


# ---------- Private helpers ----------

def _resolve_money_factor(
    money_factor: float | None,
    apr: float | None,
) -> tuple[float, float]:
    """Return (money_factor, equivalent_apr_decimal). Exactly one must be provided."""
    if money_factor is not None and apr is not None:
        raise ValueError("Provide money_factor OR apr, not both.")
    if money_factor is None and apr is None:
        raise ValueError("Provide either money_factor or apr.")
    if money_factor is not None:
        return money_factor, fm.money_factor_to_apr(money_factor)
    mf = fm.apr_to_money_factor(apr)  # type: ignore[arg-type]
    return mf, float(apr)  # type: ignore[arg-type]


def _interpolate_retained(year: int | float, curve: dict[int, float]) -> float:
    """Linear interpolation/extrapolation of retained-value fraction at `year`."""
    keys = sorted(curve)
    if year <= keys[0]:
        return curve[keys[0]]
    if year >= keys[-1]:
        return curve[keys[-1]]
    for lo, hi in zip(keys, keys[1:]):
        if lo <= year <= hi:
            frac = (year - lo) / (hi - lo)
            return curve[lo] + frac * (curve[hi] - curve[lo])
    return curve[keys[-1]]


def _maintenance_at_age(age_years: int | float, base: float, increment: float) -> float:
    """Annual maintenance cost at vehicle age, growing linearly with age."""
    return base + max(0.0, (age_years - 1)) * increment


# ---------- 1. Lease payment ----------

def lease_payment(
    cap_cost: float,
    cap_cost_reduction: float,
    residual_value: float,
    term_months: int,
    sales_tax_rate: float,
    *,
    money_factor: float | None = None,
    apr: float | None = None,
    acquisition_fee: float = 0.0,
    disposition_fee: float = 0.0,
    drive_off_fees: float = 0.0,
    allowed_miles_per_year: int = 12_000,
    actual_miles_per_year: int = 12_000,
    overage_rate: float = 0.25,
) -> dict:
    """Monthly lease payment and full lease economics.

    Industry-standard formula:
        adjusted_cap_cost = cap_cost - cap_cost_reduction + acquisition_fee
        dep_fee           = (adjusted_cap_cost - residual_value) / term_months
        rent_charge       = (adjusted_cap_cost + residual_value) × money_factor
        monthly_payment   = (dep_fee + rent_charge) × (1 + sales_tax_rate)

    The rent charge is computed on cap_cost + residual (not a declining
    balance), so it never falls to zero — the structural reason leasing is
    more expensive over the long run for the same vehicle class.

    Args:
        cap_cost:               Negotiated vehicle price before reductions.
        cap_cost_reduction:     Down payment + trade-in equity + rebates applied
                                to reduce the cap cost.
        residual_value:         Predicted vehicle value at lease end (dollars).
        term_months:            Lease duration in months.
        sales_tax_rate:         Sales/use tax as a decimal (e.g. 0.0825 for 8.25%).
        money_factor:           Lease money factor. Provide this OR apr, not both.
        apr:                    Equivalent annual rate as decimal. Provide this OR
                                money_factor, not both.
        acquisition_fee:        Bank/dealer origination fee; rolled into the
                                adjusted cap cost (standard dealer treatment).
        disposition_fee:        End-of-lease vehicle-return fee, paid at termination.
        drive_off_fees:         Doc fees, registration, etc. paid at signing but
                                NOT rolled into the cap cost.
        allowed_miles_per_year: Miles included in the lease contract per year.
        actual_miles_per_year:  Expected miles driven per year.
        overage_rate:           Penalty per mile over the allowance (dollars/mile).

    Returns:
        Dict with camelCase keys:
            monthlyPayment, depreciationFee, rentCharge, equivalentApr,
            dueAtSigning, totalLeaseCost, projectedMileagePenalty.
    """
    mf, eq_apr = _resolve_money_factor(money_factor, apr)

    adjusted_cap_cost = cap_cost - cap_cost_reduction + acquisition_fee
    dep_fee = (adjusted_cap_cost - residual_value) / term_months
    rent_charge = (adjusted_cap_cost + residual_value) * mf
    base = dep_fee + rent_charge
    monthly = base * (1.0 + sales_tax_rate)

    # Due at signing: cap-cost reduction (cash/trade down) + first month's payment
    # + drive-off fees. The cap-cost reduction lowers the monthly payment but is
    # still real money out of pocket — it MUST be counted, or a lease with a big
    # down payment looks artificially cheaper than buying and biases the TCO
    # comparison toward leasing. (Treat cap_cost_reduction as cash/trade; folding
    # manufacturer rebates in here slightly overstates cash — the safe direction
    # for a buy-vs-lease decision.) Acquisition fee is already in the cap cost.
    due_at_signing = cap_cost_reduction + monthly + drive_off_fees

    # Total out-of-pocket over the lease term (down + all monthly payments + overheads)
    total_lease_cost = cap_cost_reduction + monthly * term_months + drive_off_fees + disposition_fee

    # Projected mileage penalty (assessed at lease end)
    extra_miles = max(0.0, (actual_miles_per_year - allowed_miles_per_year) * (term_months / 12.0))
    projected_mileage_penalty = extra_miles * overage_rate

    return {
        "monthlyPayment": round(monthly, 2),
        "depreciationFee": round(dep_fee, 2),
        "rentCharge": round(rent_charge, 2),
        "equivalentApr": round(eq_apr, 6),
        "dueAtSigning": round(due_at_signing, 2),
        "totalLeaseCost": round(total_lease_cost + projected_mileage_penalty, 2),
        "projectedMileagePenalty": round(projected_mileage_penalty, 2),
    }


# ---------- 2. Finance payment ----------

def finance_payment(
    price: float,
    annual_rate: float,
    term_months: int,
    *,
    down_payment: float = 0.0,
    trade_in_value: float = 0.0,
    trade_in_payoff: float = 0.0,
    sales_tax_rate: float = 0.0,
    fees: float = 0.0,
) -> dict:
    """Standard auto loan amortization via finance_math.mortgage_payment.

    Amount financed:
        amount_financed = price + price*sales_tax_rate + fees
                          - down_payment
                          - (trade_in_value - trade_in_payoff)

    Delegates to finance_math.mortgage_payment — identical formula to a home
    loan (M = P·i(1+i)^n / ((1+i)^n − 1)). Do not reimplement here.

    Args:
        price:           Vehicle purchase price (pre-tax).
        annual_rate:     Loan APR as a decimal.
        term_months:     Loan term in months.
        down_payment:    Cash down payment applied at purchase.
        trade_in_value:  Dealer appraised trade-in value.
        trade_in_payoff: Remaining balance owed on the trade-in vehicle.
                         Negative net trade-in (underwater) increases amount financed.
        sales_tax_rate:  Sales tax rate as a decimal; applied to vehicle price.
        fees:            Dealer/documentation/title fees rolled into the loan.

    Returns:
        Dict with camelCase keys: monthlyPayment, amountFinanced,
        totalInterest, totalPaid.
        Note: totalPaid includes the down_payment (total cash out-of-pocket).
    """
    net_trade = trade_in_value - trade_in_payoff
    amount_financed = max(
        0.0,
        price + price * sales_tax_rate + fees - down_payment - net_trade,
    )

    # Round the monthly payment first, then derive the totals from it so the
    # displayed numbers reconcile (monthlyPayment * term == totalPaid - down).
    monthly = round(fm.mortgage_payment(amount_financed, annual_rate, term_months), 2)
    total_financed_paid = monthly * term_months
    interest = total_financed_paid - amount_financed
    total_paid = total_financed_paid + down_payment

    return {
        "monthlyPayment": monthly,
        "amountFinanced": round(amount_financed, 2),
        "totalInterest": round(interest, 2),
        "totalPaid": round(total_paid, 2),
    }


# ---------- 3. TCO compare ----------

def tco_compare(
    msrp: float,
    lease_monthly: float,
    lease_term_months: int,
    lease_due_at_signing: float,
    finance_monthly: float,
    finance_amount: float,
    finance_rate: float,
    finance_term_months: int,
    finance_down: float = 0.0,
    cash_price: float | None = None,
    annual_insurance: float = 1_200.0,
    annual_fuel: float = 0.0,
    annual_maintenance_base: float = 800.0,
    annual_maintenance_increment: float = 100.0,
    investment_rate: float = 0.07,
    retained_value: dict[int, float] | None = None,
    horizons: list[int] | None = None,
) -> dict:
    """3/5/10-year total cost of ownership for three strategies.

    Strategies modelled:
        lease   — perpetual: re-lease same vehicle class at each cycle end;
                  accumulates no equity.
        finance — finance once, hold the vehicle; loan paid off by term end,
                  then zero payment years until horizon.
        cash    — pay full price upfront; full equity from day one, but
                  highest opportunity cost on initial capital.

    Per-strategy output at each horizon year:
        totalPaid       — all cash outflows (vehicle + operating costs).
        opportunityCost — compound return foregone on the initial capital
                          outlay (lease_due_at_signing / finance_down /
                          cash_price) at the investment_rate.
        carEquity       — residual vehicle value minus remaining loan balance.
        netCost         — totalPaid + opportunityCost - carEquity.

    crossoverYear is the first year where finance netCost ≤ lease netCost,
    i.e., when owning-and-holding becomes less expensive than perpetual leasing.

    Maintenance grows with vehicle age for finance/cash; lease cars are always
    near-new so their maintenance is held at annual_maintenance_base.

    Args:
        msrp:                         MSRP used as the depreciation base.
        lease_monthly:                Monthly payment for the lease scenario.
        lease_term_months:            Lease cycle length (typically 24, 36, 48).
        lease_due_at_signing:         Total due at each lease signing.
        finance_monthly:              Monthly loan payment.
        finance_amount:               Loan principal (amount_financed).
        finance_rate:                 Annual loan rate as a decimal.
        finance_term_months:          Loan term in months.
        finance_down:                 Down payment (initial capital outlay).
        cash_price:                   All-in purchase price for cash buyer
                                      (defaults to msrp).
        annual_insurance:             Annual full-coverage insurance (all strategies).
        annual_fuel:                  Annual fuel cost (all strategies).
        annual_maintenance_base:      Year-1 annual maintenance.
        annual_maintenance_increment: Added maintenance per additional year of age.
        investment_rate:              Annual investment return rate for opportunity
                                      cost calculation.
        retained_value:               Depreciation curve override {age_year: fraction}.
        horizons:                     Evaluation years (default [3, 5, 10]).

    Returns:
        Dict with keys lease, finance, cash (each a dict keyed by str(horizon_year))
        and crossoverYear (int or None).
    """
    curve = retained_value if retained_value is not None else DEFAULT_RETAINED_VALUE
    hrs = sorted(set(horizons if horizons is not None else DEFAULT_HORIZONS))
    max_year = max(hrs)
    effective_cash_price = cash_price if cash_price is not None else msrp
    cycle_years = lease_term_months / 12.0

    # Running cumulative cash outflows; seeded with initial lump-sum at time 0.
    lease_paid = float(lease_due_at_signing)
    lease_signings_done = 0   # renewal signings beyond the seed (handles non-integer-year terms)
    finance_paid = float(finance_down)
    cash_paid = float(effective_cash_price)

    results: dict[str, dict] = {"lease": {}, "finance": {}, "cash": {}}
    crossover_year: int | None = None

    for year in range(1, max_year + 1):
        # --- Lease: re-sign at the end of each cycle (handles non-integer-year terms) ---
        # The seed (time 0) is the first signing; renewals occur at cycle_years, 2×cycle_years,…
        # A signing at elapsed k×cycle lands in loop-year floor(k×cycle)+1, so the cumulative
        # count by the end of `year` is floor((year − ε) / cycle_years). For integer cycles this
        # matches the old "renew at year 1+cycle, 1+2·cycle, …"; for e.g. a 30-month (2.5-yr)
        # lease it now correctly catches the 2.5- and 7.5-year renewals the old modulo missed.
        target_signings = int((year - 1e-9) / cycle_years) if cycle_years > 0 else 0
        if target_signings > lease_signings_done:
            lease_paid += lease_due_at_signing * (target_signings - lease_signings_done)
            lease_signings_done = target_signings
        lease_paid += lease_monthly * 12.0
        # Lease car is always near-new → maintenance stays at base rate
        lease_paid += annual_insurance + annual_fuel + annual_maintenance_base

        # --- Finance: loan payments drop to zero after payoff ---
        months_end = year * 12
        months_start = (year - 1) * 12
        if months_end <= finance_term_months:
            finance_paid += finance_monthly * 12.0
        elif months_start < finance_term_months:
            # Loan paid off partway through this year
            remaining_months = finance_term_months - months_start
            finance_paid += finance_monthly * remaining_months
        # (else: loan already paid off, no payment this year)
        finance_maint = _maintenance_at_age(year, annual_maintenance_base, annual_maintenance_increment)
        finance_paid += annual_insurance + annual_fuel + finance_maint

        # --- Cash: no loan payment ever; maintenance grows with age ---
        cash_maint = _maintenance_at_age(year, annual_maintenance_base, annual_maintenance_increment)
        cash_paid += annual_insurance + annual_fuel + cash_maint

        # --- Vehicle residual value at end of this year ---
        retained_frac = _interpolate_retained(year, curve)
        car_value = msrp * retained_frac

        # Finance equity = car value minus any remaining loan balance
        months_elapsed = min(year * 12, finance_term_months)
        loan_bal = fm.remaining_balance(
            finance_amount, finance_rate, finance_term_months, months_elapsed
        )
        finance_equity = max(0.0, car_value - loan_bal)

        # --- Opportunity cost: compound return on initial capital outlay ---
        opp_lease = fm.future_value_lump(lease_due_at_signing, investment_rate, year) - lease_due_at_signing
        opp_finance = fm.future_value_lump(finance_down, investment_rate, year) - finance_down
        opp_cash = fm.future_value_lump(effective_cash_price, investment_rate, year) - effective_cash_price

        # --- Net costs ---
        lease_net = lease_paid + opp_lease          # no equity
        finance_net = finance_paid + opp_finance - finance_equity
        cash_net = cash_paid + opp_cash - car_value

        # Crossover: first year buying beats perpetual leasing
        if crossover_year is None and finance_net <= lease_net:
            crossover_year = year

        if year in hrs:
            results["lease"][str(year)] = {
                "year": year,
                "totalPaid": round(lease_paid, 2),
                "opportunityCost": round(opp_lease, 2),
                "carEquity": 0.0,
                "netCost": round(lease_net, 2),
            }
            results["finance"][str(year)] = {
                "year": year,
                "totalPaid": round(finance_paid, 2),
                "opportunityCost": round(opp_finance, 2),
                "carEquity": round(finance_equity, 2),
                "netCost": round(finance_net, 2),
            }
            results["cash"][str(year)] = {
                "year": year,
                "totalPaid": round(cash_paid, 2),
                "opportunityCost": round(opp_cash, 2),
                "carEquity": round(car_value, 2),
                "netCost": round(cash_net, 2),
            }

    return {
        "lease": results["lease"],
        "finance": results["finance"],
        "cash": results["cash"],
        "crossoverYear": crossover_year,
    }


# ---------- 4. 20/4/10 affordability check ----------

def twenty_four_ten_check(
    gross_monthly_income: float,
    down_payment: float,
    vehicle_price: float,
    loan_term_months: int,
    monthly_payment: float,
    monthly_insurance: float,
    monthly_fuel: float = 0.0,
    monthly_maintenance: float = 0.0,
    include_fuel_maint: bool = False,
    *,
    annual_rate: float = 0.065,
) -> dict:
    """20/4/10 affordability guardrail for vehicle purchases.

    Three independent rules (each must pass for overall pass):
        20% down  — down_payment >= 20% of vehicle_price
        4 years   — loan_term_months <= 48
        10% gross — monthly transportation costs <= 10% of gross monthly income

    Transportation costs always include payment + insurance.  Set
    include_fuel_maint=True to also count fuel and maintenance (a stricter
    interpretation recommended for tight budgets).

    maxAffordablePrice is solved backwards from the 10% budget cap:
        max_payment    = 0.10 × gross_monthly_income − monthly_insurance
                         [− monthly_fuel − monthly_maintenance]
        max_financed   = max_payment / payment_factor(annual_rate, 48)
        max_price      = max_financed / (1 − 0.20)   # 20% down assumed

    Args:
        gross_monthly_income: Monthly gross income (pre-tax).
        down_payment:         Actual cash down payment.
        vehicle_price:        Vehicle purchase price (before tax/fees).
        loan_term_months:     Actual loan term in months.
        monthly_payment:      Actual monthly loan payment.
        monthly_insurance:    Monthly full-coverage insurance cost.
        monthly_fuel:         Monthly fuel/charging cost.
        monthly_maintenance:  Monthly maintenance budget.
        include_fuel_maint:   Include fuel + maintenance in the 10% budget test.
        annual_rate:          Rate used to back-solve maxAffordablePrice (default 6.5%).

    Returns:
        Dict with camelCase keys: downOk, termOk, budgetOk, passes,
        pctOfGross, maxAffordablePrice.
    """
    down_ok: bool = down_payment >= 0.20 * vehicle_price
    term_ok: bool = loan_term_months <= 48

    monthly_transport = monthly_payment + monthly_insurance
    if include_fuel_maint:
        monthly_transport += monthly_fuel + monthly_maintenance

    pct_of_gross = (
        monthly_transport / gross_monthly_income if gross_monthly_income > 0 else float("inf")
    )
    budget_ok: bool = pct_of_gross <= 0.10

    # Back-solve max affordable price under the 10% constraint
    budget_ceiling = 0.10 * gross_monthly_income
    max_payment_allowed = budget_ceiling - monthly_insurance
    if include_fuel_maint:
        max_payment_allowed -= monthly_fuel + monthly_maintenance
    max_payment_allowed = max(0.0, max_payment_allowed)

    if max_payment_allowed > 0:
        max_financed = fm.loan_principal_from_payment(max_payment_allowed, annual_rate, 48)
    else:
        max_financed = 0.0
    max_affordable_price = max_financed / 0.80  # assumes 20% down

    return {
        "downOk": down_ok,
        "termOk": term_ok,
        "budgetOk": budget_ok,
        "passes": down_ok and term_ok and budget_ok,
        "pctOfGross": round(pct_of_gross, 4),
        "maxAffordablePrice": round(max_affordable_price, 2),
    }


# ---------- 5. Bundled entry point ----------

def _to_camel(snake: str) -> str:
    """snake_case -> camelCase (for echoing the inputs dict consistently)."""
    head, *rest = snake.split("_")
    return head + "".join(w[:1].upper() + w[1:] for w in rest)


def calculate(inputs: dict) -> dict:
    """Full lease-vs-buy analysis bundled into a single response dict.

    Calls lease_payment, finance_payment, tco_compare, and
    twenty_four_ten_check with the provided inputs and returns all results
    plus a notes list. Designed to be called from POST /api/auto with a
    Pydantic-validated body converted to a plain dict.

    Required inputs keys:
        msrp            (float)  — MSRP; used as depreciation base.
        money_factor    (float)  — lease money factor  ─┐ provide one
        apr             (float)  — equiv. APR decimal  ─┘
        gross_monthly_income (float)

    Optional keys (with defaults):
        cap_cost               (float, default msrp)
        cap_cost_reduction     (float, default 0)
        residual_pct           (float, default 0.58) — fraction of MSRP
        residual_value         (float) — overrides residual_pct if provided
        term_months            (int,   default 36)
        sales_tax_rate         (float, default 0.0825)
        acquisition_fee        (float, default 0)
        disposition_fee        (float, default 0)
        drive_off_fees         (float, default 0)
        allowed_miles_per_year (int,   default 12_000)
        actual_miles_per_year  (int,   default 12_000)
        overage_rate           (float, default 0.25)
        finance_annual_rate    (float, default 0.065)
        finance_term_months    (int,   default 60)
        finance_down           (float, default 0)
        finance_trade_in_value (float, default 0)
        finance_trade_in_payoff(float, default 0)
        finance_sales_tax_rate (float, default sales_tax_rate)
        finance_fees           (float, default 0)
        annual_insurance       (float, default 1_200)
        annual_fuel            (float, default 0)
        annual_maintenance_base(float, default 800)
        annual_maintenance_increment (float, default 100)
        investment_rate        (float, default 0.07)
        monthly_insurance      (float, default annual_insurance/12)
        monthly_fuel           (float, default annual_fuel/12)
        monthly_maintenance    (float, default annual_maintenance_base/12)
        include_fuel_maint     (bool,  default False)
        retained_value         (dict,  optional)
        horizons               (list,  optional)

    Returns:
        {inputs, lease, finance, tco, affordability, notes}
    """
    g = inputs.get
    msrp: float = float(g("msrp", 0))
    cap_cost: float = float(g("cap_cost", msrp))
    cap_cost_reduction: float = float(g("cap_cost_reduction", 0))
    term_months: int = int(g("term_months", 36))
    sales_tax_rate: float = float(g("sales_tax_rate", 0.0825))

    # Residual: accept explicit dollars or a fraction of MSRP
    if "residual_value" in inputs:
        residual_value = float(inputs["residual_value"])
    else:
        residual_pct = float(g("residual_pct", 0.58))
        residual_value = msrp * residual_pct

    # Money factor / APR — mutually exclusive
    money_factor: float | None = inputs.get("money_factor")
    apr: float | None = inputs.get("apr")

    acq_fee: float = float(g("acquisition_fee", 0))
    disp_fee: float = float(g("disposition_fee", 0))
    drive_off: float = float(g("drive_off_fees", 0))
    allowed_mi: int = int(g("allowed_miles_per_year", 12_000))
    actual_mi: int = int(g("actual_miles_per_year", 12_000))
    overage_rate: float = float(g("overage_rate", 0.25))

    finance_rate: float = float(g("finance_annual_rate", 0.065))
    finance_term: int = int(g("finance_term_months", 60))
    finance_down: float = float(g("finance_down", 0))
    finance_trade_in: float = float(g("finance_trade_in_value", 0))
    finance_payoff: float = float(g("finance_trade_in_payoff", 0))
    finance_ftax: float = float(g("finance_sales_tax_rate", sales_tax_rate))
    finance_fees: float = float(g("finance_fees", 0))

    annual_ins: float = float(g("annual_insurance", 1_200))
    annual_fuel: float = float(g("annual_fuel", 0))
    maint_base: float = float(g("annual_maintenance_base", 800))
    maint_incr: float = float(g("annual_maintenance_increment", 100))
    inv_rate: float = float(g("investment_rate", 0.07))

    gross_monthly: float = float(g("gross_monthly_income", 0))
    monthly_ins: float = float(g("monthly_insurance", annual_ins / 12))
    monthly_fuel: float = float(g("monthly_fuel", annual_fuel / 12))
    monthly_maint: float = float(g("monthly_maintenance", maint_base / 12))
    incl_fm: bool = bool(g("include_fuel_maint", False))

    retained = inputs.get("retained_value")
    if retained is None and inputs.get("ev"):
        retained = EV_RETAINED_VALUE   # steeper EV/luxury depreciation curve
    horizons = inputs.get("horizons")

    # --- Compute lease ---
    lease = lease_payment(
        cap_cost=cap_cost,
        cap_cost_reduction=cap_cost_reduction,
        residual_value=residual_value,
        term_months=term_months,
        sales_tax_rate=sales_tax_rate,
        money_factor=money_factor,
        apr=apr,
        acquisition_fee=acq_fee,
        disposition_fee=disp_fee,
        drive_off_fees=drive_off,
        allowed_miles_per_year=allowed_mi,
        actual_miles_per_year=actual_mi,
        overage_rate=overage_rate,
    )

    # --- Compute finance ---
    finance = finance_payment(
        price=cap_cost,
        annual_rate=finance_rate,
        term_months=finance_term,
        down_payment=finance_down,
        trade_in_value=finance_trade_in,
        trade_in_payoff=finance_payoff,
        sales_tax_rate=finance_ftax,
        fees=finance_fees,
    )

    # --- Compute TCO ---
    tco = tco_compare(
        msrp=msrp,
        lease_monthly=lease["monthlyPayment"],
        lease_term_months=term_months,
        lease_due_at_signing=lease["dueAtSigning"],
        finance_monthly=finance["monthlyPayment"],
        finance_amount=finance["amountFinanced"],
        finance_rate=finance_rate,
        finance_term_months=finance_term,
        finance_down=finance_down,
        cash_price=cap_cost,
        annual_insurance=annual_ins,
        annual_fuel=annual_fuel,
        annual_maintenance_base=maint_base,
        annual_maintenance_increment=maint_incr,
        investment_rate=inv_rate,
        retained_value=retained,
        horizons=horizons,
    )

    # --- Affordability check ---
    affordability = twenty_four_ten_check(
        gross_monthly_income=gross_monthly,
        down_payment=finance_down,
        vehicle_price=cap_cost,
        loan_term_months=finance_term,
        monthly_payment=finance["monthlyPayment"],
        monthly_insurance=monthly_ins,
        monthly_fuel=monthly_fuel,
        monthly_maintenance=monthly_maint,
        include_fuel_maint=incl_fm,
        annual_rate=finance_rate,
    ) if gross_monthly > 0 else {}

    # --- Notes ---
    notes: list[str] = []
    if tco.get("crossoverYear") is not None:
        notes.append(
            f"Buying beats perpetual leasing at year {tco['crossoverYear']} "
            f"(net cost basis)."
        )
    if lease["equivalentApr"] > 0.08:
        notes.append(
            f"Lease APR equivalent is {lease['equivalentApr']*100:.2f}% — "
            "consider shopping for a lower money factor."
        )
    if actual_mi > allowed_mi:
        notes.append(
            f"Mileage overage projected: "
            f"{(actual_mi - allowed_mi) * (term_months / 12):.0f} extra miles "
            f"at ${overage_rate}/mi = ${lease['projectedMileagePenalty']:,.0f}."
        )
    if affordability and not affordability.get("passes"):
        failed = [k for k in ("downOk", "termOk", "budgetOk") if not affordability.get(k)]
        notes.append(f"20/4/10 check failed: {', '.join(failed)}.")

    return {
        "inputs": {_to_camel(k): v for k, v in inputs.items()},
        "lease": lease,
        "finance": finance,
        "tco": tco,
        "affordability": affordability,
        "notes": notes,
    }
