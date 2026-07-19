"""FastAPI server for the Income Tax Calculator.

Run locally:

    ./run.sh               # creates venv, installs deps, starts server
    # or manually:
    pip install -r requirements.txt
    uvicorn server:app --reload --port 8000

Then open http://localhost:8000
"""

import csv
import io
import json
import logging
import os
import re
import uuid
from contextlib import asynccontextmanager, closing
from datetime import date as _date
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import BaseModel, Field

try:  # Soft import (DEC-024): requirements.txt ships no PyYAML (DEC-021 --only-binary
    # posture keeps the add-on image dependency-free) — the production container has no
    # yaml module. Dev/sandbox envs typically have PyYAML installed. When absent, YAML
    # theme candidates are simply skipped (never a crash); JSON theme files always work
    # via the stdlib json module regardless of this import's outcome.
    import yaml
except ImportError:
    yaml = None

import affordability
import calculator as calc
import budgeting
import fire as _fire
import goals
import state_compare
import ventures
import investing
import auto
import scenarios as _scenarios
import tracking
import tracking_store

ROOT = Path(__file__).resolve().parent

_DISCLAIMER = (
    "This calculator provides estimates for educational purposes only. "
    "It is not financial, tax, investment, or legal advice. "
    "Consult a licensed CPA, CFP, or attorney before making financial decisions. "
    "Tax rates, contribution limits, and rules change annually — verify all figures "
    "with current IRS publications and applicable state authorities."
)

MAX_IMPORT_BYTES = int(os.environ.get("ACTUALS_MAX_IMPORT_BYTES", 25 * 1024 * 1024))

@asynccontextmanager
async def _lifespan(_app: "FastAPI"):
    """Create the actuals SQLite schema if absent (idempotent) on startup. Data lives ONLY in
    our own DB at tracking_store.resolve_db_path() — never Home Assistant's database (DEC-006)."""
    with closing(tracking_store.connect()) as conn:
        tracking_store.init_db(conn)
    yield


