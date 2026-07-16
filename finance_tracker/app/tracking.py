"""Plan-vs-actual comparison — PURE logic, no I/O.

The persistence (SQLite) lives in `tracking_store.py`; the HTTP edge lives in
`server.py`. This module is a function of (plan, actuals) → comparison dict, so it
stays as testable as the rest of the codebase (`calculator.py`, `budgeting.py`, …).

Money convention: the store speaks **integer cents** (exact ledger); these functions
take cents in the raw rows and emit **float dollars** in every output dict, matching
the engines and the frontend. All output dicts are camelCase / JSON-ready.

See docs/plan-vs-actual-design.md and DEC-006/007/008.
"""

from __future__ import annotations

import calendar
from datetime import date

# The five budget buckets (must match the budget builder's KINDS / bucket taxonomy).
BUCKETS: tuple[str, ...] = ("need", "want", "investment", "travel", "other")
# Spend buckets: under budget is good. "investment": at/over target is good.
_SPEND_BUCKETS = ("need", "want", "travel", "other")

_DAYS_PER_YEAR = 365.25


# ---------- small helpers ----------

def _d(cents: int | float) -> float:
    """Integer cents → float dollars, rounded to the cent."""
    return round(cents / 100.0, 2)


def month_end(month: str) -> str:
    """'YYYY-MM' → ISO date of the last day of that month ('YYYY-MM-DD')."""
    y, m = (int(x) for x in month.split("-"))
    return f"{month}-{calendar.monthrange(y, m)[1]:02d}"


def _years_between(d0: str, d1: str) -> float:
    """Signed years from ISO date d0 to d1 (d1 before d0 → negative)."""
    return (date.fromisoformat(d1) - date.fromisoformat(d0)).days / _DAYS_PER_YEAR


def _interp_cone(forecast: list[dict], t: float, key: str) -> float | None:
    """Linear interpolation of a cone series at elapsed-years t, between annual points.

    Matches how the frontend draws the cone (straight polylines between annual points),
    so 'ahead/behind the plan' is consistent with the chart. Clamps outside the range.
    """
    if not forecast:
        return None
    pts = sorted(forecast, key=lambda p: p["year"])
    if t <= pts[0]["year"]:
        return float(pts[0][key])
    if t >= pts[-1]["year"]:
        return float(pts[-1][key])
    for i in range(1, len(pts)):
        if t <= pts[i]["year"]:
            p0, p1 = pts[i - 1], pts[i]
            span = (p1["year"] - p0["year"]) or 1
            frac = (t - p0["year"]) / span
            return float(p0[key]) + frac * (float(p1[key]) - float(p0[key]))
    return float(pts[-1][key])


# ---------- net worth from snapshots ----------

def _net_worth_at(snapshot_rows: list[dict], liability: dict[int, bool], as_of: str) -> int:
    """Net worth in CENTS at date `as_of` = Σ_account (latest snapshot with as_of ≤ date)
    × (liability ? −1 : +1). 'Latest ≤ date per account' so a stale account isn't zeroed."""
    latest: dict[int, tuple[str, int]] = {}
    for s in snapshot_rows:
        if s["as_of"] <= as_of:
            cur = latest.get(s["account_id"])
            if cur is None or s["as_of"] > cur[0]:
                latest[s["account_id"]] = (s["as_of"], s["balance_cents"])
    total = 0
    for acct, (_, bal) in latest.items():
        total += (-bal if liability.get(acct) else bal)
    return total


def _net_worth_series(snapshot_rows: list[dict], liability: dict[int, bool], upto: str) -> list[dict]:
    """Real net-worth trajectory: one {date, value$} per distinct snapshot date ≤ `upto`."""
    dates = sorted({s["as_of"] for s in snapshot_rows if s["as_of"] <= upto})
    return [{"date": d, "value": _d(_net_worth_at(snapshot_rows, liability, d))} for d in dates]


# ---------- aggregation (rows → actuals) ----------

