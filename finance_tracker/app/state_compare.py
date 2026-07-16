"""Washington-vs-Texas cost-of-state comparison engine (TODO-224, DEC-019).

Pure functions. No I/O. Same life computed under both states: neither taxes
wages, so the comparison is payroll deductions (WA PFML + WA Cares), sales
tax on taxable spending, property tax on a target home, WA's capital-gains
excise on long-term gains, and two optional vehicle extras (Seattle RTA car
tabs, state gas-tax difference).

All parameters live in PARAMS (per-unit rates, caps, thresholds) so the
yearly refresh is a constants edit, and every call may override any of them.

2026 parameter sources (verified 2026-07-08; see TODO-224 for URLs):
  - WA PFML: 1.13% total premium, 71.43% employee share, wages capped at the
    Social Security wage base ($184,500 in 2026). esd.wa.gov news release.
  - WA Cares: 0.58% employee-paid, NO wage cap; the private-insurance opt-out
    closed 2022, so a new WA resident pays it. wacaresfund.wa.gov.
  - WA capital-gains excise: 7% of WA long-term gains above the standard
    deduction ($278,000 for TY2025 — the 2026 inflation adjustment was not
    yet published as of 2026-07; parameterized) plus 2.9% more (9.9% total)
    on taxable gain above $1M (SB 5813, retroactive to 2025-01-01; the $1M
    tier is not indexed). Short-term gains, RSU vest income, ESPP discount
    income, and retirement-account assets are NOT taxed. dor.wa.gov.
  - Sales tax (combined state+local, mid-2026): Seattle 10.55%, WA average
    9.57%, Austin/Dallas 8.25% (statutory max), TX average 8.20%. Both
    states exempt most unprepared groceries — pass TAXABLE spend only.
  - Property tax (effective, owner-occupied): WA avg 0.75%, Seattle ~0.99%;
    TX avg 1.40%, Austin ~1.65%, Dallas ~1.72% (post-homestead-exemption
    effective rates; TX Prop 13 (2025) exempts $140k of school-district
    value, which these effective rates already reflect).
  - Seattle-metro RTA car tabs: 1.1% of depreciated vehicle value annually
    (Sound Transit district only — zero elsewhere in WA and in TX).
  - Gas tax: WA $0.565/gal (from 2026-07-01, ~2%/yr auto-indexed) vs TX
    $0.20/gal (frozen since 1991).
  - NOT modeled: WA's 9.9% income tax on >$1M Washington-source income
    (ESSB 6346) — effective 2028-01-01 and under legal challenge; surfaced
    as a note when wages + gains exceed its deduction.
"""

from __future__ import annotations

__all__ = ["compare_states", "PARAMS", "WA_REGIONS", "TX_REGIONS"]

PARAMS: dict = {
    "pfmlTotalRate": 0.0113,
    "pfmlEmployeeShare": 0.7143,
    "pfmlWageCap": 184_500.0,
    "waCaresRate": 0.0058,
    "waCapGainsDeduction": 278_000.0,   # TY2025 figure (2026 unpublished) — refresh when DOR posts it
    "waCapGainsRate": 0.07,
    "waCapGainsSurchargeRate": 0.029,
    "waCapGainsSurchargeThreshold": 1_000_000.0,
    "salesRates": {
        "seattle": 0.1055, "wa_avg": 0.0957,
        "austin": 0.0825, "dallas": 0.0825, "tx_avg": 0.0820,
    },
    "propertyRates": {
        "seattle": 0.0099, "wa_avg": 0.0075,
        "austin": 0.0165, "dallas": 0.0172, "tx_avg": 0.0140,
    },
    "rtaCarTabRate": 0.011,
    "gasTaxPerGallon": {"wa": 0.565, "tx": 0.20},
    "waFutureIncomeTaxDeduction": 1_000_000.0,   # ESSB 6346, eff. 2028 — note only
}

WA_REGIONS = ("seattle", "wa_avg")
TX_REGIONS = ("austin", "dallas", "tx_avg")

_REGION_LABELS = {
    "seattle": "Seattle metro", "wa_avg": "WA average",
    "austin": "Austin", "dallas": "Dallas", "tx_avg": "TX average",
}


def _wa_capital_gains_tax(lt_gains: float, p: dict) -> float:
    """WA capital-gains excise on LONG-TERM gains: 7% above the standard
    deduction, 9.9% total on the taxable portion above the $1M tier."""
    taxable = max(0.0, lt_gains - p["waCapGainsDeduction"])
    base = p["waCapGainsRate"] * min(taxable, p["waCapGainsSurchargeThreshold"])
    surcharge = (p["waCapGainsRate"] + p["waCapGainsSurchargeRate"]) * max(
        0.0, taxable - p["waCapGainsSurchargeThreshold"])
    return base + surcharge


