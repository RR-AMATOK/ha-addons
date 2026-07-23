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
import time
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
# Identity resolver (multi-user S0.1 + S0.2-lite, DEC-026/DEC-031)
# ---------------------------------------------------------------------------

# HA Supervisor's fixed ingress peer IP (DEC-021 threat model / DEC-026 "Verified
# contract"). X-Remote-User-Id is trustworthy ONLY when the request's peer IP is
# exactly this address -- uvicorn is never configured to trust forwarded headers from
# anywhere else, so a header arriving from any other peer must be treated as absent.
_SUPERVISOR_PEER = "172.30.32.2"

# SEV-003 (2026-07-23 audit): per-caller failed link-code-redeem timestamps (monotonic
# seconds) for the 10-failures-per-10-min throttle on /api/tracking/link. In-memory by
# design — single uvicorn process; resets on add-on restart, which is acceptable for a
# defense-in-depth cap on an already ingress-authenticated, 40-bit-space endpoint.
_LINK_REDEEM_FAILURES: dict = {}


def _trusted_single_header(request: Request, name: str) -> str | None:
    """Read a single-valued, trimmed header with the SAME discipline `resolve_user`
    applies to ``X-Remote-User-Id`` (SEV-S1.1-002): a header repeated on more than one
    line, or one that strips down to nothing, is treated as wholly ABSENT rather than
    guessed at. Callers are responsible for the peer-IP trust gate (`_SUPERVISOR_PEER`)
    -- this helper only handles the header's shape, not who's allowed to send it."""
    values = request.headers.getlist(name)
    if len(values) != 1:
        return None
    trimmed = values[0].strip()
    return trimmed or None


def resolve_user(request: Request | None) -> dict:
    """The ONE identity resolver (DEC-026 "Verified contract" paragraph; DEC-031 §3).
    Every identity-dependent code path MUST call this -- never read X-Remote-User-Id
    (or any other identity signal) directly.

    Returns ``{"id": <str>, "role": "owner"|"member", "scopeId": <str>}``.

    ``scopeId`` (S1.1, per-user data separation) is the ONE seam every `/api/tracking/*`
    data-table call must thread -- never ``id``:

        scopeId = "__owner__" if role == "owner" else id

    Why a second id at all: at migration time (boot, before any request) the `users`
    table is empty and the real human owner hasn't been provisioned yet -- the v8
    migration backfills every pre-existing (single-tenant) row to the sentinel
    `"__owner__"` WITHOUT knowing the owner's eventual real HA UUID. If callers scoped
    the owner's reads by their real UUID instead, every migrated row would be invisible
    (data-loss-equivalent). Since there is exactly one owner (`idx_users_one_owner`),
    "owner role -> `__owner__` slot" is a clean 1:1, and `__owner__` is deliberately not
    a valid HA UUID so it can never collide with a member id. Identity (`id`, used by
    whoami/audit/the roster) and data-scope (`scopeId`) are DELIBERATELY kept separate --
    never conflate them.

    `request` may be ``None`` for direct in-process calls (test helpers that don't
    construct a Request) -- this resolves exactly like an absent header/query param,
    i.e. the owner fallback. Real ASGI requests always supply a concrete Request.

    Precedence (highest first; see docs/multiuser-household-plan.md constraints):
      1. TRUSTED INGRESS HEADER -- ``X-Remote-User-Id``, but ONLY when
         ``request.client.host`` equals ``_SUPERVISOR_PEER``. A header arriving from
         any other peer IP is spoofable and is treated as wholly ABSENT: it is never
         read into an identity (spoof protection). Additionally, the header value is
         trusted ONLY when it appears EXACTLY ONCE and is non-empty AFTER STRIPPING --
         a duplicate header (two ``X-Remote-User-Id`` lines, however that arose) or an
         all-whitespace value is also treated as wholly ABSENT (never selects one of
         several candidate ids, and never lets a blank header silently enable the dev
         override below). This keeps the resolver safe regardless of whether HA's
         Supervisor strips inbound ``X-Remote-User-*`` headers before proxying. The
         value is trimmed exactly ONCE (SEV-S1.1-002) and that same trimmed string is
         used both for the truthiness check above AND as the identity itself, so
         ``" abc "`` and ``"abc"`` are always the same id -- never two distinct
         provisioned members.
      2. DEV OVERRIDE (DEC-022 sandbox parity) -- the ``FINANCE_DEV_USER`` env var,
         else the ``?user=`` query param (env wins when both are set -- an arbitrary
         but stable, documented precedence). INERT whenever step 1 found a trusted
         header, so a sandbox/dev artifact can never ride along with, or override, a
         real ingress session.
      3. OWNER FALLBACK (DEC-031 §3) -- no trusted header AND no dev override. There
         is NO unauthenticated profile picker: this always resolves to the concrete
         household owner, lazily provisioning the canonical sentinel owner row if the
         `users` table is still empty (first boot, sandbox, or a pre-header HA Core
         version). This closes the DEC-026 fallback vulnerability by design.

    ACCOUNT LINKING (identity aliases -- "appoint admins" via linking, N HA accounts ->
    1 profile): once a concrete `seen_id` is determined above (trusted header or dev
    override), it is resolved ALIAS -> PRIMARY (tracking_store.resolve_identity(),
    one hop, via the `user_alias` table) BEFORE any provisioning/role lookup happens.
    A linked id therefore NEVER gets its own `users` row from that point on -- it
    resolves to the EXACT SAME {id, role} its primary would get, including scopeId, so
    linking one of the owner's OWN second logins to their primary is how "appoint an
    admin" falls out of this design with no separate admin-grant concept. This is why
    provisioning happens inside resolve_identity() rather than here directly (see below).

    Any id resolved via a trusted header or the dev override is lazily provisioned in
    the `users` table (tracking_store.resolve_identity(), which delegates to
    resolve_or_provision_user() once any alias has been collapsed to its primary): the
    very first id ever seen this way becomes 'owner'; every subsequently-seen new id
    becomes 'member' (at most one owner).

    RESERVED SENTINEL (SEV-S1.1-001): ``"__owner__"`` (tracking_store._SENTINEL_OWNER_ID)
    is not a real identity -- it is the internal data-scope slot for the owner role. If
    a trusted header or dev override ever resolves to exactly this string, it is REJECTED
    (HTTPException) rather than provisioned or silently folded into the owner fallback.
    Mapping it to the owner fallback would be wrong: it would GRANT owner-level data
    access to whoever supplied it. This is unreachable given the hardened ingress
    contract (HA always sends real 32-hex UUIDs; the dev override never runs in
    production), but is enforced here in code, not by convention alone.

    HUMAN-READABLE NAMES (household roster naming): when a trusted header supplies an
    id, this also reads ``X-Remote-User-Display-Name`` (preferred), falling back to
    ``X-Remote-User-Name`` when Display-Name is absent/empty -- DEC-026's "Verified
    contract" documents BOTH as possibly absent (older Supervisor/Core, or an HA user
    with no display name configured). Same single-value + trim-once discipline as the
    id itself (SEV-S1.1-002): a duplicated or all-whitespace header is treated as
    absent, never guessed at. The captured value (or None) is passed to
    ``resolve_or_provision_user`` — stored at first provisioning and refreshed on a
    later request whenever it changes (people rename their HA account); it is NEVER
    read from the dev override or owner-fallback paths, so a sandbox session's id
    doubles as its own name there, matching pre-this-feature behavior. When neither
    header is ever sent, the roster falls back to the owner-editable ``label``
    (PATCH /api/tracking/users/{id}) -- this is a complete fallback, not a partial one:
    a pre-header Supervisor version or a service account can still get a friendly name.
    """
    client = getattr(request, "client", None)
    peer_host = getattr(client, "host", None) if client is not None else None
    header_id = None
    header_display_name = None
    if peer_host == _SUPERVISOR_PEER:
        header_values = request.headers.getlist("X-Remote-User-Id")
        if len(header_values) == 1:
            # SEV-S1.1-002: trim ONCE, then use that same trimmed value for both the
            # trust decision (non-empty after stripping) and the id itself -- so " abc "
            # and "abc" are the same identity, not two different provisioned members.
            # An all-whitespace header strips to "" (falsy) and is treated as absent,
            # same as today (-> dev override / owner fallback).
            trimmed = header_values[0].strip()
            if trimmed:
                header_id = trimmed
                header_display_name = (
                    _trusted_single_header(request, "X-Remote-User-Display-Name")
                    or _trusted_single_header(request, "X-Remote-User-Name")
                )

    # Dev-override precedence: trusted header always wins; then the env pin; then the
    # per-request ?user= param; then the sticky sandbox cookie set by GET / (?user=...).
    # The cookie exists so an in-page API call (which never carries the page's query
    # string) resolves to the SAME simulated identity as the page that made it —
    # without it, a "?user=partner" tab silently does all its API work as the owner
    # (the 2026-07-22 false-alarm "isolation gap"). Ordering keeps it production-inert:
    # under ingress the header is present on every request and shadows everything else.
    # "off" is the cookie-clearing sentinel (see GET /), never an identity: the clearing
    # load itself must already resolve as the owner, not provision a member named "off".
    _dev_param = getattr(request, "query_params", {}).get("user")
    _dev_cookie = getattr(request, "cookies", {}).get("fps_dev_user") if header_id is None else None
    if _dev_param == "off":
        _dev_param, _dev_cookie = None, None
    seen_id = header_id or os.environ.get("FINANCE_DEV_USER") or _dev_param or _dev_cookie

    # SEV-S1.1-001: "__owner__" is a reserved sentinel (tracking_store._SENTINEL_OWNER_ID)
    # meaning "the owner's data-scope slot", never a real identity. If a trusted header or
    # a dev override ever resolves to this literal string, it must be REJECTED outright --
    # NOT provisioned, and NOT silently mapped to the owner fallback (that would hand
    # whoever supplied it the owner's entire dataset). This is enforced here regardless of
    # whether the string arrived spoofed, misconfigured, or via a dev-only override.
    if seen_id == tracking_store._SENTINEL_OWNER_ID:
        raise HTTPException(
            status_code=400,
            detail="Invalid identity: this value is reserved and cannot be used.",
        )

    with closing(tracking_store.connect()) as c:
        if seen_id:
            # header_display_name is only ever non-None when header_id was resolved
            # (it's assigned inside that same `if trimmed:` block above), and header_id
            # always wins seen_id's precedence when present -- so this is never a stray
            # header name riding along with a dev-override id. resolve_identity()
            # collapses any alias to its primary (account linking) BEFORE this ever
            # provisions/looks up a role -- see this function's ACCOUNT LINKING
            # paragraph above.
            user = tracking_store.resolve_identity(c, seen_id, display_name=header_display_name)
        else:
            user = tracking_store.resolve_owner_fallback(c)
    user["scopeId"] = "__owner__" if user["role"] == "owner" else user["id"]
    return user