def aggregate_actuals(
    txn_rows: list[dict],
    snapshot_rows: list[dict],
    account_liability: dict[int, bool],
    month: str,
) -> dict:
    """Pure rollup of fetched rows → the ACTUALS dict (float dollars).

    txn_rows: the month's transactions; each {posted_on, direction 'in'|'out',
        amount_cents, bucket|None, is_transfer (0/1), account_id}.
    snapshot_rows: ALL balance snapshots (the net-worth series spans history, not just
        the month); each {account_id, as_of, balance_cents}.
    account_liability: {account_id: bool} — True subtracts from net worth.
    """
    me = month_end(month)
    # Zero-fill the 5 known defaults so output shape is stable; custom buckets are added
    # dynamically as they appear in the data — this is the definition-less aggregation.
    buckets: dict[str, int] = {b: 0 for b in BUCKETS}
    uncategorized = 0
    income = 0
    for t in txn_rows:
        if t.get("is_transfer"):
            continue                      # own-account move — not income, not spend
        if not str(t["posted_on"]).startswith(month):
            continue                      # store usually pre-filters; belt-and-suspenders
        if t["direction"] == "in":
            income += t["amount_cents"]
        else:
            b = t.get("bucket")
            if b and str(b).strip():      # any non-empty bucket string is valid
                if b not in buckets:
                    buckets[b] = 0
                buckets[b] += t["amount_cents"]
            else:
                uncategorized += t["amount_cents"]
    return {
        "month": month,
        "buckets": {b: _d(buckets[b]) for b in buckets},
        "uncategorized": _d(uncategorized),
        "income": _d(income),
        "asOfMonthEnd": _d(_net_worth_at(snapshot_rows, account_liability, me)),
        "netWorthSnapshots": _net_worth_series(snapshot_rows, account_liability, me),
    }


# ---------- plan baseline ----------

def build_plan(
    month: str,
    *,
    bucket_planned: dict[str, float],
    income_planned: float,
    savings_rate_planned: float,
    forecast_cone: list[dict],
    anchor_date: str,
    anchor_value: float,
    engine_version: str = "1.0",
) -> dict:
    """Assemble the frozen plan baseline from the existing engines' outputs (the caller
    runs calculator/budgeting/investing and passes the derived figures — this function
    does NOT import or run them, keeping it pure). Stored as plan_snapshot.payload_json.
    """
    # Store all keys from bucket_planned (not just the 5 defaults) so custom planned
    # buckets survive the round-trip and appear in plan_vs_actual.
    all_plan_keys = sorted(set(BUCKETS) | set(bucket_planned.keys()))
    return {
        "month": month,
        "engineVersion": engine_version,
        "buckets": {b: {"planned": round(float(bucket_planned.get(b, 0.0)), 2)} for b in all_plan_keys},
        "income": {"planned": round(float(income_planned), 2)},
        "savingsRate": {"planned": round(float(savings_rate_planned), 4)},
        "netWorth": {
            "anchorDate": anchor_date,
            "anchorValue": round(float(anchor_value), 2),
            "forecast": forecast_cone,
        },
    }


# ---------- the headline comparison ----------

def _compare_bucket(bucket: str, planned: float, actual: float, tol: float) -> dict:
    planned = round(float(planned), 2)
    actual = round(float(actual), 2)
    pct = round(actual / planned * 100, 1) if planned > 0 else None
    if bucket == "investment":
        on_track = actual >= planned * (1 - tol)          # at/above target is good
    else:
        on_track = actual <= planned * (1 + tol)          # at/under budget is good
    return {
        "planned": planned,
        "actual": actual,
        "variance": round(actual - planned, 2),
        "pctUsed": pct,
        "onTrack": on_track,
    }