def compare_states(
    gross_wages: float,
    lt_capital_gains: float = 0.0,
    taxable_spend_monthly: float = 0.0,
    home_value: float = 0.0,
    wa_region: str = "seattle",
    tx_region: str = "austin",
    vehicle_value: float = 0.0,
    gallons_per_year: float = 0.0,
    overrides: dict | None = None,
) -> dict:
    """Annual cost of the same life in TX vs WA. Returns per-line tx/wa
    dollars (camelCase, JSON-ready), totals, and the WA-minus-TX delta
    (positive = WA costs more). Raises ValueError on bad input."""
    for name, v in (("gross_wages", gross_wages), ("lt_capital_gains", lt_capital_gains),
                    ("taxable_spend_monthly", taxable_spend_monthly), ("home_value", home_value),
                    ("vehicle_value", vehicle_value), ("gallons_per_year", gallons_per_year)):
        if not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"{name} must be a number >= 0, got {v!r}")
    if wa_region not in WA_REGIONS:
        raise ValueError(f"wa_region must be one of {WA_REGIONS}, got {wa_region!r}")
    if tx_region not in TX_REGIONS:
        raise ValueError(f"tx_region must be one of {TX_REGIONS}, got {tx_region!r}")

    p = dict(PARAMS)
    if overrides:
        for k, v in overrides.items():
            if k not in PARAMS:
                raise ValueError(f"unknown parameter override {k!r}")
            p[k] = v

    pfml = min(gross_wages, p["pfmlWageCap"]) * p["pfmlTotalRate"] * p["pfmlEmployeeShare"]
    wa_cares = gross_wages * p["waCaresRate"]
    spend_yr = taxable_spend_monthly * 12.0
    sales_wa = spend_yr * p["salesRates"][wa_region]
    sales_tx = spend_yr * p["salesRates"][tx_region]
    prop_wa = home_value * p["propertyRates"][wa_region]
    prop_tx = home_value * p["propertyRates"][tx_region]
    cap_gains_wa = _wa_capital_gains_tax(lt_capital_gains, p)
    rta = vehicle_value * p["rtaCarTabRate"] if wa_region == "seattle" else 0.0
    gas_wa = gallons_per_year * p["gasTaxPerGallon"]["wa"]
    gas_tx = gallons_per_year * p["gasTaxPerGallon"]["tx"]

    lines = [
        {"key": "wageTax", "label": "Wage income tax", "tx": 0.0, "wa": 0.0},
        {"key": "payroll", "label": "State payroll deductions", "tx": 0.0,
         "wa": pfml + wa_cares, "detail": {"pfml": pfml, "waCares": wa_cares}},
        {"key": "salesTax", "label": "Sales tax on taxable spend", "tx": sales_tx, "wa": sales_wa,
         "detail": {"txRate": p["salesRates"][tx_region], "waRate": p["salesRates"][wa_region]}},
        {"key": "propertyTax", "label": "Property tax", "tx": prop_tx, "wa": prop_wa,
         "detail": {"txRate": p["propertyRates"][tx_region], "waRate": p["propertyRates"][wa_region]}},
        {"key": "capitalGains", "label": "Capital-gains tax (long-term)", "tx": 0.0, "wa": cap_gains_wa},
    ]
    if vehicle_value > 0:
        lines.append({"key": "carTabs", "label": "Car tabs (Seattle RTA excise)", "tx": 0.0, "wa": rta})
    if gallons_per_year > 0:
        lines.append({"key": "gasTax", "label": "State gas tax", "tx": gas_tx, "wa": gas_wa})

    for ln in lines:
        ln["tx"] = round(ln["tx"], 2)
        ln["wa"] = round(ln["wa"], 2)
        ln["delta"] = round(ln["wa"] - ln["tx"], 2)

    total_tx = round(sum(ln["tx"] for ln in lines), 2)
    total_wa = round(sum(ln["wa"] for ln in lines), 2)
    delta = round(total_wa - total_tx, 2)

    notes = [
        "Neither state taxes wage income; the differences above are the whole story.",
        "WA capital-gains standard deduction uses the 2025 figure ($278,000) — "
        "the 2026 inflation adjustment had not been published when these "
        "parameters were verified (2026-07).",
        "Sales tax applies to TAXABLE spend only — both states exempt most "
        "unprepared groceries; rent and most services are also untaxed.",
    ]
    if gross_wages + lt_capital_gains > p["waFutureIncomeTaxDeduction"]:
        notes.append(
            "Heads-up: WA enacted a 9.9% tax on Washington-source income above "
            "$1M effective 2028 (ESSB 6346, under legal challenge) — not "
            "included in these numbers.")

    return {
        "inputs": {
            "grossWages": gross_wages, "ltCapitalGains": lt_capital_gains,
            "taxableSpendMonthly": taxable_spend_monthly, "homeValue": home_value,
            "waRegion": wa_region, "txRegion": tx_region,
            "waRegionLabel": _REGION_LABELS[wa_region], "txRegionLabel": _REGION_LABELS[tx_region],
            "vehicleValue": vehicle_value, "gallonsPerYear": gallons_per_year,
        },
        "lines": lines,
        "totalTx": total_tx,
        "totalWa": total_wa,
        "deltaWaMinusTx": delta,
        "deltaMonthly": round(delta / 12.0, 2),
        "notes": notes,
    }