def require_owner(request: Request) -> dict:
    """Guard for owner-only endpoints (restore, backup/CSV downloads -- DEC-031 plan
    S0.2). Resolves identity and raises 403 for anyone who isn't the household owner;
    returns the resolved user dict on success so callers that also need it don't have
    to re-resolve."""
    user = resolve_user(request)
    if user["role"] != "owner":
        raise HTTPException(
            status_code=403,
            detail="This action is available to the household owner only.",
        )
    return user


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
    resp = HTMLResponse(html.replace("<head>", "<head>" + inject, 1))

    # Sticky sandbox identity (dev override only): anchor ?user=<id> in a cookie so the
    # page's own API calls resolve to the same simulated identity (see resolve_user's
    # precedence comment). Only when NO trusted header is present — under ingress the
    # header rides every request and this whole block is skipped. `?user=off` clears it.
    client = getattr(request, "client", None)
    peer_host = getattr(client, "host", None) if client is not None else None
    has_trusted_header = (
        peer_host == _SUPERVISOR_PEER
        and len(request.headers.getlist("X-Remote-User-Id")) == 1
        and request.headers.getlist("X-Remote-User-Id")[0].strip() != ""
    )
    if not has_trusted_header:
        dev_user = request.query_params.get("user")
        if dev_user == "off":
            resp.delete_cookie("fps_dev_user")
        elif dev_user and dev_user != tracking_store._SENTINEL_OWNER_ID:
            resp.set_cookie("fps_dev_user", dev_user, httponly=True, samesite="lax")
    return resp


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
def create_account_endpoint(m: AccountModel, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        try:
            return tracking_store.create_account(c, scope, m.name, m.type, m.is_liability, m.currency, invest_group=m.invest_group)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/tracking/accounts")
def list_accounts_endpoint(includeArchived: bool = False, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        return {"accounts": tracking_store.list_accounts(c, scope, include_archived=includeArchived)}


@app.patch("/api/tracking/accounts/{account_id}")
def update_account_endpoint(account_id: int, m: AccountUpdateModel, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    fields = {k: v for k, v in m.model_dump(by_alias=False).items() if v is not None}
    with closing(tracking_store.connect()) as c:
        try:
            acct = tracking_store.update_account(c, scope, account_id, **fields)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if acct is None:
            raise HTTPException(status_code=404, detail="account not found")
        return acct


@app.delete("/api/tracking/accounts/{account_id}")
def delete_account_endpoint(account_id: int, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        try:
            tracking_store.delete_account(c, scope, account_id)
        except ValueError as e:                    # venture-linked account (DEC-020 invariant)
            raise HTTPException(status_code=422, detail=str(e))
    return {"deleted": account_id}


# ----- transactions -----

@app.post("/api/tracking/transactions")
def create_txn_endpoint(m: TransactionModel, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    splits = None
    if m.splits:
        splits = [{"bucket": sp.get("bucket"), "category": sp.get("category"),
                   "amount_cents": _cents(sp.get("amount", 0))} for sp in m.splits]
    with closing(tracking_store.connect()) as c:
        try:
            tracking_store._require_own_account(c, scope, m.account_id)
            return tracking_store.create_txn(
                c, scope, m.account_id, m.posted_on, m.direction, _cents(m.amount),
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
                       tag: str | None = None, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        txns = tracking_store.list_txns(c, scope, month=month, account_id=accountId,
                                        bucket=bucket, direction=direction, tag=tag)
    return {"transactions": txns, "count": len(txns)}


@app.get("/api/tracking/tags")
def list_tags_endpoint(request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        return {"tags": tracking_store.list_tags(c, scope)}


@app.patch("/api/tracking/transactions/{txn_id}")
def update_txn_endpoint(txn_id: int, m: TransactionUpdateModel, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
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
            txn = tracking_store.update_txn(c, scope, txn_id, **fields)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    if txn is None:
        raise HTTPException(status_code=404, detail="transaction not found")
    return txn


@app.delete("/api/tracking/transactions/{txn_id}")
def delete_txn_endpoint(txn_id: int, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        ids = tracking_store.delete_txn(c, scope, txn_id)
    return {"deleted": txn_id, "deletedIds": ids, "rows": len(ids)}


@app.get("/api/tracking/suggest")
def suggest_endpoint(request: Request = None) -> dict:
    """Quick-add autocomplete: payee memory + categories-by-bucket (drives the datalist)."""
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        return tracking_store.suggestions(c, scope)


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
def create_template_endpoint(m: TemplateModel, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        try:
            if m.account_id is not None:
                tracking_store._require_own_account(c, scope, m.account_id)
            return tracking_store.create_template(
                c, scope, m.name, direction=m.direction, amount_cents=_cents(m.amount),
                bucket=m.bucket, category=m.category, account_id=m.account_id, description=m.description)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/tracking/templates")
def list_templates_endpoint(request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        return {"templates": tracking_store.list_templates(c, scope)}


@app.delete("/api/tracking/templates/{template_id}")
def delete_template_endpoint(template_id: int, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        tracking_store.delete_template(c, scope, template_id)
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


def _goal_with_progress(c, scope: str, g: dict, pay_freq: float) -> dict:
    saved = tracking_store.goal_saved_cents(c, scope, g) / 100.0
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
def list_goals_endpoint(payFreq: float = 24.0, includeInactive: bool = False, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    pf = _check_pay_freq(payFreq)
    with closing(tracking_store.connect()) as c:
        rows = tracking_store.list_goals(c, scope, include_inactive=includeInactive)
        return {"goals": [_goal_with_progress(c, scope, g, pf) for g in rows]}


@app.post("/api/tracking/goals")
def create_goal_endpoint(m: GoalModel, payFreq: float = 24.0, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    pf = _check_pay_freq(payFreq)    # validate BEFORE the write — a bad param must not commit
    with closing(tracking_store.connect()) as c:
        try:
            g = tracking_store.create_goal(
                c, scope, m.name, _cents(m.target), m.target_date, account_id=m.account_id,
                manual_saved_cents=None if m.saved_so_far is None else _cents(m.saved_so_far))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return _goal_with_progress(c, scope, g, pf)


@app.patch("/api/tracking/goals/{goal_id}")
def update_goal_endpoint(goal_id: int, m: GoalUpdateModel, payFreq: float = 24.0, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
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
            g = tracking_store.update_goal(c, scope, goal_id, **fields)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if g is None:
            raise HTTPException(status_code=404, detail="goal not found")
        return _goal_with_progress(c, scope, g, pf)


@app.delete("/api/tracking/goals/{goal_id}")
def delete_goal_endpoint(goal_id: int, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        tracking_store.delete_goal(c, scope, goal_id)
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


def _venture_with_roi(c, scope: str, v: dict) -> dict:
    f = tracking_store.venture_flows(c, scope, v)
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
def list_ventures_endpoint(includeStopped: bool = False, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        rows = tracking_store.list_ventures(c, scope, include_stopped=includeStopped)
        return {"ventures": [_venture_with_roi(c, scope, v) for v in rows]}


@app.post("/api/tracking/ventures")
def create_venture_endpoint(m: VentureModel, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        try:
            v = tracking_store.create_venture(
                c, scope, m.name,
                [{"label": i.label, "amountCents": _cents(i.amount)} for i in m.items],
                m.started_on, tag=m.tag, account_id=m.account_id)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return _venture_with_roi(c, scope, v)


@app.patch("/api/tracking/ventures/{venture_id}")
def update_venture_endpoint(venture_id: int, m: VentureUpdateModel, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
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
            v = tracking_store.update_venture(c, scope, venture_id, **fields)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if v is None:
            raise HTTPException(status_code=404, detail="venture not found")
        return _venture_with_roi(c, scope, v)


@app.delete("/api/tracking/ventures/{venture_id}")
def delete_venture_endpoint(venture_id: int, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        tracking_store.delete_venture(c, scope, venture_id)
    return {"deleted": venture_id}


# ----- sinking funds (TODO-238, DEC-034, docs/sinking-funds-design.md) -----
# Phase 1 only: CRUD + a separate fund-lens endpoint (like /api/tracking/card-rollup).
# Does NOT touch aggregate_actuals/plan_vs_actual or the month_actuals seam — those stay
# byte-unchanged (Phase 2 folds the funded-draw excusal into the headline on-track number).

class FundModel(BaseModel):
    name: str
    bucket: str | None = None
    monthly_contribution: float = Field(0, ge=0, alias="monthlyContribution")   # dollars
    target: float | None = Field(None, gt=0)                                   # dollars
    target_date: str | None = Field(None, alias="targetDate")                  # YYYY-MM-DD
    model_config = {"populate_by_name": True}


class FundUpdateModel(BaseModel):
    name: str | None = None
    bucket: str | None = None
    monthly_contribution: float | None = Field(None, ge=0, alias="monthlyContribution")
    target: float | None = Field(None, gt=0)
    target_date: str | None = Field(None, alias="targetDate")
    clear_target: bool = Field(False, alias="clearTarget")   # unsets target + targetDate together
    status: Literal["active", "archived"] | None = None
    model_config = {"populate_by_name": True}


class FundLinkModel(BaseModel):
    txn_id: int = Field(..., alias="txnId")
    role: Literal["contribute", "draw"]
    model_config = {"populate_by_name": True}


@app.get("/api/tracking/funds")
def list_funds_endpoint(includeArchived: bool = False, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        return {"funds": tracking_store.list_funds(c, scope, include_archived=includeArchived)}


@app.post("/api/tracking/funds")
def create_fund_endpoint(m: FundModel, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        try:
            return tracking_store.create_fund(
                c, scope, m.name, bucket=m.bucket,
                monthly_contribution_cents=_cents(m.monthly_contribution),
                target_cents=None if m.target is None else _cents(m.target),
                target_date=m.target_date)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))


@app.patch("/api/tracking/funds/{fund_id}")
def update_fund_endpoint(fund_id: int, m: FundUpdateModel, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    fields: dict = {}
    if m.name is not None:
        fields["name"] = m.name
    if m.bucket is not None:
        fields["bucket"] = m.bucket
    if m.monthly_contribution is not None:
        fields["monthly_contribution_cents"] = _cents(m.monthly_contribution)
    if m.clear_target:
        fields["target_cents"] = None
        fields["target_date"] = None
    else:
        if m.target is not None:
            fields["target_cents"] = _cents(m.target)
        if m.target_date is not None:
            fields["target_date"] = m.target_date
    if m.status is not None:
        fields["status"] = m.status
    with closing(tracking_store.connect()) as c:
        try:
            f = tracking_store.update_fund(c, scope, fund_id, **fields)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        if f is None:
            raise HTTPException(status_code=404, detail="fund not found")
        return f


@app.delete("/api/tracking/funds/{fund_id}")
def delete_fund_endpoint(fund_id: int, hard: bool = False, force: bool = False, request: Request = None) -> dict:
    """Archive-by-default (`hard=false`, the default) — always succeeds, idempotent.
    `hard=true` permanently deletes (cascades fund_txn); 409 when the fund's all-time
    reserve is nonzero unless `force=true` is also passed (§4.3 — a hard delete reverts
    past funded draws to raw spend)."""
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        try:
            result = tracking_store.delete_fund(c, scope, fund_id, hard=hard, force=force)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e))
    return {"fundId": fund_id, **result}


@app.post("/api/tracking/funds/{fund_id}/txns")
def link_fund_txn_endpoint(fund_id: int, m: FundLinkModel, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        try:
            return tracking_store.link_fund_txn(c, scope, fund_id, m.txn_id, m.role)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))


@app.delete("/api/tracking/funds/{fund_id}/txns/{txn_id}")
def unlink_fund_txn_endpoint(fund_id: int, txn_id: int, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        try:
            tracking_store.unlink_fund_txn(c, scope, fund_id, txn_id)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    return {"fundId": fund_id, "txnId": txn_id, "unlinked": True}


@app.get("/api/tracking/fund-rollup")
def fund_rollup_endpoint(month: str, includeArchived: bool = False, request: Request = None) -> dict:
    """The fund lens (Phase 1, DEC-034 §5) — per-fund reserve trajectory for `month`,
    mirroring /api/tracking/card-rollup's shape. Purely additive: does not read or write
    aggregate_actuals/plan_vs_actual (Phase 2 folds the funded-draw excusal into the
    headline on-track number via the month_actuals seam — not built here)."""
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        funds = tracking_store.list_funds(c, scope, include_archived=includeArchived)
        out = []
        for f in funds:
            flows = tracking_store.fund_monthly_flows(c, scope, f["id"], upto_month=month)
            rollup = tracking.fund_rollup(flows, upto_month=month)
            history = tracking_store.list_fund_txns(c, scope, f["id"])
            out.append({**f, **rollup, "history": history})
    return {"month": month, "funds": out}


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
def upsert_recurring_endpoint(m: RecurringModel, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        try:
            return tracking_store.upsert_recurring(
                c, scope, m.category, direction=m.direction, bucket=m.bucket,
                due_day=m.due_day, expected_cents=_cents(m.expected), active=m.active)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/tracking/recurring")
def list_recurring_endpoint(request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        return {"recurring": tracking_store.list_recurring(c, scope)}


@app.delete("/api/tracking/recurring/{recurring_id}")
def delete_recurring_endpoint(recurring_id: int, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        tracking_store.delete_recurring(c, scope, recurring_id)
    return {"deleted": recurring_id}


@app.post("/api/tracking/transactions/import")
def import_txns_endpoint(m: ImportModel, request: Request = None) -> dict:
    """Bulk CSV import. Header required; columns case-insensitive, order-independent:
    date,account,direction,amount,bucket,category,description,transfer_group,external_id.
    Valid rows commit; bad rows are reported and skipped (partial success). A member
    imports into THEIR OWN accounts only — the by-name lookup and the numeric-id
    fallback are both restricted to `list_accounts(c, scope, ...)`, so a row cannot
    address another user's account by guessing its id."""
    scope = resolve_user(request)["scopeId"]
    imported, skipped, errors = 0, 0, []
    with closing(tracking_store.connect()) as c:
        owned_accounts = tracking_store.list_accounts(c, scope, include_archived=True)
        accounts = {a["name"].lower(): a["id"] for a in owned_accounts}
        owned_ids = {a["id"] for a in owned_accounts}
        reader = csv.DictReader(io.StringIO(m.csv))
        for i, raw in enumerate(reader, start=2):     # row 1 is the header
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
            try:
                acct_key = row.get("account", "").lower()
                acct_id = accounts.get(acct_key)
                if acct_id is None and acct_key.isdigit() and int(acct_key) in owned_ids:
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
                    c, scope, acct_id, row["date"], direction, _cents(abs(amount)),
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
def upsert_snapshot_endpoint(m: SnapshotModel, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        try:
            tracking_store._require_own_account(c, scope, m.account_id)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
        return tracking_store.upsert_snapshot(c, scope, m.account_id, m.as_of, _cents(m.balance))


@app.get("/api/tracking/snapshots")
def list_snapshots_endpoint(accountId: int | None = None,
                            date_from: str | None = Query(None, alias="from"),
                            date_to: str | None = Query(None, alias="to"),
                            request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        return {"snapshots": tracking_store.list_snapshots(
            c, scope, account_id=accountId, date_from=date_from, date_to=date_to)}


@app.delete("/api/tracking/snapshots/{snapshot_id}")
def delete_snapshot_endpoint(snapshot_id: int, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        tracking_store.delete_snapshot(c, scope, snapshot_id)
    return {"deleted": snapshot_id}


# ----- plan baseline + the headline comparison -----

@app.post("/api/tracking/plan/{month}/lock")
def lock_plan_endpoint(month: str, m: PlanLockModel, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    payload = tracking.build_plan(
        month, bucket_planned=m.bucket_planned, income_planned=m.income_planned,
        savings_rate_planned=m.savings_rate_planned, forecast_cone=m.forecast_cone,
        anchor_date=m.anchor_date, anchor_value=m.anchor_value, engine_version=m.engine_version,
    )
    with closing(tracking_store.connect()) as c:
        return tracking_store.save_plan(c, scope, month, payload, status=m.status, engine_version=m.engine_version)


@app.get("/api/tracking/plan/{month}")
def get_plan_endpoint(month: str, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        plan = tracking_store.get_plan(c, scope, month)
    if plan is None:
        raise HTTPException(status_code=404, detail="no plan for month")
    return plan


@app.get("/api/tracking/plan-vs-actual")
def plan_vs_actual_endpoint(month: str, tol: float = 0.05, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        actuals = tracking_store.month_actuals(c, scope, month)
        plan = tracking_store.get_plan(c, scope, month)
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
def list_scenarios_endpoint(request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        return {"scenarios": tracking_store.list_scenarios(c, scope)}


@app.post("/api/tracking/scenarios")
def create_scenario_endpoint(m: ScenarioCreateModel, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    _scenario_blob_guard(m.spec, "spec")
    with closing(tracking_store.connect()) as c:
        try:
            return tracking_store.create_scenario(c, scope, m.name, m.spec)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/tracking/scenarios/{scenario_id}")
def get_scenario_endpoint(scenario_id: int, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        s = tracking_store.get_scenario(c, scope, scenario_id)
    if s is None:
        raise HTTPException(status_code=404, detail="scenario not found")
    return s


@app.put("/api/tracking/scenarios/{scenario_id}")
def update_scenario_endpoint(scenario_id: int, m: ScenarioUpdateModel, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    _scenario_blob_guard(m.spec, "spec")
    with closing(tracking_store.connect()) as c:
        try:
            s = tracking_store.update_scenario(c, scope, scenario_id, name=m.name, spec=m.spec)
        except tracking_store.ScenarioConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    if s is None:
        raise HTTPException(status_code=404, detail="scenario not found")
    return s


@app.delete("/api/tracking/scenarios/{scenario_id}")
def delete_scenario_endpoint(scenario_id: int, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        try:
            ok = tracking_store.delete_scenario(c, scope, scenario_id)
        except tracking_store.ScenarioConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))
    if not ok:
        raise HTTPException(status_code=404, detail="scenario not found")
    return {"deleted": scenario_id}


@app.post("/api/tracking/scenarios/{scenario_id}/activate")
def activate_scenario_endpoint(scenario_id: int, m: ScenarioActivateModel, request: Request = None) -> dict:
    """Install the scenario from its activation month onward (DEC-017 #5): one
    transaction writing plan snapshots for months >= M through the same machinery
    as /plan/{month}/lock; months < M are never touched (DEC-007). 409 while
    another scenario (of the CALLER's own) is active — revert it first; a different
    user's active scenario never blocks this one (per-user one-active index)."""
    scope = resolve_user(request)["scopeId"]
    _scenario_blob_guard(m.client_state, "clientState")
    plan_months = [pm.model_dump(by_alias=True) for pm in m.plan_months]
    with closing(tracking_store.connect()) as c:
        try:
            out = tracking_store.activate_scenario(
                c, scope, scenario_id, m.activation_month, plan_months, client_state=m.client_state)
        except tracking_store.ScenarioConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    if out is None:
        raise HTTPException(status_code=404, detail="scenario not found")
    return out


@app.post("/api/tracking/scenarios/{scenario_id}/revert")
def revert_scenario_endpoint(scenario_id: int, request: Request = None) -> dict:
    """Exactly undo activation: restore every overwritten plan snapshot, delete the
    ones activation created, flip the scenario back to draft, and hand back the
    opaque clientState so the client restores its budget/Tax config (DEC-017 #6)."""
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        try:
            out = tracking_store.revert_scenario(c, scope, scenario_id)
        except tracking_store.ScenarioConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))
    if out is None:
        raise HTTPException(status_code=404, detail="scenario not found")
    return out


# ----- per-user server profile (S1.2, DEC-027/DEC-035, docs/s1_2-migration-design.md) -----
# Two endpoints, mirroring the scenario blob endpoints above: `with closing(...)`,
# scoped by `resolve_user(request)["scopeId"]`, the same `_scenario_blob_guard` size
# ceiling reused for the blob. Deliberately PER-USER, NOT `require_owner` — every
# household member (owner AND member) reads/writes their own profile; this differs
# from the owner-only export/import surface above.

# Mirrors index.html's `BACKUP_CLIENT_KEYS` verbatim (§2 of the design doc) — the
# blob IS the versioned fps-backup client-section, so this allowlist is the single
# source of truth on both sides. Structural exclusion, not a denylist: `itc.whatif.*`
# and `itc.activetab` are simply never members of this set, so a crafted/corrupt blob
# can never smuggle them in — the PUT validator below rejects any unknown key.
_PROFILE_BACKUP_CLIENT_KEYS = frozenset({
    "itc.v3", "itc.tabs.v1", "budgetBuilder_v1", "itc.fire.v1",
    "itc.categories.v1", "itc.proj.accts.v1", "itc.maxout.v1",
})


class ProfilePutModel(BaseModel):
    blob: dict
    # strict=True (§3.2 "reject bool-as-int"): Pydantic's default lax `int` coercion
    # accepts `True`/`False` as 1/0 (bool is an int subclass) — strict mode is the only
    # way to actually reject a bool value here rather than silently normalizing it.
    base_state_version: int = Field(0, alias="baseStateVersion", strict=True)
    migration: bool = False
    model_config = {"populate_by_name": True}


def _validate_profile_blob(blob) -> None:
    """422, no mutation (mirrors `_validate_backup`'s posture): the blob must be the
    versioned fps-backup client-section shape, `{version:3, keys:{...}}`, and `keys`
    may carry ONLY allowlisted client keys — server-side defense-in-depth mirroring
    the client's own allowlist (§3.2)."""
    if not isinstance(blob, dict):
        raise HTTPException(status_code=422, detail="blob must be an object")
    version = blob.get("version")
    if isinstance(version, bool) or version != 3:
        raise HTTPException(status_code=422, detail="blob.version must be 3")
    keys = blob.get("keys")
    if not isinstance(keys, dict):
        raise HTTPException(status_code=422, detail="blob.keys must be an object")
    unknown = sorted(k for k in keys if k not in _PROFILE_BACKUP_CLIENT_KEYS)
    if unknown:
        raise HTTPException(
            status_code=422, detail=f"blob.keys contains disallowed key(s): {unknown}")


@app.get("/api/tracking/profile")
def get_profile_endpoint(request: Request = None) -> dict:
    """§3.1: always 200. `{hasState:false, stateVersion:0, blob:null, updatedAt:null}`
    when this scope has no row yet — identical code path for owner and member; only the
    scopeId (the table's PK) differs. No existence-leak: a never-seen scope gets this
    exact same shape, never a distinguishable 403/404."""
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        return tracking_store.get_profile(c, scope)


@app.put("/api/tracking/profile")
def put_profile_endpoint(m: ProfilePutModel, request: Request = None) -> dict:
    """§3.2: last-write-wins flush. Always wins — `baseStateVersion` is advisory only in
    v1 (recorded nowhere, never used to refuse a stale write); the blob it displaces (if
    any) moves to the store's one-level `prev_blob`/`prev_state_version` undo.

    500 (no mutation) only when the ONE-TIME server-side pre-migration snapshot's
    `conn.backup()` raises OSError (§5.2) — abort before the upsert, exactly like
    `import_all`'s pre-mutation copy; the client keeps `dirty` and retries next boot.
    """
    scope = resolve_user(request)["scopeId"]
    _validate_profile_blob(m.blob)
    _scenario_blob_guard(m.blob, "blob")
    with closing(tracking_store.connect()) as c:
        try:
            return tracking_store.put_profile(
                c, scope, m.blob, m.base_state_version, is_migration=m.migration)
        except OSError as e:
            raise HTTPException(
                status_code=500, detail=f"pre-migration safety snapshot failed: {e}")


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
def card_rollup_endpoint(month: str, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        accounts = tracking_store.list_accounts(c, scope)
        credit_ids = [a["id"] for a in accounts if a["type"] == "credit"]
        txns = tracking_store.list_txns(
            c, scope,
            date_to=tracking.month_end(month),
            account_ids=credit_ids,
        )
    rollup = tracking.card_rollup_running(txns, accounts, month)
    return {"month": month, "rollup": rollup}


@app.get("/api/tracking/open-pending")
def open_pending_endpoint(month: str, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        accounts = tracking_store.list_accounts(c, scope)
        credit_ids = [a["id"] for a in accounts if a["type"] == "credit"]
        txns = tracking_store.list_txns(
            c, scope,
            status="pending",
            date_before=f"{month}-01",
            account_ids=credit_ids,
        )
    return {"month": month, "txns": txns}


@app.post("/api/tracking/card-payment")
def card_payment_endpoint(m: CardPaymentModel, request: Request = None) -> dict:
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        if _cents(m.amount) <= 0:
            raise HTTPException(status_code=422, detail="amount must be > 0")
        card_acct = tracking_store.get_account(c, scope, m.card_account_id)
        if card_acct is None or card_acct["type"] != "credit":
            raise HTTPException(status_code=422, detail="cardAccountId must be an existing credit account")
        if m.from_account_id is not None:
            if m.from_account_id == m.card_account_id:
                raise HTTPException(status_code=422, detail="fromAccountId must differ from cardAccountId")
            from_acct = tracking_store.get_account(c, scope, m.from_account_id)
            # No type check on from_acct: any account type may fund a card payment by design.
            if from_acct is None:
                raise HTTPException(status_code=422, detail="fromAccountId must be an existing account")
        tg = uuid.uuid4().hex
        try:
            ids = tracking_store.record_card_payment(
                c, scope, m.card_account_id, _cents(m.amount), m.date, tg,
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
def edit_card_payment_endpoint(in_leg_id: int, m: CardPaymentEditModel, request: Request = None) -> dict:
    """Edit the amount and/or earmark bucket on a card-payment transfer-IN leg.

    Full-replace contract: ``applyToCategory`` absent or null clears any existing earmark
    (means "whole card").  Amount is written to both legs of the transfer so the pair
    stays balanced.  The funding account is not changed.

    Returns the updated IN-leg txn dict.
    404 when the id does not exist (or belongs to another user); 422 when the id is not
    a card-payment IN-leg or validation fails (non-positive amount, empty bucket string).
    """
    scope = resolve_user(request)["scopeId"]
    with closing(tracking_store.connect()) as c:
        try:
            txn = tracking_store.update_card_payment(
                c, scope, in_leg_id,
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
def export_backup_endpoint(request: Request) -> JSONResponse:
    """Dump the entire actuals DB as a JSON backup file (Content-Disposition: attachment).

    Values are raw integers (cents), not dollars — import_all restores them verbatim.
    Owner-only (DEC-031 S0.2): this backs every backup-popup download scope that
    reaches the server (full + actuals-only both call this endpoint; client-only never
    calls the server at all — see server.py's DEC-031 report). Members get 403.

    S1.1: NOT scoped to the caller — every user-owned table now carries `user_id`, so
    this dump is explicitly the WHOLE HOUSEHOLD's data (envelope `scope:"household-full"`,
    filename `household-backup-*`), never just the owner's. The per-user backup slice
    (DEC-028) is deferred — see docs/multiuser-household-plan.md S1.1 §4c.
    """
    require_owner(request)
    with closing(tracking_store.connect()) as c:
        payload = tracking_store.export_all(c)
    stamp = payload["exportedAt"].replace(":", "").replace("-", "")
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="household-backup-{stamp}.json"'},
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
    fails (no mutation occurred). Owner-only (DEC-031 S0.2) — members get 403 before
    the body is even read.

    S1.1: full-DB REPLACE, NOT scoped — restores every household member's rows. A
    pre-S1.1 backup (no `user_id` in any row) restores correctly for free: the allow-
    list INSERT simply omits the absent column, and the `NOT NULL DEFAULT '__owner__'`
    assigns every restored legacy row to the owner (single-tenant data always was
    owner data).
    """
    require_owner(request)
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
def export_txns_csv_endpoint(request: Request,
                              date_from: str | None = Query(None, alias="from"),
                              date_to: str | None = Query(None, alias="to")) -> Response:
    """Date-ranged transactions CSV for analysis/tax-prep (Content-Disposition: attachment).

    `from`/`to` are optional inclusive ISO date (YYYY-MM-DD) bounds on posted_on; either or
    both may be omitted for an unbounded side. Plainly a spreadsheet, not a restorable
    backup — see tracking_store.export_txns_csv for the column contract. 422 for a
    malformed date or from > to. Owner-only (DEC-031 S0.2 — this is an exfiltration
    surface): members get 403.

    S1.1: scoped to the OWNER's own transactions (`scopeId` — always `__owner__` since
    only the owner may call this) — their personal tax-prep export, not a household dump.
    """
    owner = require_owner(request)
    with closing(tracking_store.connect()) as c:
        try:
            csv_text = tracking_store.export_txns_csv(c, owner["scopeId"], date_from, date_to)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    filename = f"transactions-{date_from or 'all'}_{date_to or 'all'}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ----- identity self-check (multi-user S0.1, DEC-026/031) -----

@app.get("/api/whoami")
def whoami_endpoint(request: Request) -> dict:
    """On-device identity verification (docs/multiuser-household-plan.md S0.1 exit
    criteria). Always resolves via resolve_user() — never reads the ingress header
    directly. The owner sees the full picture (their own id/role plus the provisioned
    household roster); a member sees ONLY their own id and role — never who else is
    in the household.

    Human-readable names: every roster entry in `users` (owner included) carries
    `displayName` (header-captured, may be None) and `label` (owner-edited via PATCH
    /api/tracking/users/{id}, may be None) — render-time precedence is `label ||
    displayName || id` (index.html's owner-transfer picker). The caller's own
    top-level entry gains the same two fields, whichever branch they're in, so a
    member who never sees the roster can still learn their own name.

    ACCOUNT LINKING: every caller (owner or member) also gets `linkedAccounts` —
    the accounts currently aliased to THEIR OWN resolved persona (server.py's
    resolve_user() already collapsed the caller's own identity through any alias of
    its own before this ever runs, so `user["id"]` here is always a primary). This is
    always self-scoped, unlike `users` — a member sees their own linkedAccounts just
    like the owner sees theirs, never anyone else's. The owner-only `users` roster
    array no longer lists ids that are currently aliased to something (see
    tracking_store.list_users()'s docstring) — they're reachable only via
    `linkedAccounts` now, not as independent roster rows."""
    user = resolve_user(request)
    with closing(tracking_store.connect()) as c:
        linked_accounts = tracking_store.list_linked_accounts(c, user["id"])
        if user["role"] == "owner":
            users = tracking_store.list_users(c)
            own = next((u for u in users if u["id"] == user["id"]), None)
            return {
                "id": user["id"],
                "role": user["role"],
                "displayName": own["displayName"] if own else None,
                "label": own["label"] if own else None,
                "users": users,
                "linkedAccounts": linked_accounts,
            }
        own = tracking_store.get_user(c, user["id"])
    return {
        "id": user["id"],
        "role": user["role"],
        "displayName": own["displayName"] if own else None,
        "label": own["label"] if own else None,
        "linkedAccounts": linked_accounts,
    }


# ----- owner transfer (SEV-004 follow-up, addon/DOCS.md; 0.2.1 explicitly deferred this) -----

class OwnerTransferModel(BaseModel):
    to_user_id: str = Field(..., alias="toUserId")
    model_config = {"populate_by_name": True}


@app.post("/api/tracking/owner-transfer")
def owner_transfer_endpoint(m: OwnerTransferModel, request: Request) -> dict:
    """Hand the household owner seat to another already-provisioned member. Owner-only
    (require_owner) -- a member gets 403 before this ever runs.

    ZERO DATA MOVEMENT: owner-scoped data (every `/api/tracking/*` table) is keyed to the
    `__owner__` scopeId sentinel, never to a raw HA user id (resolve_user()'s scopeId
    formula, DEC-033). Promoting `toUserId` to `role='owner'` means their VERY NEXT
    request resolves `scopeId = '__owner__'` -- they immediately see every transaction,
    account, fund, and synced profile the old owner had, with zero rows copied or moved.
    Full mechanics + the atomicity guarantee (single transaction; current owner demoted
    before the target is promoted) live in tracking_store.transfer_ownership's docstring.

    ORPHANED DATA (documented, not merged -- see addon/DOCS.md): if `toUserId` had
    already logged data as a member (their own transactions, their own synced profile),
    it stays under their raw id, unreachable while they hold the owner seat -- it
    reappears only if they're later demoted back to member. No merge tooling exists.

    Body: ``{"toUserId": "<id>"}``. 400 if `toUserId` is the current owner (no-op) or the
    reserved `__owner__` sentinel. 404 if `toUserId` has never opened the app (must be
    lazily provisioned first, DEC-026/031). 200 ``{"previousOwnerId", "newOwnerId"}`` on
    success.
    """
    owner = require_owner(request)
    with closing(tracking_store.connect()) as c:
        try:
            return tracking_store.transfer_ownership(c, owner["id"], m.to_user_id)
        except tracking_store.OwnerTransferError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except tracking_store.UnknownTransferTargetError as e:
            raise HTTPException(status_code=404, detail=str(e))
        except tracking_store.OwnerTransferConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))


# ----- owner-editable roster label (human-readable household roster names) -----

_USER_LABEL_MAX_LEN = 64


class UserLabelModel(BaseModel):
    label: str = Field(...)


@app.patch("/api/tracking/users/{user_id}")
def set_user_label_endpoint(user_id: str, m: UserLabelModel, request: Request) -> dict:
    """Owner-editable fallback name for a household member -- covers Supervisor
    versions that never send X-Remote-User-Display-Name/-Name (pre-2023.08.2) or
    service accounts, where resolve_user()'s header capture has nothing to work with.
    `label` wins over the header-captured `displayName` at render time (index.html's
    owner-transfer picker: label || displayName || id).

    Owner-only (require_owner) -- a member gets 403 before this ever runs, same
    posture as every other roster-mutating control.

    Body: ``{"label": "..."}``. The value is stripped server-side; an empty (or
    all-whitespace) string CLEARS the label (stored as NULL), matching "leave it
    blank to fall back to the header name / raw id." 422 if the stripped value
    exceeds `_USER_LABEL_MAX_LEN` characters. 400 if `user_id` is the reserved
    `__owner__` data-scope sentinel (SEV-S1.1-001) -- never a real household member.
    404 if `user_id` has never been provisioned (must open the app at least once
    first, DEC-026/031). 200 ``{"id", "label"}`` on success.
    """
    require_owner(request)
    stripped = m.label.strip()
    if len(stripped) > _USER_LABEL_MAX_LEN:
        raise HTTPException(
            status_code=422,
            detail=f"label must be {_USER_LABEL_MAX_LEN} characters or fewer",
        )
    with closing(tracking_store.connect()) as c:
        try:
            return tracking_store.set_user_label(c, user_id, stripped or None)
        except tracking_store.UserLabelError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except tracking_store.UnknownUserError as e:
            raise HTTPException(status_code=404, detail=str(e))


# ----- account linking (identity aliases -- "appoint admins" via linking) -----
#
# User requirement (condensed): "I want both my admin accounts in HA to be the same
# profile because both are me, but I want other users (dashboard and my partner) to
# have their dedicated instance." I.e. N HA accounts -> 1 profile (persona), opt-in
# per account via a two-sided link-code handshake; unlinked accounts stay dedicated
# personas. Every account linked to a persona shares that persona's role wholesale --
# linking the owner's second login to their primary IS how "appoint an admin" falls
# out of this design, with no separate admin-grant concept. NO new HA API surface is
# introduced (DEC-021) -- this is entirely a `users`/`user_alias`/`link_code` table
# affair behind the existing ingress-authenticated identity resolver.

class LinkCodeRedeemModel(BaseModel):
    code: str = Field(...)


@app.post("/api/tracking/link-code")
def create_link_code_endpoint(request: Request) -> dict:
    """Issue a single-use, 10-minute account-linking code for the CALLER's own persona.
    ANY signed-in persona may call this -- owner or member alike; there is no
    `require_owner` gate, because linking is "make another one of MY OWN accounts share
    this profile," not a household-admin action on someone else. Response:
    ``{"code": "<8 chars>", "expiresAt": "<iso8601>"}``.

    THREAT MODEL: the plaintext code is the ONLY secret in this handshake -- possessing
    it, within its 10-minute TTL, is treated as proof "this browser also controls the
    issuer's account," without ever comparing HA credentials directly. It is generated
    with `secrets.choice` over a 40-bit-entropy typo-resistant alphabet (no `0`/`O`/`1`/
    `I`) and only its SHA-256 hash is ever persisted (tracking_store.create_link_code) --
    a stolen DB file, WAL, or `/api/tracking/export` backup can never recover a still-
    live code, and the plaintext itself is returned exactly once, in this response body,
    never logged. Because redemption GRANTS THE JOINER WHATEVER ROLE THE ISSUER'S
    PRIMARY HOLDS -- immediately, including owner rights if the issuer is (or later
    becomes) the household owner -- a code intercepted in flight during its 10-minute
    window is a genuine privilege-escalation vector. Mitigated by: (a) ingress-only
    exposure, no direct port (DEC-021), (b) single-use (redemption invalidates it
    immediately -- tracking_store.redeem_link_code marks `used=1`), (c) the short TTL,
    and (d) generating a new code deletes the issuer's prior outstanding one first
    (bounds the live-code surface to at most one per issuer at a time -- rate-limit
    sanity, not a formal rate limiter). There is deliberately no server-side notification
    to the issuer when their code is redeemed (v1) -- the issuer must be present on their
    OTHER device to type the code there in the first place, which is itself the
    real-world proof of intent this whole flow is modeling.
    """
    user = resolve_user(request)
    with closing(tracking_store.connect()) as c:
        return tracking_store.create_link_code(c, user["id"])


@app.post("/api/tracking/link")
def redeem_link_code_endpoint(m: LinkCodeRedeemModel, request: Request) -> dict:
    """Redeem a link code, called from the JOINING account's OWN session. The endpoint
    operates purely on the caller's own resolved identity (`resolve_user(request)`),
    which is always a PRIMARY by construction -- resolve_user() already collapsed any
    pre-existing alias of the caller's own before this handler ever runs. On success the
    caller's id becomes an alias of the code's issuer's primary; see
    tracking_store.redeem_link_code's docstring for the full mechanics, orphan-data
    semantics, and no-chain proof. Body: ``{"code": "..."}``. Success 200:
    ``{"aliasId", "primaryUserId"}``.

    THREAT MODEL: fail-closed on every validated axis, checked inside
    tracking_store.redeem_link_code in this order:
      - **Bad code (400):** unknown, expired, or already-used codes are looked up by
        hash only and return ONE generic message -- there is no oracle that lets an
        attacker distinguish "never existed," "expired," or "already redeemed," which
        would otherwise leak information useful for guessing or timing attacks against
        the (already large, 40-bit) code space.
      - **Self-link (400):** joiner == issuer is rejected outright -- a no-op that would
        otherwise silently succeed and clutter the alias table.
      - **Owner-seat holder as joiner (409):** if the caller currently holds the
        household owner seat, linking them away from it would strand
        `scopeId='__owner__'` -- nobody would resolve to it until a future
        owner-transfer, an availability bug dressed as a feature. The caller is told the
        fix is directional: issue the code from the owner account instead (making the
        owner the primary), or transfer the seat first.
      - **No-chain (409):** if the caller (joiner) is ALREADY a primary for one or more
        existing aliases, linking it to a NEW primary would leave those existing aliases
        silently re-parented onto a THIRD party through a two-hop chain -- structurally
        forbidden (an alias's primary must never itself be an alias). This is the one
        check that most directly defends the "N accounts -> 1 profile" invariant this
        entire feature exists to provide: without it, profile identity could drift
        transitively instead of staying a flat, auditable one-hop mapping.
    """
    joiner = resolve_user(request)
    # SEV-003 (2026-07-23 audit): per-caller failed-redeem throttle. In-memory is fine
    # (single uvicorn process); 10 failures / 10 min -> 429. A success clears the slate.
    now = time.monotonic()
    window = _LINK_REDEEM_FAILURES.setdefault(joiner["id"], [])
    window[:] = [t for t in window if now - t < 600]
    if len(window) >= 10:
        raise HTTPException(status_code=429,
                            detail="too many failed link attempts -- try again later")
    with closing(tracking_store.connect()) as c:
        try:
            result = tracking_store.redeem_link_code(c, joiner["id"], m.code)
            _LINK_REDEEM_FAILURES.pop(joiner["id"], None)
            return result
        except tracking_store.LinkError as e:
            window.append(now)
            raise HTTPException(status_code=400, detail=str(e))
        except tracking_store.LinkOwnerSeatConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))
        except tracking_store.LinkChainConflictError as e:
            raise HTTPException(status_code=409, detail=str(e))


@app.delete("/api/tracking/link/{alias_id}")
def unlink_account_endpoint(alias_id: str, request: Request) -> dict:
    """Remove a link. Callable by the persona that owns it, from ANY of its currently-
    linked sessions -- the primary's own session, or a DIFFERENT alias of the same
    persona -- because `resolve_user(request)` has already collapsed the CALLER's own
    identity through any alias of its own before this ever runs, so `user["id"]` here is
    always the owning persona's id regardless of which physical HA login made the
    request. Success 200: ``{"aliasId", "unlinkedFrom"}``.

    THREAT MODEL: ownership is checked by RESOLVED identity, not by trusting the caller
    to only ever pass their own alias ids -- `tracking_store.unlink_alias` independently
    verifies `caller_id == user_alias.primary_user_id` before deleting anything, so one
    persona can never unlink an alias belonging to a DIFFERENT persona by guessing or
    enumerating alias ids (403, `AliasNotOwnedError`), and unlinking a never-linked or
    already-unlinked id 404s rather than silently no-op-succeeding (`UnknownAliasError`)
    -- both distinguishable failure modes are safe to expose here (unlike the redeem
    path above) because they only ever reveal information about links the CALLER already
    has some relationship to (either they own it, or they guessed a nonexistent id --
    neither leaks another persona's link graph). After removal, `alias_id` reverts to
    resolving as its own persona on its very next request -- its pre-link data (never
    deleted, only orphaned by the original link) becomes reachable again.
    """
    user = resolve_user(request)
    with closing(tracking_store.connect()) as c:
        try:
            return tracking_store.unlink_alias(c, user["id"], alias_id)
        except (tracking_store.UnknownAliasError, tracking_store.AliasNotOwnedError):
            # SEV-005 (2026-07-23 audit): not-owned vs not-found must be
            # indistinguishable — same status AND same message — or a member can
            # enumerate which ids are currently aliases.
            raise HTTPException(status_code=404, detail="no such linked account.")