def plan_vs_actual(plan: dict, actuals: dict, month: str, tol: float = 0.05) -> dict:
    """The primary dashboard payload. Pure arithmetic + cone interpolation; no engines.

    `plan` is a build_plan() dict (or a frozen plan_snapshot payload); `actuals` is an
    aggregate_actuals() dict for the same month. `tol` is the on-track tolerance band.
    """
    p_buckets = plan.get("buckets", {})
    a_buckets = actuals.get("buckets", {})

    # Union of all known buckets: 5 defaults + any from the plan + any from actuals.
    # "uncategorized" is handled below by its own special branch, not iterated here.
    all_b = sorted(
        (set(BUCKETS) | set(p_buckets.keys()) | set(a_buckets.keys())) - {"uncategorized"}
    )

    out_buckets: dict[str, dict] = {}
    for b in all_b:
        planned = p_buckets.get(b, {}).get("planned", 0.0)
        out_buckets[b] = _compare_bucket(b, planned, a_buckets.get(b, 0.0), tol)

    unc = round(float(actuals.get("uncategorized", 0.0)), 2)
    if unc:
        out_buckets["uncategorized"] = {
            "planned": 0.0, "actual": unc, "variance": unc, "pctUsed": None, "onTrack": False,
        }

    income_planned = round(float(plan.get("income", {}).get("planned", 0.0)), 2)
    income_actual = round(float(actuals.get("income", 0.0)), 2)
    income = {
        "planned": income_planned,
        "actual": income_actual,
        "variance": round(income_actual - income_planned, 2),
    }

    invested = a_buckets.get("investment", 0.0)
    sr_actual = round(invested / income_actual, 4) if income_actual > 0 else 0.0
    savings_rate = {
        "planned": round(float(plan.get("savingsRate", {}).get("planned", 0.0)), 4),
        "actual": sr_actual,
    }

    # ----- net worth: frozen cone + real overlay + delta/within-band for the latest dot -----
    forecast = plan.get("netWorth", {}).get("forecast", []) or []
    anchor_date = plan.get("netWorth", {}).get("anchorDate")
    real = actuals.get("netWorthSnapshots", [])
    delta_vs_mid = None
    within_band = None
    if real and forecast and anchor_date:
        latest = real[-1]
        t = _years_between(anchor_date, latest["date"])
        mid = _interp_cone(forecast, t, "mid")
        low = _interp_cone(forecast, t, "low")
        high = _interp_cone(forecast, t, "high")
        if mid is not None:
            delta_vs_mid = round(latest["value"] - mid, 2)
        if low is not None and high is not None:
            within_band = low <= latest["value"] <= high
    net_worth = {
        "asOfMonthEnd": actuals.get("asOfMonthEnd"),
        "realSnapshots": real,
        "forecast": forecast,
        "deltaVsMid": delta_vs_mid,
        "withinBand": within_band,
    }

    # ----- totals (all buckets in the union + uncategorized; income excluded) -----
    planned_outflow = round(sum(p_buckets.get(b, {}).get("planned", 0.0) for b in all_b), 2)
    actual_outflow = round(sum(a_buckets.get(b, 0.0) for b in all_b) + unc, 2)
    totals = {
        "plannedOutflow": planned_outflow,
        "actualOutflow": actual_outflow,
        "variance": round(actual_outflow - planned_outflow, 2),
        "pctUsed": round(actual_outflow / planned_outflow * 100, 1) if planned_outflow > 0 else None,
        "onTrack": actual_outflow <= planned_outflow * (1 + tol),
    }

    return {
        "month": month,
        "buckets": out_buckets,
        "income": income,
        "savingsRate": savings_rate,
        "netWorth": net_worth,
        "totals": totals,
        "asOf": month_end(month),
    }


# ---------- per-card rollup ----------