app = FastAPI(title="Income Tax Calculator", version="1.0", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Existing models (tax calculator)
# ---------------------------------------------------------------------------

class BracketModel(BaseModel):
    upper: float | None = None
    rate: float


class InputModel(BaseModel):
    # Compensation
    salary: float = 0
    # Pre-tax
    trad_401k: float = Field(0, alias="trad401k")
    hsa: float = 0
    employer_hsa: float = Field(0, alias="employerHsa")
    hsa_coverage: Literal["self", "family"] = Field("self", alias="hsaCoverage")
    # Pre-tax §125 health/dental/vision premiums (Fed/FICA/CA-exempt)
    medical: float = 0
    dental: float = 0
    vision: float = 0
    # Post-tax
    roth_401k: float = Field(0, alias="roth401k")
    ee_stock: float = Field(0, alias="eeStock")
    roth_ira: float = Field(0, alias="rothIra")
    # Non-cash taxable additions
    er_stock: float = Field(0, alias="erStock")
    gtli: float = 0
    # Jurisdiction
    state: str = "CA"
    filing_status: Literal["single", "mfj"] = Field("single", alias="filingStatus")
    # Federal
    fed_std_deduction: float = Field(16_100, alias="fedStd")
    fed_brackets: list[BracketModel] = Field(default_factory=list, alias="fedBrackets")
    # FICA
    ss_wage_base: float = Field(184_500, alias="ssBase")
    ss_rate: float = Field(0.062, alias="ssRate")
    medicare_rate: float = Field(0.0145, alias="medRate")
    addl_medicare_threshold: float = Field(200_000, alias="addlMedThresh")
    addl_medicare_rate: float = Field(0.009, alias="addlMedRate")
    # California
    ca_std_deduction: float = Field(5_540, alias="caStd")
    ca_brackets: list[BracketModel] = Field(default_factory=list, alias="caBrackets")
    ca_sdi_rate: float = Field(0.012, alias="caSdi")
    ca_mhst_threshold: float = Field(1_000_000, alias="caMhstThresh")
    ca_mhst_rate: float = Field(0.01, alias="caMhstRate")
    # Roth IRA phase-out
    roth_ira_limit: float = Field(7_500, alias="rothIraLimit")
    roth_ira_phase_in: float = Field(153_000, alias="rothIraPhaseIn")
    roth_ira_phase_out: float = Field(168_000, alias="rothIraPhaseOut")
    # Backdoor / mega-backdoor Roth and bonus
    backdoor_roth: bool = Field(False, alias="backdoorRoth")
    bonus: float = 0
    after_tax_401k: float = Field(0, alias="afterTax401k")
    # §415(c) annual-additions limit
    employer_401k_match: float = Field(0, alias="employer401kMatch")
    sec415c_limit: float = Field(72_000, alias="sec415cLimit")
    # Investment income (taxed at filing, isolated from take-home)
    long_term_gains: float = Field(0, alias="longTermGains")
    short_term_gains: float = Field(0, alias="shortTermGains")
    qualified_dividends: float = Field(0, alias="qualifiedDividends")
    ordinary_dividends: float = Field(0, alias="ordinaryDividends")
    taxable_interest: float = Field(0, alias="taxableInterest")
    ltcg_0pct_upper: float = Field(49_450, alias="ltcg0pctUpper")
    ltcg_15pct_upper: float = Field(545_500, alias="ltcg15pctUpper")
    niit_threshold: float = Field(200_000, alias="niitThreshold")
    # ESPP / RSU dispositions (share sales)
    espp_shares_sold: float = Field(0, alias="esppSharesSold")
    espp_purchase_price_per_share: float = Field(0, alias="esppPurchasePrice")
    espp_purchase_fmv_per_share: float = Field(0, alias="esppPurchaseFmv")
    espp_grant_fmv_per_share: float = Field(0, alias="esppGrantFmv")
    espp_sale_price_per_share: float = Field(0, alias="esppSalePrice")
    espp_qualifying: bool = Field(True, alias="esppQualifying")
    espp_disq_gain_long_term: bool = Field(False, alias="esppDisqLongTerm")
    rsu_shares_sold: float = Field(0, alias="rsuSharesSold")
    rsu_vest_fmv_per_share: float = Field(0, alias="rsuVestFmv")
    rsu_sale_price_per_share: float = Field(0, alias="rsuSalePrice")
    rsu_long_term: bool = Field(True, alias="rsuLongTerm")
    # Safe harbor / estimated tax
    prior_year_fed_tax: float = Field(0, alias="priorYearFedTax")
    safe_harbor_rate: float = Field(1.10, alias="safeHarborRate")

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Budgeting models
# ---------------------------------------------------------------------------

class BudgetModel(BaseModel):
    net_take_home: float = Field(0.0, alias="netTakeHome")
    pretax_401k: float = Field(0.0, alias="pretax401k")
    pretax_hsa: float = Field(0.0, alias="pretaxHsa")
    pretax_health_premiums: float = Field(0.0, alias="pretaxHealthPremiums")
    other_nontax_payroll: float = Field(0.0, alias="otherNontaxPayroll")
    post_tax_savings: float = Field(0.0, alias="postTaxSavings")
    framework: str = Field("fiftyThirtyTwenty", alias="framework")
    savings_rate: float | None = Field(None, alias="savingsRate")
    categories: dict[str, float] | None = Field(None, alias="categories")
    base_mode: Literal["after_tax", "take_home"] = Field("after_tax", alias="baseMode")
    gross_income: float | None = Field(None, alias="grossIncome")
    employer_match: float | None = Field(None, alias="employerMatch")

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Investing models
# ---------------------------------------------------------------------------

class DebtModel(BaseModel):
    balance: float = 0.0
    apr: float = 0.0


class EmployerMatchModel(BaseModel):
    pct_of_salary: float = Field(0.0, alias="pctOfSalary")
    match_rate: float = Field(0.0, alias="matchRate")

    model_config = {"populate_by_name": True}


class InvestModel(BaseModel):
    # Annual dollars to allocate through the savings waterfall
    amount: float = Field(0.0, alias="amount")

    # Profile identity / income
    age: int = Field(30, alias="age")
    state: Literal["TX", "CA", "WA", "none"] = Field("TX", alias="state")
    gross_income: float = Field(0.0, alias="grossIncome")
    monthly_essential_expenses: float = Field(0.0, alias="monthlyEssentialExpenses")

    # Current emergency-fund balance
    ef_balance: float = Field(0.0, alias="efBalance")

    # Debts: list of {balance, apr}
    debts: list[DebtModel] = Field(default_factory=list, alias="debts")

    # Employer match rule: {pctOfSalary, matchRate}
    employer_match: EmployerMatchModel = Field(
        default_factory=EmployerMatchModel, alias="employerMatch"
    )

    # Remaining annual contribution room
    k401_room: float = Field(24_500.0, alias="k401Room")
    hsa_room: float = Field(4_400.0, alias="hsaRoom")
    ira_room: float = Field(7_500.0, alias="iraRoom")
    aftertax_401k_room: float = Field(0.0, alias="aftertax401kRoom")

    # Eligibility / situational flags
    hsa_eligible: bool = Field(True, alias="hsaEligible")
    roth_magi_over_limit: bool = Field(False, alias="rothMagiOverLimit")
    pretax_ira_balance: float = Field(0.0, alias="pretaxIraBalance")
    mega_available: bool = Field(False, alias="megaAvailable")
    retire_state_no_tax: bool = Field(False, alias="retireStateNoTax")
    retire_marginal_rate: float = Field(0.22, alias="retireMarginalRate")
    retire_income: float = Field(0, alias="retireIncome")

    # EF / debt thresholds
    ef_starter_target: float = Field(1_000.0, alias="efStarterTarget")
    ef_months_target: int = Field(6, alias="efMonthsTarget")
    high_interest_threshold: float = Field(0.06, alias="highInterestThreshold")

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Auto models
# ---------------------------------------------------------------------------

class AutoModel(BaseModel):
    # MSRP / depreciation base (required for TCO)
    msrp: float = Field(0.0, alias="msrp")

    # Lease inputs
    cap_cost: float | None = Field(None, alias="capCost")
    cap_cost_reduction: float = Field(0.0, alias="capCostReduction")
    residual_pct: float = Field(0.58, alias="residualPct")
    residual_value: float | None = Field(None, alias="residualValue")
    term_months: int = Field(36, gt=0, alias="termMonths")
    sales_tax_rate: float = Field(0.0825, alias="salesTaxRate")
    # Provide moneyFactor OR apr (not both) — lease_payment raises ValueError if both/neither
    money_factor: float | None = Field(None, alias="moneyFactor")
    apr: float | None = Field(None, alias="apr")
    acquisition_fee: float = Field(0.0, alias="acquisitionFee")
    disposition_fee: float = Field(0.0, alias="dispositionFee")
    drive_off_fees: float = Field(0.0, alias="driveOffFees")
    allowed_miles_per_year: int = Field(12_000, alias="allowedMilesPerYear")
    actual_miles_per_year: int = Field(12_000, alias="actualMilesPerYear")
    overage_rate: float = Field(0.25, alias="overageRate")

    # Finance inputs
    finance_annual_rate: float = Field(0.065, alias="financeAnnualRate")
    finance_term_months: int = Field(60, gt=0, alias="financeTermMonths")
    finance_down: float = Field(0.0, alias="financeDown")
    finance_trade_in_value: float = Field(0.0, alias="financeTradeInValue")
    finance_trade_in_payoff: float = Field(0.0, alias="financeTradeInPayoff")
    finance_sales_tax_rate: float | None = Field(None, alias="financeSalesTaxRate")
    finance_fees: float = Field(0.0, alias="financeFees")

    # Operating costs
    annual_insurance: float = Field(1_200.0, alias="annualInsurance")
    annual_fuel: float = Field(0.0, alias="annualFuel")
    annual_maintenance_base: float = Field(800.0, alias="annualMaintenanceBase")
    annual_maintenance_increment: float = Field(100.0, alias="annualMaintenanceIncrement")
    investment_rate: float = Field(0.07, alias="investmentRate")

    # Affordability (20/4/10 check; skipped when gross_monthly_income == 0)
    gross_monthly_income: float = Field(0.0, alias="grossMonthlyIncome")
    monthly_insurance: float | None = Field(None, alias="monthlyInsurance")
    monthly_fuel: float | None = Field(None, alias="monthlyFuel")
    monthly_maintenance: float | None = Field(None, alias="monthlyMaintenance")
    include_fuel_maint: bool = Field(False, alias="includeFuelMaint")

    # TCO settings
    retained_value: dict[int, float] | None = Field(None, alias="retainedValue")
    ev: bool = Field(False, alias="ev")   # use steeper EV/luxury depreciation curve
    horizons: list[int] | None = Field(None, alias="horizons")

    model_config = {"populate_by_name": True}


class ProjectModel(BaseModel):
    # annual dollars added per account type, e.g. {"trad401k": 24500, "roth": 7500, ...}
    contributions: dict[str, float] = Field(default_factory=dict, alias="contributions")
    balances: dict[str, float] = Field(default_factory=dict, alias="balances")
    annual_return: float = Field(0.07, alias="annualReturn")
    years: int = Field(30, ge=0, le=80, alias="years")

    model_config = {"populate_by_name": True}


class NetWorthModel(BaseModel):
    current_net_worth: float = Field(0.0, alias="currentNetWorth")
    annual_contribution: float = Field(0.0, alias="annualContribution")
    years: int = Field(30, gt=0, le=80, alias="years")
    annual_return: float = Field(0.07, alias="returnRate")
    band: float = Field(0.02, ge=0, le=0.2, alias="band")
    targets: list[float] | None = Field(None, alias="targets")   # user-defined milestone $ targets

    model_config = {"populate_by_name": True}


class FireModel(BaseModel):
    current_net_worth: float = Field(0.0, alias="currentNetWorth")
    annual_spend: float = Field(0.0, alias="annualSpend")
    current_age: float = Field(30.0, ge=0, le=120, alias="currentAge")
    target_fi_age: float = Field(55.0, ge=0, le=120, alias="targetFiAge")
    annual_savings: float = Field(0.0, alias="annualSavings")
    swr: float = Field(0.035, ge=0, le=0.2, alias="swr")
    nominal_return: float = Field(0.07, ge=-0.5, le=1.0, alias="nominalReturn")
    inflation: float = Field(0.03, ge=-0.2, le=1.0, alias="inflation")
    lean_mult: float = Field(0.7, gt=0, le=5, alias="leanMult")
    fat_mult: float = Field(1.5, gt=0, le=5, alias="fatMult")
    band: float = Field(0.02, ge=0, le=0.2, alias="band")
    income: float | None = Field(None, alias="income")  # savingsRate = annual_savings / income; use a consistent basis (gross, or take-home + pre-tax savings) so the rate stays 0–1
    current_year: int = Field(2026, ge=1900, le=3000, alias="currentYear")

    model_config = {"populate_by_name": True}


class StateCompareModel(BaseModel):
    gross_wages: float = Field(0.0, ge=0, alias="grossWages")
    lt_capital_gains: float = Field(0.0, ge=0, alias="ltCapitalGains")
    taxable_spend_monthly: float = Field(0.0, ge=0, alias="taxableSpendMonthly")
    home_value: float = Field(0.0, ge=0, alias="homeValue")
    wa_region: Literal["seattle", "wa_avg"] = Field("seattle", alias="waRegion")
    tx_region: Literal["austin", "dallas", "tx_avg"] = Field("austin", alias="txRegion")
    vehicle_value: float = Field(0.0, ge=0, alias="vehicleValue")
    gallons_per_year: float = Field(0.0, ge=0, alias="gallonsPerYear")

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------

def _to_brackets(models: list[BracketModel], fallback: list[calc.Bracket]) -> list[calc.Bracket]:
    if not models:
        return list(fallback)
    return [calc.Bracket(upper=m.upper, rate=m.rate) for m in models]


def _to_budgeting_inputs(m: BudgetModel) -> budgeting.Inputs:
    return budgeting.Inputs(
        net_take_home=m.net_take_home,
        pretax_401k=m.pretax_401k,
        pretax_hsa=m.pretax_hsa,
        pretax_health_premiums=m.pretax_health_premiums,
        other_nontax_payroll=m.other_nontax_payroll,
        post_tax_savings=m.post_tax_savings,
        framework=m.framework,
        savings_rate=m.savings_rate,
        categories=m.categories,
        base_mode=m.base_mode,
        gross_income=m.gross_income,
        employer_match=m.employer_match,
    )


def _to_investing_profile(m: InvestModel) -> investing.Profile:
    return investing.Profile(
        age=m.age,
        state=m.state,
        gross_income=m.gross_income,
        monthly_essential_expenses=m.monthly_essential_expenses,
        ef_balance=m.ef_balance,
        debts=[{"balance": d.balance, "apr": d.apr} for d in m.debts],
        employer_match={
            "pct_of_salary": m.employer_match.pct_of_salary,
            "match_rate": m.employer_match.match_rate,
        },
        k401_room=m.k401_room,
        hsa_room=m.hsa_room,
        ira_room=m.ira_room,
        aftertax_401k_room=m.aftertax_401k_room,
        hsa_eligible=m.hsa_eligible,
        roth_magi_over_limit=m.roth_magi_over_limit,
        pretax_ira_balance=m.pretax_ira_balance,
        mega_available=m.mega_available,
        retire_state_no_tax=m.retire_state_no_tax,
        retire_marginal_rate=m.retire_marginal_rate,
        retire_income=m.retire_income,
        ef_starter_target=m.ef_starter_target,
        ef_months_target=m.ef_months_target,
        high_interest_threshold=m.high_interest_threshold,
    )


def _to_auto_dict(m: AutoModel) -> dict:
    """Build the snake_case dict that auto.calculate() expects."""
    d: dict = {
        "msrp": m.msrp,
        "cap_cost_reduction": m.cap_cost_reduction,
        "residual_pct": m.residual_pct,
        "term_months": m.term_months,
        "sales_tax_rate": m.sales_tax_rate,
        "acquisition_fee": m.acquisition_fee,
        "disposition_fee": m.disposition_fee,
        "drive_off_fees": m.drive_off_fees,
        "allowed_miles_per_year": m.allowed_miles_per_year,
        "actual_miles_per_year": m.actual_miles_per_year,
        "overage_rate": m.overage_rate,
        "finance_annual_rate": m.finance_annual_rate,
        "finance_term_months": m.finance_term_months,
        "finance_down": m.finance_down,
        "finance_trade_in_value": m.finance_trade_in_value,
        "finance_trade_in_payoff": m.finance_trade_in_payoff,
        "finance_fees": m.finance_fees,
        "annual_insurance": m.annual_insurance,
        "annual_fuel": m.annual_fuel,
        "annual_maintenance_base": m.annual_maintenance_base,
        "annual_maintenance_increment": m.annual_maintenance_increment,
        "investment_rate": m.investment_rate,
        "gross_monthly_income": m.gross_monthly_income,
        "include_fuel_maint": m.include_fuel_maint,
    }
    # Optional fields: only include when explicitly provided
    if m.cap_cost is not None:
        d["cap_cost"] = m.cap_cost
    if m.residual_value is not None:
        d["residual_value"] = m.residual_value
    if m.money_factor is not None:
        d["money_factor"] = m.money_factor
    if m.apr is not None:
        d["apr"] = m.apr
    if m.finance_sales_tax_rate is not None:
        d["finance_sales_tax_rate"] = m.finance_sales_tax_rate
    if m.monthly_insurance is not None:
        d["monthly_insurance"] = m.monthly_insurance
    if m.monthly_fuel is not None:
        d["monthly_fuel"] = m.monthly_fuel
    if m.monthly_maintenance is not None:
        d["monthly_maintenance"] = m.monthly_maintenance
    if m.retained_value is not None:
        d["retained_value"] = m.retained_value
    if m.ev:
        d["ev"] = True
    if m.horizons is not None:
        d["horizons"] = m.horizons
    return d


# ---------------------------------------------------------------------------
# HA theme adapter (DEC-024 v1, file-based)
#
# Reads an optional Home Assistant theme file and hands the frontend a small, verbatim
# passthrough of its custom-property tokens so the app can visually match the user's HA
# theme. Every step is best-effort: no theme file, an unparseable one, or a missing YAML
# parser all resolve to "no theme" (never an exception) so the app renders its existing
# dark palette exactly as before this feature existed.
# ---------------------------------------------------------------------------

_VAR_REF_RE = re.compile(r"^var\(--([\w-]+)\)$")

# Module-level constant (not a literal inline in load_theme()) purely so tests can
# monkeypatch it to a tmp path and exercise the "/data override wins over the bundled
# default" precedence without touching the real HA add-on volume at /data.
_DATA_THEME_PATH = Path("/data/theme.json")


def load_theme() -> tuple[dict, str] | None:
    """Locate + parse the HA theme file. Search order (first hit wins):

      1. `FINANCE_THEME_FILE` env var (explicit override — if it points at a missing or
         unparseable file, this is treated as "no theme", not a fall-through).
      2. `/data/theme.json`                        — HA add-on volume; a user-provided
         device override always wins over the bundled default (#4 below).
      3. `./theme.local.json`                      — repo-local JSON override for dev.
      4. `<server.py dir>/theme.json`               — the BUNDLED default (DEC-025):
         addon/build_bundle.sh pre-converts homeassistant/theme/masai.yaml to JSON at
         build time (on a machine with PyYAML) and places it next to server.py in the
         deploy bundle, so the no-PyYAML production image (DEC-021) still boots with a
         theme present with zero runtime YAML parsing. `ROOT` is `Path(__file__).resolve
         ().parent`, so this resolves to `/app/theme.json` on the device and to the repo
         root in the sandbox (normally absent there — masai.yaml (#5) covers the sandbox).
      5. `./homeassistant/theme/masai.yaml`         — the shipped sandbox theme (works
         out of the box in dev, where PyYAML is installed).
      6. `./theme.local.yaml`                       — repo-local YAML override for dev.

    JSON candidates always parse (stdlib `json`, no optional dependency). YAML candidates
    require the soft-imported `yaml` module; when it's unavailable (production add-on
    image, DEC-021) a YAML candidate is simply skipped rather than raising. Returns
    (raw_dict, source_path_str) on success, or None when no candidate yields a usable
    dict — never raises to the caller.
    """
    log = logging.getLogger("finance")
    env_path = os.environ.get("FINANCE_THEME_FILE")
    candidates = [Path(env_path)] if env_path else [
        _DATA_THEME_PATH,
        ROOT / "theme.local.json",
        ROOT / "theme.json",
        ROOT / "homeassistant" / "theme" / "masai.yaml",
        ROOT / "theme.local.yaml",
    ]
    for path in candidates:
        try:
            if not path.is_file():
                continue
            suffix = path.suffix.lower()
            if suffix == ".json":
                raw = json.loads(path.read_text(encoding="utf-8"))
            elif suffix in (".yaml", ".yml"):
                if yaml is None:
                    log.debug("theme candidate %s is YAML but PyYAML is unavailable; skipping", path)
                    continue
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
            else:
                log.debug("theme candidate %s has an unrecognized extension; skipping", path)
                continue
            if not isinstance(raw, dict) or not raw:
                log.debug("theme candidate %s parsed to an empty/invalid structure; skipping", path)
                continue
            log.info("HA theme loaded from %s", path)
            return raw, str(path)
        except Exception:
            log.debug("failed to read/parse theme candidate %s", path, exc_info=True)
            continue
    log.debug("no HA theme file found (checked %d candidate(s)); using built-in dark palette", len(candidates))
    return None


def _resolve_hex(value: object, table: dict, _depth: int = 0) -> str | None:
    """Resolve a literal '#hex' color, or one level of 'var(--x)' indirection against
    `table`, to a literal hex string. Follows short var() chains (up to 6 hops) but never
    evaluates arbitrary CSS — anything else (e.g. rgba()/hsl() expressions) returns None
    since we deliberately do not resolve CSS at parse time (that's the browser's job)."""
    if not isinstance(value, str) or _depth > 6:
        return None
    v = value.strip()
    if v.startswith("#"):
        return v
    m = _VAR_REF_RE.match(v)
    if m:
        return _resolve_hex(table.get(m.group(1)), table, _depth + 1)
    return None


def _relative_luminance(hex_color: str) -> float | None:
    """WCAG relative luminance of a '#rgb' or '#rrggbb' color, or None if unparseable."""
    h = hex_color.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    if len(h) != 6:
        return None
    try:
        r, g, b = (int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return None

    def _lin(c: float) -> float:
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _detect_mode(base: dict, modes: dict) -> str:
    """light/dark rule: resolve a background-ish token (md-sys-color-background, then
    md-sys-color-surface, then primary-background-color) to a literal hex — trying the
    'light' mode block first if present, else whichever mode block exists — and compute
    its relative luminance. L >= 0.5 -> 'light', else 'dark'. If nothing resolves to a
    literal hex (unusual/custom theme shape), default to 'light': that's an explicit,
    documented fallback rather than a silent guess, and matches the one theme (Shiro /
    masai.yaml) this adapter ships against today."""
    mode_names = ["light"] if "light" in modes else (list(modes.keys()) or [None])
    for mode_name in mode_names:
        block = modes.get(mode_name) if mode_name is not None else {}
        table = {**base, **(block if isinstance(block, dict) else {})}
        for key in ("md-sys-color-background", "md-sys-color-surface", "primary-background-color"):
            hexval = _resolve_hex(table.get(key), table)
            if hexval is not None:
                lum = _relative_luminance(hexval)
                if lum is not None:
                    return "light" if lum >= 0.5 else "dark"
    return "light"


def _looks_pre_normalized(raw: dict) -> bool:
    return "available" in raw and isinstance(raw.get("vars"), dict)


def normalize_theme(raw: dict, source: str = "file") -> dict | None:
    """Pure transform: raw parsed theme file -> the transport contract:

        {"available": True, "name": <top-level theme key>, "mode": "light"|"dark",
         "source": source, "vars": {<every theme token>: <string value>, ...}}

    Accepts two input shapes:
      - A raw HA theme-YAML mapping: {<theme name>: {...tokens..., modes: {light: {...}}}}.
        `name` is read from the FILE's top-level key (never the filename — a theme file
        named e.g. masai.yaml may define a theme called "Shiro"). `modes.<active mode>`
        is merged OVER the base tokens (mode-specific values win) per Home Assistant's
        own theme-resolution semantics; the `modes` key itself is dropped from `vars`.
      - An already-normalized JSON shape (has "available" + a "vars" dict) — passed
        through as-is (values re-stringified defensively), letting production ship a
        pre-converted /data/theme.json without needing this function's YAML-shape logic.

    Deliberate choice: ALL top-level theme tokens are kept in `vars` (elevation, motion,
    shape, typeface, color, legacy semantic aliases — not just md-sys-color-*). They are
    harmless as unused CSS custom properties if the frontend only consumes a subset, and
    keeping everything avoids a second "which keys matter" decision that would silently
    break if the theme file adds new tokens later. Values are passed through VERBATIM
    (var(...) references, block-scalar shadow/motion strings, literal hex) — CSS resolves
    var() chains at runtime, not this function.

    Returns None on anything that isn't a usable dict (never raises).
    """
    if not isinstance(raw, dict) or not raw:
        return None
    try:
        if _looks_pre_normalized(raw):
            vars_ = {str(k): (v if isinstance(v, str) else str(v)) for k, v in raw["vars"].items()}
            if not vars_:
                return None
            mode = raw.get("mode")
            return {
                "available": True,
                "name": str(raw.get("name") or "theme"),
                "mode": mode if mode in ("light", "dark") else "light",
                "source": source,
                "vars": vars_,
            }

        # Raw HA theme-YAML shape: top-level maps theme-name -> {tokens...}. Take the
        # first entry whose value is itself a mapping (skip stray top-level scalars).
        name, body = None, None
        for k, v in raw.items():
            if isinstance(v, dict):
                name, body = k, v
                break
        if name is None or body is None:
            return None

        body = dict(body)  # don't mutate the parsed input
        modes = body.pop("modes", None)
        modes = modes if isinstance(modes, dict) else {}
        mode = _detect_mode(body, modes)
        active = modes.get(mode)
        active = active if isinstance(active, dict) else {}
        merged = {**body, **active}

        vars_ = {}
        for k, v in merged.items():
            if isinstance(v, (dict, list)):
                continue  # not a scalar theme token; skip (none expected in practice)
            vars_[str(k)] = v if isinstance(v, str) else str(v)
        if not vars_:
            return None

        return {
            "available": True,
            "name": str(name),
            "mode": mode,
            "source": source,
            "vars": vars_,
        }
    except Exception:
        logging.getLogger("finance").debug("normalize_theme failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/")
def index(request: Request) -> HTMLResponse:
    """Serve the frontend. Under Home Assistant ingress (DEC-021) the app lives at a
    rewritten prefix (/api/hassio_ingress/<token>/); the Supervisor forwards that base
    in the X-Ingress-Path header. Inject it so the frontend's fetch shim can prefix
    every /api/... call. Outside HA the header is absent and the base stays ''."""
    base = request.headers.get("X-Ingress-Path", "").rstrip("/")
    # Defense-in-depth (review 2026-07-13): the header is Supervisor-set under ingress,
    # but don't TRUST that — reject anything that isn't a plain absolute path, and
    # escape "</" so a hostile value can never break out of the injected <script>.
    if base and not re.fullmatch(r"/[\w\-./]*", base):
        base = ""
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    inject = "<script>window.__API_BASE=" + json.dumps(base).replace("</", "<\\/") + ";</script>"

    # HA theme adapter (DEC-024): inject window.__HA_THEME only when a theme is actually
    # available. No file / parse error / no yaml module -> inject nothing, so the
    # frontend's window.__HA_THEME stays undefined and it renders today's dark palette.
    loaded = load_theme()
    if loaded is not None:
        theme = normalize_theme(loaded[0], "file")
        if theme is not None:
            inject += ("<script>window.__HA_THEME=" + json.dumps(theme).replace("</", "<\\/") + ";</script>")

    # index.html's <head> is a bare literal tag (no attributes) — pinned invariant.
    return HTMLResponse(html.replace("<head>", "<head>" + inject, 1))


@app.get("/health")
def health() -> dict:
    """Liveness probe for the add-on watchdog (DEC-021): process up + DB reachable."""
    try:
        with closing(tracking_store.connect()) as c:
            c.execute("SELECT 1").fetchone()
        return {"ok": True}
    except Exception:
        logging.getLogger("finance").exception("health check failed")
        raise HTTPException(status_code=503, detail="db unavailable")


@app.get("/api/theme")
def theme_endpoint() -> dict:
    """DEC-024 v1 file-based HA theme adapter. Always 200 (never 204 — the frontend
    parses JSON either way): the normalized theme when available, else {"available":
    False}. Read-only, no request body/params. Under HA this sits behind ingress auth;
    in the sandbox it's localhost-only. Returns only theme/color strings — never
    secrets, never SUPERVISOR_TOKEN."""
    loaded = load_theme()
    if loaded is None:
        return {"available": False}
    theme = normalize_theme(loaded[0], "file")
    if theme is None:
        return {"available": False}
    return theme


def _load_tax_year_overlay() -> dict:
    """Overlay year-dependent figures from the newest tax_data/<year>.json, if any.

    Lets the annual IRS/SSA refresh be a data change (run scripts/update_tax_values.py)
    rather than a code edit. Returns {} when no data file ships, so the literals in
    defaults() remain the fallback. Picks the newest year file (clock-independent).
    """
    import json
    tdir = ROOT / "tax_data"
    if not tdir.is_dir():
        return {}
    files = sorted(tdir.glob("[0-9][0-9][0-9][0-9].json"))
    if not files:
        return {}
    try:
        data = json.loads(files[-1].read_text())
    except Exception:
        return {}
    return dict(data.get("values", {}))


@app.get("/api/defaults")
def defaults() -> dict:
    """Return all server-side default rates, brackets, and limits so the UI can hydrate.

    Year-dependent figures (brackets, std deduction, contribution limits, SS base, Roth
    MAGI band) are overlaid from tax_data/<year>.json when present — the literals below
    are the fallback. Refresh the data file with scripts/update_tax_values.py.
    """
    d = {
        # ---- Tax brackets ----
        "fedBrackets": [{"upper": b.upper, "rate": b.rate} for b in calc.DEFAULT_FED_BRACKETS],
        "caBrackets": [{"upper": b.upper, "rate": b.rate} for b in calc.DEFAULT_CA_BRACKETS],
        # ---- Federal / FICA ----
        "fedStd": 16_100,
        "ssBase": 184_500,
        "ssRate": 0.062,
        "medRate": 0.0145,
        "addlMedThresh": 200_000,
        "addlMedRate": 0.009,
        # ---- California ----
        "caStd": 5_540,
        "caSdi": 0.012,
        "caMhstThresh": 1_000_000,
        "caMhstRate": 0.01,
        # ---- 2026 contribution limits ----
        "k401Limit": 24_500,
        "iraLimit": 7_500,
        "hsaLimitSelf": 4_400,
        "hsaLimitFamily": 8_750,
        "sec415cLimit": 72_000,
        # ---- Roth IRA MAGI phase-out (2026, single filer) ----
        "rothIraLimit": 7_500,
        "rothIraPhaseIn": 153_000,
        "rothIraPhaseOut": 168_000,
        # ---- Auto depreciation curve + note ----
        "autoDepreciation": {
            "curve": auto.DEFAULT_RETAINED_VALUE,
            "moneyFactorNote": (
                "Money factor × 2400 = approximate APR. "
                "A factor of 0.00125 ≈ 3.0 % APR; always verify the exact "
                "equivalent APR with the dealer before signing."
            ),
        },
        # ---- Investing / savings-waterfall thresholds ----
        "efStarterTarget": 1_000,
        "efMonthsTarget": 6,
        "highInterestThreshold": 0.06,
        # ---- Not-financial-advice disclaimer ----
        "disclaimer": _DISCLAIMER,
    }
    d.update(_load_tax_year_overlay())   # newest tax_data/<year>.json wins, if present

    # Per-filing-status federal defaults so the UI can repopulate the bracket table and
    # the NIIT / Add'l-Medicare / LTCG / Roth-MAGI thresholds when the user switches status.
    # "single" mirrors the (possibly overlaid) values above; "mfj" layers the calculator's
    # MFJ overrides so there is one source of truth (calc.FILING_STATUS_OVERRIDES).
    _mfj = calc.FILING_STATUS_OVERRIDES["mfj"]
    d["filingStatusDefaults"] = {
        "single": {
            "fedStd": d["fedStd"],
            "fedBrackets": d["fedBrackets"],
            "addlMedThresh": d["addlMedThresh"],
            "niitThreshold": 200_000,
            "ltcg0pctUpper": 49_450,
            "ltcg15pctUpper": 545_500,
            "rothIraPhaseIn": d["rothIraPhaseIn"],
            "rothIraPhaseOut": d["rothIraPhaseOut"],
        },
        "mfj": {
            "fedStd": _mfj["fed_std_deduction"],
            "fedBrackets": [{"upper": b.upper, "rate": b.rate} for b in _mfj["fed_brackets"]],
            "addlMedThresh": _mfj["addl_medicare_threshold"],
            "niitThreshold": _mfj["niit_threshold"],
            "ltcg0pctUpper": _mfj["ltcg_0pct_upper"],
            "ltcg15pctUpper": _mfj["ltcg_15pct_upper"],
            "rothIraPhaseIn": _mfj["roth_ira_phase_in"],
            "rothIraPhaseOut": _mfj["roth_ira_phase_out"],
        },
    }
    return d


@app.post("/api/calculate")
def calculate_endpoint(inp: InputModel) -> dict:
    inputs = calc.Inputs(
        salary=inp.salary,
        trad_401k=inp.trad_401k,
        roth_401k=inp.roth_401k,
        hsa=inp.hsa,
        employer_hsa=inp.employer_hsa,
        hsa_coverage=inp.hsa_coverage,
        medical=inp.medical,
        dental=inp.dental,
        vision=inp.vision,
        ee_stock=inp.ee_stock,
        roth_ira=inp.roth_ira,
        er_stock=inp.er_stock,
        gtli=inp.gtli,
        state=inp.state,  # type: ignore[arg-type]
        filing_status=inp.filing_status,  # type: ignore[arg-type]
        fed_std_deduction=inp.fed_std_deduction,
        fed_brackets=_to_brackets(inp.fed_brackets, calc.DEFAULT_FED_BRACKETS),
        ss_wage_base=inp.ss_wage_base,
        ss_rate=inp.ss_rate,
        medicare_rate=inp.medicare_rate,
        addl_medicare_threshold=inp.addl_medicare_threshold,
        addl_medicare_rate=inp.addl_medicare_rate,
        ca_std_deduction=inp.ca_std_deduction,
        ca_brackets=_to_brackets(inp.ca_brackets, calc.DEFAULT_CA_BRACKETS),
        ca_sdi_rate=inp.ca_sdi_rate,
        ca_mhst_threshold=inp.ca_mhst_threshold,
        ca_mhst_rate=inp.ca_mhst_rate,
        roth_ira_limit=inp.roth_ira_limit,
        roth_ira_phase_in=inp.roth_ira_phase_in,
        roth_ira_phase_out=inp.roth_ira_phase_out,
        backdoor_roth=inp.backdoor_roth,
        bonus=inp.bonus,
        after_tax_401k=inp.after_tax_401k,
        employer_401k_match=inp.employer_401k_match,
        sec415c_limit=inp.sec415c_limit,
        long_term_gains=inp.long_term_gains,
        short_term_gains=inp.short_term_gains,
        qualified_dividends=inp.qualified_dividends,
        ordinary_dividends=inp.ordinary_dividends,
        taxable_interest=inp.taxable_interest,
        ltcg_0pct_upper=inp.ltcg_0pct_upper,
        ltcg_15pct_upper=inp.ltcg_15pct_upper,
        niit_threshold=inp.niit_threshold,
        espp_shares_sold=inp.espp_shares_sold,
        espp_purchase_price_per_share=inp.espp_purchase_price_per_share,
        espp_purchase_fmv_per_share=inp.espp_purchase_fmv_per_share,
        espp_grant_fmv_per_share=inp.espp_grant_fmv_per_share,
        espp_sale_price_per_share=inp.espp_sale_price_per_share,
        espp_qualifying=inp.espp_qualifying,
        espp_disq_gain_long_term=inp.espp_disq_gain_long_term,
        rsu_shares_sold=inp.rsu_shares_sold,
        rsu_vest_fmv_per_share=inp.rsu_vest_fmv_per_share,
        rsu_sale_price_per_share=inp.rsu_sale_price_per_share,
        rsu_long_term=inp.rsu_long_term,
        prior_year_fed_tax=inp.prior_year_fed_tax,
        safe_harbor_rate=inp.safe_harbor_rate,
    )
    return calc.calculate(inputs)


@app.post("/api/budget")
def budget_endpoint(inp: BudgetModel) -> dict:
    return budgeting.calculate(_to_budgeting_inputs(inp))


@app.post("/api/invest")
def invest_endpoint(inp: InvestModel) -> dict:
    profile = _to_investing_profile(inp)
    return investing.calculate(profile, inp.amount)


@app.post("/api/auto")
def auto_endpoint(inp: AutoModel) -> dict:
    return auto.calculate(_to_auto_dict(inp))


@app.post("/api/project")
def project_endpoint(inp: ProjectModel) -> dict:
    return investing.project_growth(inp.contributions, inp.balances, inp.annual_return, inp.years)


@app.post("/api/networth")
def networth_endpoint(inp: NetWorthModel) -> dict:
    return investing.project_net_worth(
        inp.current_net_worth, inp.annual_contribution, inp.years, inp.annual_return, inp.band, inp.targets
    )


@app.post("/api/fire")
def fire_endpoint(inp: FireModel) -> dict:
    return _fire.compute_fire(
        current_net_worth=inp.current_net_worth,
        annual_spend=inp.annual_spend,
        current_age=inp.current_age,
        target_fi_age=inp.target_fi_age,
        annual_savings=inp.annual_savings,
        swr=inp.swr,
        nominal_return=inp.nominal_return,
        inflation=inp.inflation,
        lean_mult=inp.lean_mult,
        fat_mult=inp.fat_mult,
        band=inp.band,
        income=inp.income,
        current_year=inp.current_year,
    )


class FlexBucketModel(BaseModel):
    key: str
    label: str = ""
    now: float = Field(0.0, ge=0)


class AffordabilityModel(BaseModel):
    mode: Literal["payment_first", "payment_last"]
    household: bool = False
    your_take_home: float = Field(0.0, ge=0, alias="yourTakeHome")
    partner_take_home: float = Field(0.0, ge=0, alias="partnerTakeHome")
    partner_debt: float = Field(0.0, ge=0, alias="partnerDebt")
    protected_needs: float = Field(0.0, ge=0, alias="protectedNeeds")
    investing: float = Field(0.0, ge=0)
    current_housing: float = Field(0.0, ge=0, alias="currentHousing")
    flex_buckets: list[FlexBucketModel] = Field(default_factory=list, alias="flexBuckets")
    desired_payment: float = Field(0.0, ge=0, alias="desiredPayment")
    down_pct: float = Field(20.0, ge=0, lt=100, alias="downPct")
    rate_pct: float = Field(6.5, ge=0, le=30, alias="ratePct")
    term_years: float = Field(30.0, gt=0, le=50, alias="termYears")
    prop_tax_rate_pct: float = Field(1.65, ge=0, le=10, alias="propTaxRatePct")
    insurance_yr: float = Field(2_400.0, ge=0, alias="insuranceYr")
    hoa_mo: float = Field(0.0, ge=0, alias="hoaMo")

    model_config = {"populate_by_name": True}


@app.post("/api/affordability")
def affordability_endpoint(inp: AffordabilityModel) -> dict:
    try:
        return affordability.afford(
            inp.mode, inp.your_take_home,
            household=inp.household,
            partner_take_home=inp.partner_take_home,
            partner_debt=inp.partner_debt,
            protected_needs=inp.protected_needs,
            investing=inp.investing,
            current_housing=inp.current_housing,
            flex_buckets=[b.model_dump() for b in inp.flex_buckets],
            desired_payment=inp.desired_payment,
            down_pct=inp.down_pct,
            rate_pct=inp.rate_pct,
            term_years=inp.term_years,
            prop_tax_rate_pct=inp.prop_tax_rate_pct,
            insurance_yr=inp.insurance_yr,
            hoa_mo=inp.hoa_mo,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/state-compare")
def state_compare_endpoint(inp: StateCompareModel) -> dict:
    try:
        return state_compare.compare_states(
            gross_wages=inp.gross_wages,
            lt_capital_gains=inp.lt_capital_gains,
            taxable_spend_monthly=inp.taxable_spend_monthly,
            home_value=inp.home_value,
            wa_region=inp.wa_region,
            tx_region=inp.tx_region,
            vehicle_value=inp.vehicle_value,
            gallons_per_year=inp.gallons_per_year,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


# ---------------------------------------------------------------------------
# Plan-vs-actual tracking (north star). Data lives in our own SQLite (DEC-006);
# the comparison logic is pure (tracking.py); this is the thin HTTP edge.
# ---------------------------------------------------------------------------

def _cents(dollars: float) -> int:
    return round(float(dollars) * 100)


class AccountModel(BaseModel):
    name: str
    type: str = "other"
    is_liability: bool = Field(False, alias="isLiability")
    currency: str = "USD"
    invest_group: str | None = Field(None, alias="investGroup")   # Invest-tab grouping (TODO-222)
    model_config = {"populate_by_name": True}


class AccountUpdateModel(BaseModel):
    name: str | None = None
    type: str | None = None
    is_liability: bool | None = Field(None, alias="isLiability")
    archived: bool | None = None
    currency: str | None = None
    invest_group: str | None = Field(None, alias="investGroup")   # "" clears the group (PATCH drops None)
    model_config = {"populate_by_name": True}


class TransactionModel(BaseModel):
    account_id: int = Field(..., alias="accountId")
    posted_on: str = Field(..., alias="postedOn")          # 'YYYY-MM-DD'
    direction: Literal["in", "out"]
    amount: float                                          # dollars; stored as cents
    bucket: str | None = None
    category: str | None = None
    description: str | None = None
    is_transfer: bool = Field(False, alias="isTransfer")
    transfer_group: str | None = Field(None, alias="transferGroup")
    external_id: str | None = Field(None, alias="externalId")
    tags: list[str] | None = None
    splits: list[dict] | None = None                        # [{bucket, category, amount}] in dollars
    partner_owed: float = Field(0, alias="partnerOwed")     # partner's share of a shared expense (Venmo)
    status: Literal["settled", "pending"] = "settled"
    kind: Literal["charge", "refund"] = "charge"
    model_config = {"populate_by_name": True}


class CardPaymentModel(BaseModel):
    card_account_id: int = Field(..., alias="cardAccountId")
    amount: float
    date: str
    from_account_id: int | None = Field(None, alias="fromAccountId")
    apply_to_category: str | None = Field(None, alias="applyToCategory")
    model_config = {"populate_by_name": True}


class CardPaymentEditModel(BaseModel):
    """PATCH /api/tracking/card-payment/{in_leg_id} body.

    Full-replace contract: ``applyToCategory`` absent or null means "whole card" and
    clears any existing earmark.  Do NOT treat omitted as "no change".
    """
    amount: float
    apply_to_category: str | None = Field(None, alias="applyToCategory")
    model_config = {"populate_by_name": True}


class SnapshotModel(BaseModel):
    account_id: int = Field(..., alias="accountId")
    as_of: str = Field(..., alias="asOf")                  # 'YYYY-MM-DD'
    balance: float                                         # dollars; stored as cents
    model_config = {"populate_by_name": True}


class TransactionUpdateModel(BaseModel):
    posted_on: str | None = Field(None, alias="postedOn")
    account_id: int | None = Field(None, alias="accountId")
    direction: Literal["in", "out"] | None = None
    amount: float | None = None                            # dollars; stored as cents
    bucket: str | None = None
    category: str | None = None
    description: str | None = None
    is_transfer: bool | None = Field(None, alias="isTransfer")
    transfer_group: str | None = Field(None, alias="transferGroup")
    tags: list[str] | None = None
    partner_owed: float | None = Field(None, alias="partnerOwed")
    status: Literal["settled", "pending"] | None = None
    kind: Literal["charge", "refund"] | None = None
    model_config = {"populate_by_name": True}


class ImportModel(BaseModel):
    csv: str                                               # raw CSV text (header required)
    model_config = {"populate_by_name": True}


class PlanLockModel(BaseModel):
    """The client computes the plan baseline (it owns the planner inputs) and posts the
    derived figures; the server freezes them. status='draft' for the open month,
    'locked' for immutable history (DEC-007)."""
    bucket_planned: dict[str, float] = Field(default_factory=dict, alias="bucketPlanned")
    income_planned: float = Field(0.0, alias="incomePlanned")
    savings_rate_planned: float = Field(0.0, alias="savingsRatePlanned")
    forecast_cone: list[dict] = Field(default_factory=list, alias="forecastCone")
    anchor_date: str = Field("", alias="anchorDate")
    anchor_value: float = Field(0.0, alias="anchorValue")
    status: Literal["draft", "locked"] = "locked"
    engine_version: str = Field("1.0", alias="engineVersion")
    model_config = {"populate_by_name": True}


# Scenario planner (TODO-219, DEC-017). The spec/clientState blobs are opaque to the
# server (stored + round-tripped, never parsed into behavior) — bounded to keep the
# unauthenticated tracking surface from becoming a disk-filler (SEC-004 posture).
_SCENARIO_MAX_MONTHS = 24
_SCENARIO_MAX_BLOB_BYTES = 512 * 1024


class ScenarioCreateModel(BaseModel):
    name: str
    spec: dict                                             # opaque what-if definition (DEC-017 #3)
    model_config = {"populate_by_name": True}


class ScenarioUpdateModel(BaseModel):
    name: str | None = None
    spec: dict | None = None
    model_config = {"populate_by_name": True}


class ScenarioPlanMonthModel(PlanLockModel):
    """One month's derived plan figures — the same shape the /plan/{month}/lock
    endpoint takes, plus which month it lands on."""
    month: str


class ScenarioActivateModel(BaseModel):
    activation_month: str = Field(..., alias="activationMonth")
    plan_months: list[ScenarioPlanMonthModel] = Field(..., alias="planMonths",
                                                      min_length=1, max_length=_SCENARIO_MAX_MONTHS)
    client_state: dict | list | str | None = Field(None, alias="clientState")
    model_config = {"populate_by_name": True}


class CatchupAccountModel(BaseModel):
    key: str | None = None
    label: str | None = None
    annual_target: float = Field(0.0, alias="annualTarget", ge=0)
    already_in: float = Field(0.0, alias="alreadyIn", ge=0)
    model_config = {"populate_by_name": True}


class ExtraPrincipalGoalModel(BaseModel):
    label: str | None = None
    target_amount: float = Field(0.0, alias="targetAmount", ge=0)
    already_in: float = Field(0.0, alias="alreadyIn", ge=0)
    target_date: str | None = Field(None, alias="targetDate")  # default: the catch-up yearEnd
    model_config = {"populate_by_name": True}


class CatchupModel(BaseModel):
    accounts: list[CatchupAccountModel] = Field(default_factory=list, max_length=50)
    activation_month: str = Field(..., alias="activationMonth")
    pay_freq: int = Field(24, alias="payFreq", ge=1, le=366)
    year_end: str | None = Field(None, alias="yearEnd")    # default: Dec 31 of the activation year
    extra_principal_goals: list[ExtraPrincipalGoalModel] = Field(
        default_factory=list, alias="extraPrincipalGoals", max_length=50)
    net_per_paycheck: float | None = Field(None, alias="netPerPaycheck")
    model_config = {"populate_by_name": True}


class PlanDeltaModel(BaseModel):
    current: dict                                          # window.__budgetPlan-shaped
    scenario: dict
    model_config = {"populate_by_name": True}


# ----- accounts -----

@app.post("/api/tracking/accounts")
def create_account_endpoint(m: AccountModel) -> dict:
    with closing(tracking_store.connect()) as c:
        try:
            return tracking_store.create_account(c, m.name, m.type, m.is_liability, m.currency, invest_group=m.invest_group)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/tracking/accounts")
def list_accounts_endpoint(includeArchived: bool = False) -> dict:
    with closing(tracking_store.connect()) as c:
        return {"accounts": tracking_store.list_accounts(c, include_archived=includeArchived)}


@app.patch("/api/tracking/accounts/{account_id}")
def update_account_endpoint(account_id: int, m: AccountUpdateModel) -> dict:
    fields = {k: v for k, v in m.model_dump(by_alias=False).items() if v is not None}
    with closing(tracking_store.connect()) as c:
        try:
            acct = tracking_store.update_account(c, account_id, **fields)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if acct is None:
            raise HTTPException(status_code=404, detail="account not found")
        return acct


@app.delete("/api/tracking/accounts/{account_id}")
def delete_account_endpoint(account_id: int) -> dict:
    with closing(tracking_store.connect()) as c:
        try:
            tracking_store.delete_account(c, account_id)
        except ValueError as e:                    # venture-linked account (DEC-020 invariant)
            raise HTTPException(status_code=422, detail=str(e))
    return {"deleted": account_id}


# ----- transactions -----

@app.post("/api/tracking/transactions")
def create_txn_endpoint(m: TransactionModel) -> dict:
    splits = None
    if m.splits:
        splits = [{"bucket": sp.get("bucket"), "category": sp.get("category"),
                   "amount_cents": _cents(sp.get("amount", 0))} for sp in m.splits]
    with closing(tracking_store.connect()) as c:
        try:
            return tracking_store.create_txn(
                c, m.account_id, m.posted_on, m.direction, _cents(m.amount),
                bucket=m.bucket, category=m.category, description=m.description,
                is_transfer=m.is_transfer, transfer_group=m.transfer_group, external_id=m.external_id,
                tags=m.tags, splits=splits, partner_owed_cents=_cents(m.partner_owed or 0),
                status=m.status, kind=m.kind,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/tracking/transactions")
def list_txns_endpoint(month: str | None = None, accountId: int | None = None,
                       bucket: str | None = None, direction: str | None = None,
                       tag: str | None = None) -> dict:
    with closing(tracking_store.connect()) as c:
        txns = tracking_store.list_txns(c, month=month, account_id=accountId,
                                        bucket=bucket, direction=direction, tag=tag)
    return {"transactions": txns, "count": len(txns)}


@app.get("/api/tracking/tags")
def list_tags_endpoint() -> dict:
    with closing(tracking_store.connect()) as c:
        return {"tags": tracking_store.list_tags(c)}


@app.patch("/api/tracking/transactions/{txn_id}")
def update_txn_endpoint(txn_id: int, m: TransactionUpdateModel) -> dict:
    fields = {}
    for k, v in m.model_dump(by_alias=False).items():
        if v is None:
            continue
        if k == "amount":
            fields["amount_cents"] = _cents(v)
        elif k == "partner_owed":
            fields["partner_owed_cents"] = _cents(v)
        else:
            fields[k] = v
    with closing(tracking_store.connect()) as c:
        try:
            txn = tracking_store.update_txn(c, txn_id, **fields)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    if txn is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    return txn


@app.delete("/api/tracking/transactions/{txn_id}")
def delete_txn_endpoint(txn_id: int) -> dict:
    with closing(tracking_store.connect()) as c:
        ids = tracking_store.delete_txn(c, txn_id)
    return {"deleted": txn_id, "deletedIds": ids, "rows": len(ids)}


@app.get("/api/tracking/suggest")
def suggest_endpoint() -> dict:
    """Quick-add autocomplete: payee memory + categories-by-bucket (drives the datalist)."""
    with closing(tracking_store.connect()) as c:
        return tracking_store.suggestions(c)


# ----- recurring templates (pre-fill only) -----

class TemplateModel(BaseModel):
    name: str
    direction: Literal["in", "out"] = "out"
    amount: float = 0
    bucket: str | None = None
    category: str | None = None
    account_id: int | None = Field(None, alias="accountId")
    description: str | None = None
    model_config = {"populate_by_name": True}


@app.post("/api/tracking/templates")
def create_template_endpoint(m: TemplateModel) -> dict:
    with closing(tracking_store.connect()) as c:
        try:
            return tracking_store.create_template(
                c, m.name, direction=m.direction, amount_cents=_cents(m.amount),
                bucket=m.bucket, category=m.category, account_id=m.account_id, description=m.description)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/tracking/templates")
def list_templates_endpoint() -> dict:
    with closing(tracking_store.connect()) as c:
        return {"templates": tracking_store.list_templates(c)}


@app.delete("/api/tracking/templates/{template_id}")
def delete_template_endpoint(template_id: int) -> dict:
    with closing(tracking_store.connect()) as c:
        tracking_store.delete_template(c, template_id)
    return {"deleted": template_id}


# ----- target-savings goals (TODO-226, DEC-019) -----

class GoalModel(BaseModel):
    name: str
    target: float = Field(..., gt=0)                              # dollars
    target_date: str = Field(..., alias="targetDate")             # YYYY-MM-DD
    account_id: int | None = Field(None, alias="accountId")       # linked balance source
    saved_so_far: float | None = Field(None, ge=0, alias="savedSoFar")  # manual fallback
    model_config = {"populate_by_name": True}


class GoalUpdateModel(BaseModel):
    name: str | None = None
    target: float | None = Field(None, gt=0)
    target_date: str | None = Field(None, alias="targetDate")
    account_id: int | None = Field(None, alias="accountId")
    clear_account: bool = Field(False, alias="clearAccount")      # None can't signal "unlink" — this does
    saved_so_far: float | None = Field(None, ge=0, alias="savedSoFar")
    status: Literal["active", "done", "cancelled"] | None = None
    model_config = {"populate_by_name": True}


def _goal_with_progress(c, g: dict, pay_freq: float) -> dict:
    saved = tracking_store.goal_saved_cents(c, g) / 100.0
    out = dict(g)
    out["saved"] = saved
    # Pace anchors at creation, clamped to the target date: a goal created with an
    # already-past deadline still yields a valid (overdue) progress block instead of
    # start_date > target_date raising and nulling the whole thing (review finding 2).
    start = (g.get("createdAt") or "")[:10] or None
    if start and start > g["targetDate"]:
        start = g["targetDate"]
    try:
        out["progress"] = goals.goal_progress(
            g["target"], g["targetDate"], saved, _date.today(),
            pay_freq=pay_freq, start_date=start)
    except ValueError:
        out["progress"] = None    # bad stored dates should never 500 the list
    return out


def _check_pay_freq(pay_freq: float) -> float:
    # Plain defaults (not Query(...)) so tests can call these endpoint functions
    # directly — FastAPI still binds/coerces query params either way.
    if not pay_freq or pay_freq <= 0:
        raise HTTPException(status_code=422, detail="payFreq must be > 0")
    return float(pay_freq)


@app.get("/api/tracking/goals")
def list_goals_endpoint(payFreq: float = 24.0, includeInactive: bool = False) -> dict:
    pf = _check_pay_freq(payFreq)
    with closing(tracking_store.connect()) as c:
        rows = tracking_store.list_goals(c, include_inactive=includeInactive)
        return {"goals": [_goal_with_progress(c, g, pf) for g in rows]}


@app.post("/api/tracking/goals")
def create_goal_endpoint(m: GoalModel, payFreq: float = 24.0) -> dict:
    pf = _check_pay_freq(payFreq)    # validate BEFORE the write — a bad param must not commit
    with closing(tracking_store.connect()) as c:
        try:
            g = tracking_store.create_goal(
                c, m.name, _cents(m.target), m.target_date, account_id=m.account_id,
                manual_saved_cents=None if m.saved_so_far is None else _cents(m.saved_so_far))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return _goal_with_progress(c, g, pf)


@app.patch("/api/tracking/goals/{goal_id}")
def update_goal_endpoint(goal_id: int, m: GoalUpdateModel, payFreq: float = 24.0) -> dict:
    pf = _check_pay_freq(payFreq)    # validate BEFORE the write — a bad param must not commit
    fields: dict = {}
    if m.name is not None:
        fields["name"] = m.name
    if m.target is not None:
        fields["target_cents"] = _cents(m.target)
    if m.target_date is not None:
        fields["target_date"] = m.target_date
    if m.clear_account:
        fields["account_id"] = None
    elif m.account_id is not None:
        fields["account_id"] = m.account_id
    if m.saved_so_far is not None:
        fields["manual_saved_cents"] = _cents(m.saved_so_far)
    if m.status is not None:
        fields["status"] = m.status
    with closing(tracking_store.connect()) as c:
        try:
            g = tracking_store.update_goal(c, goal_id, **fields)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if g is None:
            raise HTTPException(status_code=404, detail="goal not found")
        return _goal_with_progress(c, g, pf)


@app.delete("/api/tracking/goals/{goal_id}")
def delete_goal_endpoint(goal_id: int) -> dict:
    with closing(tracking_store.connect()) as c:
        tracking_store.delete_goal(c, goal_id)
    return {"deleted": goal_id}


# ----- venture ROI tracker (TODO-228, DEC-020) -----

class VentureItemModel(BaseModel):
    label: str
    amount: float = Field(..., gt=0)   # dollars


class VentureModel(BaseModel):
    name: str
    items: list[VentureItemModel] = Field(..., min_length=1)
    started_on: str = Field(..., alias="startedOn")
    tag: str | None = None
    account_id: int | None = Field(None, alias="accountId")
    model_config = {"populate_by_name": True}


class VentureUpdateModel(BaseModel):
    name: str | None = None
    items: list[VentureItemModel] | None = None
    started_on: str | None = Field(None, alias="startedOn")
    tag: str | None = None
    account_id: int | None = Field(None, alias="accountId")
    status: Literal["active", "stopped"] | None = None
    model_config = {"populate_by_name": True}


def _venture_with_roi(c, v: dict) -> dict:
    f = tracking_store.venture_flows(c, v)
    out = dict(v)
    out["txnCount"] = f["txnCount"]
    by_month = {m: {"revenue": mm["revenueCents"] / 100.0, "cost": mm["costCents"] / 100.0}
                for m, mm in f["byMonth"].items()}
    out["byMonth"] = by_month
    try:
        out["roi"] = ventures.venture_roi(
            v["invested"], f["revenueCents"] / 100.0, f["costCents"] / 100.0,
            v["startedOn"], _date.today(), by_month=by_month)
    except ValueError:
        out["roi"] = None    # bad stored data must never 500 the list
    return out


@app.get("/api/tracking/ventures")
def list_ventures_endpoint(includeStopped: bool = False) -> dict:
    with closing(tracking_store.connect()) as c:
        rows = tracking_store.list_ventures(c, include_stopped=includeStopped)
        return {"ventures": [_venture_with_roi(c, v) for v in rows]}


@app.post("/api/tracking/ventures")
def create_venture_endpoint(m: VentureModel) -> dict:
    with closing(tracking_store.connect()) as c:
        try:
            v = tracking_store.create_venture(
                c, m.name,
                [{"label": i.label, "amountCents": _cents(i.amount)} for i in m.items],
                m.started_on, tag=m.tag, account_id=m.account_id)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return _venture_with_roi(c, v)


@app.patch("/api/tracking/ventures/{venture_id}")
def update_venture_endpoint(venture_id: int, m: VentureUpdateModel) -> dict:
    fields: dict = {}
    if m.name is not None:
        fields["name"] = m.name
    if m.items is not None:
        fields["items"] = [{"label": i.label, "amountCents": _cents(i.amount)} for i in m.items]
    if m.started_on is not None:
        fields["started_on"] = m.started_on
    if m.tag is not None:
        fields["tag"] = m.tag
    if m.account_id is not None:
        fields["account_id"] = m.account_id
    if m.status is not None:
        fields["status"] = m.status
    with closing(tracking_store.connect()) as c:
        try:
            v = tracking_store.update_venture(c, venture_id, **fields)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if v is None:
            raise HTTPException(status_code=404, detail="venture not found")
        return _venture_with_roi(c, v)


@app.delete("/api/tracking/ventures/{venture_id}")
def delete_venture_endpoint(venture_id: int) -> dict:
    with closing(tracking_store.connect()) as c:
        tracking_store.delete_venture(c, venture_id)
    return {"deleted": venture_id}


# ----- recurring expectations (monthly bills/income seeded from the budget) -----

class RecurringModel(BaseModel):
    category: str
    direction: Literal["in", "out"] = "out"
    bucket: str | None = None
    due_day: int | None = Field(None, alias="dueDay")
    expected: float = 0
    active: bool = True
    model_config = {"populate_by_name": True}


@app.post("/api/tracking/recurring")
def upsert_recurring_endpoint(m: RecurringModel) -> dict:
    with closing(tracking_store.connect()) as c:
        try:
            return tracking_store.upsert_recurring(
                c, m.category, direction=m.direction, bucket=m.bucket,
                due_day=m.due_day, expected_cents=_cents(m.expected), active=m.active)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/tracking/recurring")
def list_recurring_endpoint() -> dict:
    with closing(tracking_store.connect()) as c:
        return {"recurring": tracking_store.list_recurring(c)}


@app.delete("/api/tracking/recurring/{recurring_id}")
def delete_recurring_endpoint(recurring_id: int) -> dict:
    with closing(tracking_store.connect()) as c:
        tracking_store.delete_recurring(c, recurring_id)
    return {"deleted": recurring_id}


@app.post("/api/tracking/transactions/import")
def import_txns_endpoint(m: ImportModel) -> dict:
    """Bulk CSV import. Header required; columns case-insensitive, order-independent:
    date,account,direction,amount,bucket,category,description,transfer_group,external_id.
    Valid rows commit; bad rows are reported and skipped (partial success)."""
    imported, skipped, errors = 0, 0, []
    with closing(tracking_store.connect()) as c:
        accounts = {a["name"].lower(): a["id"] for a in tracking_store.list_accounts(c, include_archived=True)}
        reader = csv.DictReader(io.StringIO(m.csv))
        for i, raw in enumerate(reader, start=2):     # row 1 is the header
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            try:
                acct_key = row.get("account", "").lower()
                acct_id = accounts.get(acct_key)
                if acct_id is None and acct_key.isdigit():
                    acct_id = int(acct_key)
                if acct_id is None:
                    raise ValueError(f"unknown account {row.get('account')!r}")
                amount = float(row["amount"].replace(",", "").replace("$", ""))
                direction = (row.get("direction") or "").lower()
                if direction not in ("in", "out"):
                    direction = "out" if amount < 0 else "in" if direction == "in" else "out"
                tg = row.get("transfer_group") or None
                tags = [s.strip() for s in (row.get("tags") or "").replace("|", ";").split(";") if s.strip()] or None
                tracking_store.create_txn(
                    c, acct_id, row["date"], direction, _cents(abs(amount)),
                    bucket=(row.get("bucket") or None), category=(row.get("category") or None),
                    description=(row.get("description") or None),
                    is_transfer=bool(tg), transfer_group=tg,
                    source="csv", external_id=(row.get("external_id") or None), tags=tags,
                )
                imported += 1
            except Exception as e:   # noqa: BLE001 — partial success: report and skip the row
                skipped += 1
                errors.append({"row": i, "reason": str(e)})
    return {"imported": imported, "skipped": skipped, "errors": errors}


# ----- balance snapshots -----

@app.post("/api/tracking/snapshots")
def upsert_snapshot_endpoint(m: SnapshotModel) -> dict:
    with closing(tracking_store.connect()) as c:
        return tracking_store.upsert_snapshot(c, m.account_id, m.as_of, _cents(m.balance))


@app.get("/api/tracking/snapshots")
def list_snapshots_endpoint(accountId: int | None = None,
                            date_from: str | None = Query(None, alias="from"),
                            date_to: str | None = Query(None, alias="to")) -> dict:
    with closing(tracking_store.connect()) as c:
        return {"snapshots": tracking_store.list_snapshots(
            c, account_id=accountId, date_from=date_from, date_to=date_to)}


@app.delete("/api/tracking/snapshots/{snapshot_id}")
def delete_snapshot_endpoint(snapshot_id: int) -> dict:
    with closing(tracking_store.connect()) as c:
        tracking_store.delete_snapshot(c, snapshot_id)
    return {"deleted": snapshot_id}


# ----- plan baseline + the headline comparison -----

@app.post("/api/tracking/plan/{month}/lock")
def lock_plan_endpoint(month: str, m: PlanLockModel) -> dict:
    payload = tracking.build_plan(
        month, bucket_planned=m.bucket_planned, income_planned=m.income_planned,
        savings_rate_planned=m.savings_rate_planned, forecast_cone=m.forecast_cone,
        anchor_date=m.anchor_date, anchor_value=m.anchor_value, engine_version=m.engine_version,
    )
    with closing(tracking_store.connect()) as c:
        return tracking_store.save_plan(c, month, payload, status=m.status, engine_version=m.engine_version)


@app.get("/api/tracking/plan/{month}")
def get_plan_endpoint(month: str) -> dict:
    with closing(tracking_store.connect()) as c:
        plan = tracking_store.get_plan(c, month)
    if plan is None:
        raise HTTPException(status_code=404, detail="no plan for month")
    return plan


@app.get("/api/tracking/plan-vs-actual")
def plan_vs_actual_endpoint(month: str, tol: float = 0.05) -> dict:
    with closing(tracking_store.connect()) as c:
        actuals = tracking_store.month_actuals(c, month)
        plan = tracking_store.get_plan(c, month)
    if plan is None:
        # No baseline yet — return actuals so the UI can still show them and prompt to lock.
        return {"month": month, "needsPlan": True, "comparison": None, "actuals": actuals}
    comparison = tracking.plan_vs_actual(plan["payload"], actuals, month, tol)
    return {"month": month, "needsPlan": False, "planStatus": plan["status"], "comparison": comparison}


# ----- scenarios (TODO-219, DEC-017): what-if comp + catch-up + revertible activation -----

def _scenario_blob_guard(obj, what: str) -> None:
    """413 when an opaque client blob is oversized — same posture as MAX_IMPORT_BYTES."""
    if obj is not None and len(json.dumps(obj)) > _SCENARIO_MAX_BLOB_BYTES:
        raise HTTPException(status_code=413, detail=f"{what} exceeds {_SCENARIO_MAX_BLOB_BYTES} bytes")


@app.get("/api/tracking/scenarios")
def list_scenarios_endpoint() -> dict:
    with closing(tracking_store.connect()) as c:
        return {"scenarios": tracking_store.list_scenarios(c)}


@app.post("/api/tracking/scenarios")
def create_scenario_endpoint(m: ScenarioCreateModel) -> dict:
    _scenario_blob_guard(m.spec, "spec")
    with closing(tracking_store.connect()) as c:
        try:
            return tracking_store.create_scenario(c, m.name, m.spec)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/tracking/scenarios/{scenario_id}")
def get_scenario_endpoint(scenario_id: int) -> dict:
    with closing(tracking_store.connect()) as c:
        s = tracking_store.get_scenario(c, scenario_id)
    if s is None:
        raise HTTPException(status_code=404, detail="scenario not found")
    return s


@app.put("/api/tracking/scenarios/{scenario_id}")
def update_scenario_endpoint(scenario_id: int, m: ScenarioUpdateModel) -> dict:
    _scenario_blob_guard(m.spec, "spec")
    with closing(tracking_store.connect()) as c:
        try:
            s = tracking_store.update_scenario(c, scenario_id, name=m.name, spec=m.spec)
        except tracking_store.ScenarioConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    if s is None:
        raise HTTPException(status_code=404, detail="scenario not found")
    return s


@app.delete("/api/tracking/scenarios/{scenario_id}")
def delete_scenario_endpoint(scenario_id: int) -> dict:
    with closing(tracking_store.connect()) as c:
        try:
            ok = tracking_store.delete_scenario(c, scenario_id)
        except tracking_store.ScenarioConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="scenario not found")
    return {"deleted": scenario_id}


@app.post("/api/tracking/scenarios/{scenario_id}/activate")
def activate_scenario_endpoint(scenario_id: int, m: ScenarioActivateModel) -> dict:
    """Install the scenario from its activation month onward (DEC-017 #5): one
    transaction writing plan snapshots for months >= M through the same machinery
    as /plan/{month}/lock; months < M are never touched (DEC-007). 409 while
    another scenario is active — revert it first."""
    _scenario_blob_guard(m.client_state, "clientState")
    plan_months = [pm.model_dump(by_alias=True) for pm in m.plan_months]
    with closing(tracking_store.connect()) as c:
        try:
            out = tracking_store.activate_scenario(
                c, scenario_id, m.activation_month, plan_months, client_state=m.client_state)
        except tracking_store.ScenarioConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    if out is None:
        raise HTTPException(status_code=404, detail="scenario not found")
    return out


@app.post("/api/tracking/scenarios/{scenario_id}/revert")
def revert_scenario_endpoint(scenario_id: int) -> dict:
    """Exactly undo activation: restore every overwritten plan snapshot, delete the
    ones activation created, flip the scenario back to draft, and hand back the
    opaque clientState so the client restores its budget/Tax config (DEC-017 #6)."""
    with closing(tracking_store.connect()) as c:
        try:
            out = tracking_store.revert_scenario(c, scenario_id)
        except tracking_store.ScenarioConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))
    if out is None:
        raise HTTPException(status_code=404, detail="scenario not found")
    return out


@app.post("/api/scenario/catchup")
def scenario_catchup_endpoint(m: CatchupModel) -> dict:
    """Pure catch-up schedule (no DB): per-account and per-goal rest-of-year pace
    from the activation month. The transient time-axis view — never folded into
    the level plan baseline (DEC-017 #4)."""
    year_end = m.year_end or f"{m.activation_month[:4]}-12-31"
    try:
        return _scenarios.catchup_plan(
            [a.model_dump(by_alias=True) for a in m.accounts],
            m.activation_month, m.pay_freq, year_end,
            extra_principal_goals=[g.model_dump(by_alias=True) for g in m.extra_principal_goals],
            net_per_paycheck=m.net_per_paycheck,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.post("/api/scenario/plan-delta")
def scenario_plan_delta_endpoint(m: PlanDeltaModel) -> dict:
    """Pure diff of the current vs scenario budget plans for the compare view."""
    return _scenarios.budget_plan_delta(m.current, m.scenario)


@app.get("/api/tracking/card-rollup")
def card_rollup_endpoint(month: str) -> dict:
    with closing(tracking_store.connect()) as c:
        accounts = tracking_store.list_accounts(c)
        credit_ids = [a["id"] for a in accounts if a["type"] == "credit"]
        txns = tracking_store.list_txns(
            c,
            date_to=tracking.month_end(month),
            account_ids=credit_ids,
        )
    rollup = tracking.card_rollup_running(txns, accounts, month)
    return {"month": month, "rollup": rollup}


@app.get("/api/tracking/open-pending")
def open_pending_endpoint(month: str) -> dict:
    with closing(tracking_store.connect()) as c:
        accounts = tracking_store.list_accounts(c)
        credit_ids = [a["id"] for a in accounts if a["type"] == "credit"]
        txns = tracking_store.list_txns(
            c,
            status="pending",
            date_before=f"{month}-01",
            account_ids=credit_ids,
        )
    return {"month": month, "txns": txns}


@app.post("/api/tracking/card-payment")
def card_payment_endpoint(m: CardPaymentModel) -> dict:
    with closing(tracking_store.connect()) as c:
        if _cents(m.amount) <= 0:
            raise HTTPException(status_code=422, detail="amount must be > 0")
        card_acct = tracking_store.get_account(c, m.card_account_id)
        if card_acct is None or card_acct["type"] != "credit":
            raise HTTPException(status_code=422, detail="cardAccountId must be an existing credit account")
        if m.from_account_id is not None:
            if m.from_account_id == m.card_account_id:
                raise HTTPException(status_code=422, detail="fromAccountId must differ from cardAccountId")
            from_acct = tracking_store.get_account(c, m.from_account_id)
            # No type check on from_acct: any account type may fund a card payment by design.
            if from_acct is None:
                raise HTTPException(status_code=422, detail="fromAccountId must be an existing account")
        tg = uuid.uuid4().hex
        try:
            ids = tracking_store.record_card_payment(
                c, m.card_account_id, _cents(m.amount), m.date, tg,
                from_account_id=m.from_account_id, description="Card payment",
                bucket=m.apply_to_category,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    return {
        "transferGroup": tg,
        "cardTxnId": ids[0],
        "fundingTxnId": ids[1] if len(ids) > 1 else None,
        "txnIds": ids,
    }


@app.patch("/api/tracking/card-payment/{in_leg_id}")
def edit_card_payment_endpoint(in_leg_id: int, m: CardPaymentEditModel) -> dict:
    """Edit the amount and/or earmark bucket on a card-payment transfer-IN leg.

    Full-replace contract: ``applyToCategory`` absent or null clears any existing earmark
    (means "whole card").  Amount is written to both legs of the transfer so the pair
    stays balanced.  The funding account is not changed.

    Returns the updated IN-leg txn dict.
    404 when the id does not exist; 422 when the id is not a card-payment IN-leg or
    validation fails (non-positive amount, empty bucket string).
    """
    with closing(tracking_store.connect()) as c:
        try:
            txn = tracking_store.update_card_payment(
                c, in_leg_id,
                amount_cents=_cents(m.amount),
                bucket=m.apply_to_category,
            )
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    if txn is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    return txn


# ----- backup / restore -----

@app.get("/api/tracking/export")
def export_backup_endpoint() -> JSONResponse:
    """Dump the entire actuals DB as a JSON backup file (Content-Disposition: attachment).

    Values are raw integers (cents), not dollars — import_all restores them verbatim.
    """
    with closing(tracking_store.connect()) as c:
        payload = tracking_store.export_all(c)
    stamp = payload["exportedAt"].replace(":", "").replace("-", "")
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="actuals-backup-{stamp}.json"'},
    )


@app.post("/api/tracking/import")
async def import_backup_endpoint(request: Request) -> dict:
    """Atomically restore the actuals DB from a JSON backup produced by GET /api/tracking/export.

    A pre-import safety copy is written alongside the live DB file before any mutation.
    Returns {restored: {table: count}, schemaVersion: int, safetyCopy: path|null}.
    413 when the body exceeds MAX_IMPORT_BYTES — checked twice: once against the
    declared Content-Length header BEFORE the body is read (SEC-002; rejects an
    oversized upload without buffering it), and again against the actual byte count
    after reading (backstop for a missing or understated Content-Length). 422 for
    malformed JSON or an invalid/incompatible backup; 500 if the safety-copy write
    fails (no mutation occurred).
    """
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            declared_length = int(content_length)
        except ValueError:
            declared_length = None   # non-numeric header — fall through to the post-read check
        if declared_length is not None and declared_length > MAX_IMPORT_BYTES:
            raise HTTPException(status_code=413, detail="payload too large")
    raw = await request.body()
    if len(raw) > MAX_IMPORT_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    with closing(tracking_store.connect()) as c:
        try:
            return tracking_store.import_all(c, payload)
        except tracking_store.RestoreError as e:
            raise HTTPException(status_code=422, detail=str(e))


# ----- CSV export (analysis / tax-prep — NOT a backup: no app tag, never importable) -----

@app.get("/api/tracking/export.csv")
def export_txns_csv_endpoint(date_from: str | None = Query(None, alias="from"),
                              date_to: str | None = Query(None, alias="to")) -> Response:
    """Date-ranged transactions CSV for analysis/tax-prep (Content-Disposition: attachment).

    `from`/`to` are optional inclusive ISO date (YYYY-MM-DD) bounds on posted_on; either or
    both may be omitted for an unbounded side. Plainly a spreadsheet, not a restorable
    backup — see tracking_store.export_txns_csv for the column contract. 422 for a
    malformed date or from > to.
    """
    with closing(tracking_store.connect()) as c:
        try:
            csv_text = tracking_store.export_txns_csv(c, date_from, date_to)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    filename = f"transactions-{date_from or 'all'}_{date_to or 'all'}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