def card_rollup(txn_rows: list[dict], accounts: list[dict]) -> dict:
    """Per-credit-card payment rollup. Pure (no I/O), float dollars at the edge.

    Credit accounts appear in perAccount if they have EITHER at least one participating
    out-txn (charge/refund) OR at least one settled transfer-in (card payment) this month.

    Parameters
    ----------
    txn_rows : list[dict]
        The month's transactions as returned by tracking_store.list_txns —
        camelCase dicts: accountId (int), amount (float $), direction ('in'/'out'),
        bucket (str|None), status ('settled'/'pending'), kind ('charge'/'refund'),
        isTransfer (bool), splits (list of {bucket, category, amount}).
    accounts : list[dict]
        Accounts from tracking_store.list_accounts. Only type=='credit' rows are used.

    Returns
    -------
    dict
        perAccount  : {str(accountId): {accountId, name, byBucket, uncategorized,
                       payNow, pending, total, paid, remaining, credit}} for each
                      credit account with participating charges or settled payments.
        buckets     : sorted list of all bucket strings used across all included
                      accounts (stable matrix columns; 'uncategorized' is not a column).
        grandTotal  : {byBucket, uncategorized, payNow, pending, total,
                       paid, remaining, credit}.

    Cent-exact invariants (held because all arithmetic is integer-cents internally;
    _d() is called once per output field):
        round(payNow + pending, 2) == total
        round(sum(byBucket.values()) + uncategorized, 2) == total
        round(payNow + credit, 2) == round(paid + remaining, 2)  (per-account and grandTotal)
        at most one of {remaining, credit} is non-zero  (per-account only; grandTotal fields
            are sums of per-account values, so both can be non-zero simultaneously when
            one account is overpaid and another is underpaid)
    """
    # Build credit-account lookup: id (int) → account dict
    credit: dict[int, dict] = {a["id"]: a for a in accounts if a["type"] == "credit"}

    # Per-account integer-cent tallies
    # {acct_id: {"by": {bucket_str: cents}, "unc": cents, "pay": cents, "pend": cents}}
    acct_cents: dict[int, dict] = {}
    acct_paid: dict[int, int] = {}         # total settled card-payment cents per account
    acct_earmarked_paid: dict[int, dict[str, int]] = {}   # {acct_id: {bucket: cents}}
    acct_general_paid: dict[int, int] = {}                # unearmarked payment cents per account

    def _accum(acct_id: int, signed_c: int, bucket: str | None, status: str) -> None:
        """Add one signed-cent value into the account's running tallies."""
        if acct_id not in acct_cents:
            acct_cents[acct_id] = {"by": {}, "unc": 0, "pay": 0, "pend": 0}
        d = acct_cents[acct_id]
        if bucket:
            d["by"][bucket] = d["by"].get(bucket, 0) + signed_c
        else:
            d["unc"] += signed_c
        if status == "settled":
            d["pay"] += signed_c
        else:
            d["pend"] += signed_c

    for row in txn_rows:
        acct_id = row["accountId"]
        if row.get("isTransfer"):
            # Intercept settled inbound transfers to credit accounts — these are card payments.
            if row["direction"] == "in" and row.get("status") == "settled" and acct_id in credit:
                amt_c = round(row["amount"] * 100)
                acct_paid[acct_id] = acct_paid.get(acct_id, 0) + amt_c
                b = row.get("bucket")
                if b is not None and not str(b).strip():
                    b = None
                if b:
                    ep = acct_earmarked_paid.setdefault(acct_id, {})
                    ep[b] = ep.get(b, 0) + amt_c
                else:
                    acct_general_paid[acct_id] = acct_general_paid.get(acct_id, 0) + amt_c
            continue
        if row["direction"] != "out":
            continue                         # income / partner paybacks — irrelevant
        if acct_id not in credit:
            continue                         # non-credit account (checking, savings, …)

        status = row["status"]               # 'settled' | 'pending'
        splits = row.get("splits") or []

        if splits:
            # Split parent: expand into legs; parent status applies to all legs.
            # Refunds are never split (spec), so legs are always positive charges.
            for leg in splits:
                leg_c = round(leg["amount"] * 100)
                b = leg.get("bucket")
                if b is not None and not str(b).strip():
                    b = None                 # leg with empty bucket → uncategorized
                _accum(acct_id, leg_c, b, status)
        else:
            amount_c = round(row["amount"] * 100)
            signed_c = -amount_c if row["kind"] == "refund" else amount_c
            b = row.get("bucket")
            if b is not None and not str(b).strip():
                b = None                     # empty bucket → uncategorized
            _accum(acct_id, signed_c, b, status)

    # Collect all bucket names actually used (for the stable matrix-column list).
    # Also union earmarked-payment buckets so an overpay on a zero-charge bucket
    # surfaces as a column (the bucket has no charges but has earmarked payments).
    all_buckets: set[str] = set()
    for d in acct_cents.values():
        all_buckets.update(d["by"].keys())
    for ep in acct_earmarked_paid.values():
        all_buckets.update(ep.keys())

    # Build per-account output (accounts with charges OR settled payments)
    per_account: dict[str, dict] = {}
    gt_paid = gt_remaining = gt_credit = 0
    for acct_id in set(acct_cents) | set(acct_paid):
        acct = credit[acct_id]
        d = acct_cents.get(acct_id, {"by": {}, "unc": 0, "pay": 0, "pend": 0})
        pay_c = d["pay"]
        paid_c = acct_paid.get(acct_id, 0)
        remaining_c = max(0, pay_c - paid_c)
        credit_c = max(0, paid_c - pay_c)
        per_account[str(acct_id)] = {
            "accountId": acct_id,
            "name": acct["name"],
            "byBucket": {b: _d(c) for b, c in d["by"].items()},
            "uncategorized": _d(d["unc"]),
            "payNow": _d(d["pay"]),
            "pending": _d(d["pend"]),
            "total": _d(d["pay"] + d["pend"]),  # sum cents THEN convert — one rounding
            "paid": _d(paid_c),
            "remaining": _d(remaining_c),
            "credit": _d(credit_c),
            "earmarkedPaid": {b: _d(c) for b, c in acct_earmarked_paid.get(acct_id, {}).items()},
            "generalPaid":   _d(acct_general_paid.get(acct_id, 0)),
        }
        gt_paid += paid_c
        gt_remaining += remaining_c
        gt_credit += credit_c

    # Grand total: sum integer cents across all accounts, convert once per field
    gt_by: dict[str, int] = {}
    gt_unc = gt_pay = gt_pend = 0
    for d in acct_cents.values():
        for b, c in d["by"].items():
            gt_by[b] = gt_by.get(b, 0) + c
        gt_unc += d["unc"]
        gt_pay += d["pay"]
        gt_pend += d["pend"]

    # Accumulate grand-total earmark tallies in integer cents
    gt_earmarked: dict[str, int] = {}
    gt_general = 0
    for ep in acct_earmarked_paid.values():
        for b, c in ep.items():
            gt_earmarked[b] = gt_earmarked.get(b, 0) + c
    for c in acct_general_paid.values():
        gt_general += c

    grand_total: dict = {
        "byBucket": {b: _d(c) for b, c in gt_by.items()},
        "uncategorized": _d(gt_unc),
        "payNow": _d(gt_pay),
        "pending": _d(gt_pend),
        "total": _d(gt_pay + gt_pend),
        "paid": _d(gt_paid),
        "remaining": _d(gt_remaining),
        "credit": _d(gt_credit),
        "earmarkedPaid": {b: _d(c) for b, c in gt_earmarked.items()},
        "generalPaid":   _d(gt_general),
    }

    return {
        "perAccount": per_account,
        "buckets": sorted(all_buckets),
        "grandTotal": grand_total,
    }


def card_rollup_running(txn_rows: list[dict], accounts: list[dict], month: str) -> dict:
    """Running-balance card rollup: current-month fields PLUS cumulative running fields.

    Extends :func:`card_rollup` by computing four slices of ``txn_rows`` (which must
    cover the full credit-account history up to ``month``-end, inclusive) and merging
    their outputs through four ``card_rollup`` calls.  No refund/split/payment logic is
    duplicated — everything stays inside ``card_rollup``.

    Slices
    ------
    cur          rows where ``postedOn`` startswith ``month``
                 → month-scoped fields, byte-identical to old ``card_rollup(month_rows)``
    settled_full rows where ``status == 'settled'`` OR ``isTransfer`` is true
                 → settled-only byBucket / payNow (→ byBucketOwed, runningTotal)
                 Pending charges are excluded so running owed reflects SETTLED net only.
                 Transfer rows (card payments) are kept because they are always settled
                 and must still credit ``acct_paid`` inside card_rollup.
    full         all rows (settled + pending = txn_rows itself)
                 → runningPending (pending sub-total) and cumulativePaid (total paid)
    prior        rows where ``postedOn < month-start``
                 → per-account remaining before this month (→ carriedIn)

    Why settled-only for owed
    -------------------------
    ``card_rollup`` accumulates *all* (settled + pending) rows into ``byBucket``/``total``;
    ``payNow`` is its settled-only sub-total.  The spec requires running owed and
    ``byBucketOwed`` to be SETTLED net charges only (pending stays separate as "Coming").
    Passing a settled-only slice to a dedicated ``card_rollup`` call is the cleanest path:
    it reuses the refund/split/payment logic without modification.

    Reconciliation invariants (assertable by QA)
    --------------------------------------------
    * ``cur`` per-account + grandTotal fields are byte-identical to
      ``card_rollup(cur_rows, accounts)`` — month numbers never change.
    * Per-account: ``runningRemaining = max(0, runningTotal - cumulativePaid)`` and
      ``runningCredit = max(0, cumulativePaid - runningTotal)``; exactly one is non-zero.
    * INV-2 per-acct: ``Σc(byBucketOwed[b]) + c(uncategorizedOwed)
      == c(runningTotal) − Σc(cumulativeEarmarkedPaid[b])`` (cent-exact).
    * INV-3 per-acct & grand: ``Σc(byBucketOwed[b]) + c(uncategorizedOwed)
      − c(cumulativeGeneralPaid) == c(runningTotal) − c(cumulativePaid)``.
    * INV-1: ``c(cumulativePaid) == Σc(cumulativeEarmarkedPaid[b])
      + c(cumulativeGeneralPaid)`` (payments are always settled so
      settled_full and full carry identical payment totals).
    * ``grandTotal.runningRemaining == Σ per-account runningRemaining`` (decision #6 —
      one card's credit must NOT reduce another card's owed amount).
    * ``perAccount`` includes every credit account with ANY history in txn_rows, not only
      accounts active this month.  Cards with a carried balance but no current-month
      transactions appear with month fields zeroed so the UI can display "still owed".

    Parameters
    ----------
    txn_rows:
        Full credit-account history with posted_on <= month_end(month), as returned by
        ``tracking_store.list_txns(date_to=month_end(month), account_ids=<credit_ids>)``.
    accounts:
        All accounts from ``tracking_store.list_accounts``.
    month:
        ``'YYYY-MM'`` string for the month being queried.

    Returns
    -------
    dict
        Same top-level shape as :func:`card_rollup` (``perAccount``, ``buckets``,
        ``grandTotal``), with additional fields per account and on grandTotal:
        ``carriedIn``, ``byBucketOwed``, ``uncategorizedOwed``, ``bucketsOwed``,
        ``runningPending``, ``cumulativePaid``, ``runningTotal``, ``runningRemaining``,
        ``runningCredit``, ``cumulativeEarmarkedPaid``, ``cumulativeGeneralPaid``.

        New top-level key ``bucketsOwed`` is the sorted union of bucket keys that appear
        in any account's ``byBucketOwed`` (i.e. ``settled_full["buckets"] ∪ cur["buckets"]``).
        The existing ``buckets`` key (month-scoped) is unchanged for backward compat.
    """
    month_start = f"{month}-01"

    # --- Slice txn_rows in Python via string-compare on postedOn (ISO dates sort lexically) ---

    # cur: current-month rows only → produces month fields identical to old endpoint
    cur_rows = [r for r in txn_rows if r["postedOn"].startswith(month)]

    # prior: rows strictly before this month → drives carriedIn
    prior_rows = [r for r in txn_rows if r["postedOn"] < month_start]

    # settled_full: settled charges/refunds + all transfer rows (payments are always settled).
    # Excluding pending non-transfer rows ensures card_rollup's byBucket/payNow reflects
    # SETTLED net charges only → byBucketOwed and runningTotal.
    settled_full_rows = [
        r for r in txn_rows
        if r.get("isTransfer") or r["status"] == "settled"
    ]
    # full: all rows (settled + pending); txn_rows itself → runningPending, cumulativePaid

    # --- Four card_rollup calls — all refund/split/payment logic stays inside card_rollup ---
    cur          = card_rollup(cur_rows, accounts)
    prior        = card_rollup(prior_rows, accounts)
    settled_full = card_rollup(settled_full_rows, accounts)
    full         = card_rollup(txn_rows, accounts)

    # --- Per-account output ---
    # FIX 1: iterate ALL credit accounts with history across any slice (including
    # accounts that have a carried balance from a prior month but ZERO current-month
    # activity).  This is the full union the grand total has always used — keeping
    # perAccount and grandTotal on the same key set guarantees the invariant:
    #   grandTotal.runningRemaining == Σ per-account runningRemaining
    all_account_keys = (
        set(cur["perAccount"])
        | set(settled_full["perAccount"])
        | set(full["perAccount"])
    )

    # Build a fallback lookup for accountId/name (used when zero-filling).
    # cur takes precedence; earlier entries are overwritten, so the merge is safe.
    _any_acct_ref: dict[str, dict] = {
        **prior["perAccount"],
        **settled_full["perAccount"],
        **full["perAccount"],
        **cur["perAccount"],
    }

    result_per_account: dict[str, dict] = {}
    for acct_key in all_account_keys:
        if acct_key in cur["perAccount"]:
            # Month fields preserved byte-identical to card_rollup(cur_rows, accounts)
            acct_data = cur["perAccount"][acct_key]
        else:
            # FIX 1: carried-balance-only account — zero-fill month fields so the card
            # still renders in the UI while its running fields reflect the full history.
            ref = _any_acct_ref[acct_key]
            acct_data = {
                "accountId":     ref["accountId"],
                "name":          ref["name"],
                "byBucket":      {},
                "uncategorized": 0.0,
                "payNow":        0.0,
                "pending":       0.0,
                "total":         0.0,
                "paid":          0.0,
                "remaining":     0.0,
                "credit":        0.0,
                "earmarkedPaid": {},
                "generalPaid":   0.0,
            }

        prior_acct   = prior["perAccount"].get(acct_key, {})
        settled_acct = settled_full["perAccount"].get(acct_key, {})
        full_acct    = full["perAccount"].get(acct_key, {})

        # carriedIn: unsettled balance carried from prior months.
        # Definition: prior perAccount remaining = max(0, prior payNow − prior paid).
        # Semantics: "$X owed from before this month that hasn't been paid yet."
        carried_in = prior_acct.get("remaining", 0.0)

        # byBucketOwed: NET = settled gross per bucket minus earmarked payments to that bucket.
        # Keys union settled byBucket and earmarkedPaid so over-payments on zero-charge
        # buckets (negative net) surface as columns.
        # INV-2: Σ byBucketOwed + uncategorizedOwed == runningTotal − Σ cumulativeEarmarkedPaid
        settled_by  = settled_acct.get("byBucket", {})
        settled_emp = settled_acct.get("earmarkedPaid", {})
        by_bucket_owed = {
            b: _d(round(settled_by.get(b, 0.0) * 100) - round(settled_emp.get(b, 0.0) * 100))
            for b in set(settled_by) | set(settled_emp)
        }

        # uncategorizedOwed: settled uncategorized — GROSS (earmarks do not affect it).
        # INV-2: Σ byBucketOwed + uncategorizedOwed == runningTotal − Σ cumulativeEarmarkedPaid
        uncategorized_owed = settled_acct.get("uncategorized", 0.0)

        running_pending = full_acct.get("pending", 0.0)
        cumulative_paid = full_acct.get("paid", 0.0)

        # runningTotal: Σ byBucketOwed + uncategorizedOwed = settled_full payNow
        # (cent-exact because card_rollup accumulates in integer cents → _d() once per field)
        running_total = settled_acct.get("payNow", 0.0)

        # Compute in cents to prevent float drift
        rt_c   = round(running_total * 100)
        paid_c = round(cumulative_paid * 100)
        # INVARIANT: exactly one of runningRemaining / runningCredit is non-zero per account
        remaining_c = max(0, rt_c - paid_c)
        credit_c    = max(0, paid_c - rt_c)

        result_per_account[acct_key] = {
            **acct_data,                               # all cur (month) fields intact
            "carriedIn":               carried_in,
            "byBucketOwed":            by_bucket_owed,
            "uncategorizedOwed":       uncategorized_owed,
            "runningPending":          running_pending,
            "cumulativePaid":          cumulative_paid,
            "runningTotal":            running_total,
            "runningRemaining":        _d(remaining_c),
            "runningCredit":           _d(credit_c),
            "cumulativeEarmarkedPaid": settled_acct.get("earmarkedPaid", {}),
            "cumulativeGeneralPaid":   settled_acct.get("generalPaid", 0.0),
        }

    # --- Grand total running fields ---
    # FIX 1: sum directly from result_per_account (same key set) so that
    #   grandTotal.runningRemaining == Σ per-account runningRemaining   is guaranteed.
    # Decision #6: Σ max(0,...) per account prevents cross-card credit netting.
    gt_run_remaining_c = sum(
        round(v["runningRemaining"] * 100) for v in result_per_account.values()
    )
    gt_run_credit_c = sum(
        round(v["runningCredit"] * 100) for v in result_per_account.values()
    )
    gt_carried_in_c = sum(
        round(v["carriedIn"] * 100) for v in result_per_account.values()
    )

    # Grand total byBucketOwed: NET = settled_full byBucket − earmarkedPaid per bucket.
    sf_gt    = settled_full["grandTotal"]
    sf_gt_by = sf_gt["byBucket"]
    sf_gt_ep = sf_gt.get("earmarkedPaid", {})
    gt_by_bucket_owed = {
        b: _d(round(sf_gt_by.get(b, 0.0) * 100) - round(sf_gt_ep.get(b, 0.0) * 100))
        for b in set(sf_gt_by) | set(sf_gt_ep)
    }

    grand_total: dict = {
        **cur["grandTotal"],               # all cur (month) grandTotal fields intact
        "carriedIn":               _d(gt_carried_in_c),
        # byBucketOwed: NET = settled_full byBucket − earmarkedPaid per bucket.
        # INV-2: Σ byBucketOwed + uncategorizedOwed == runningTotal − Σ cumulativeEarmarkedPaid
        # INV-3: Σ byBucketOwed + uncategorizedOwed − cumulativeGeneralPaid
        #        == runningTotal − cumulativePaid
        "byBucketOwed":            gt_by_bucket_owed,
        # uncategorizedOwed: settled_full grandTotal uncategorized (GROSS — no earmark offset)
        "uncategorizedOwed":       sf_gt["uncategorized"],
        # runningPending: full grandTotal pending
        "runningPending":          full["grandTotal"]["pending"],
        # cumulativePaid: full grandTotal paid
        "cumulativePaid":          full["grandTotal"]["paid"],
        # runningTotal: cumulative SETTLED net charges = settled_full grandTotal payNow
        "runningTotal":            sf_gt["payNow"],
        # runningRemaining/Credit: Σ per-account (decision #6 — no cross-card netting)
        "runningRemaining":        _d(gt_run_remaining_c),
        "runningCredit":           _d(gt_run_credit_c),
        # earmark fields: passed through from settled_full grandTotal
        "cumulativeEarmarkedPaid": sf_gt.get("earmarkedPaid", {}),
        "cumulativeGeneralPaid":   sf_gt.get("generalPaid", 0.0),
    }

    return {
        "perAccount":  result_per_account,
        "buckets":     cur["buckets"],
        # FIX 2: bucketsOwed — sorted union of all bucket keys present in any account's
        # byBucketOwed (equivalently settled_full["buckets"] ∪ cur["buckets"]).
        # Use this for the per-card "Balance by category" columns; "buckets" (month-scoped)
        # is preserved unchanged for backward compat.
        "bucketsOwed": sorted(set(settled_full["buckets"]) | set(cur["buckets"])),
        "grandTotal":  grand_total,
    }


__all__ = [
    "BUCKETS",
    "month_end",
    "aggregate_actuals",
    "build_plan",
    "plan_vs_actual",
    "card_rollup",
    "card_rollup_running",
]
