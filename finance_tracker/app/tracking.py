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


# ---------- accounts: flow-driven running balance (S2.1, DEC-038) ----------

def _nw_delta_cents(direction: str, kind: str, amount_cents: int) -> int:
    """One flow's net-worth-space signed delta (docs/reports-accounts-design.md §3.1):
    ``+amount`` for ``direction='in'``, ``-amount`` for ``'out'``; a ``kind='refund'``
    flips the sign (mirrors ``month_actuals``'s refund sign-flip, DEC-011 #2 -- a refund
    on an ``out`` txn behaves like money coming back in). This is the SAME formula for
    every account type: the asset/liability sign lives entirely in the snapshot anchor
    (``nwSign``, scaled once), never re-applied to a flow -- that's what makes a balanced
    transfer pair cancel to 0 regardless of which leg is the asset and which the
    liability (invariant I3)."""
    sign = 1 if direction == "in" else -1
    if kind == "refund":
        sign = -sign
    return sign * amount_cents


def account_balances(txn_rows: list[dict], snapshot_rows: list[dict], accounts: list[dict], as_of: str) -> dict:
    """Per-account flow-driven running balance (docs/reports-accounts-design.md §3) --
    generalizes ``card_rollup_running``'s credit-only running balance to every account
    type. Pure (no I/O), float dollars at the edge; all arithmetic is integer cents
    internally (mirrors ``card_rollup`` / ``fund_rollup``'s discipline). Computed, never
    stored -- no reconciliation "adjustment transaction" is ever synthesized (§3.2).

    Parameters
    ----------
    txn_rows : list[dict]
        Full account history with ``postedOn <= as_of``, in the ``list_txns`` camelCase
        shape (``accountId``, ``postedOn``, ``direction``, ``amount`` $, ``isTransfer``,
        ``status``, ``kind``). Splits are irrelevant here (a split only re-buckets a
        txn's money, never adds a flow) so this reads the flat parent row, not
        ``splits``. Transfers are INCLUDED (opposite of ``aggregate_actuals``, which
        excludes them, DEC-009 #1) -- balance math and spend math read the same
        ``isTransfer`` flag in opposite directions, by design (§3.4).
    snapshot_rows : list[dict]
        Full snapshot history, in the ``list_snapshots`` shape (``accountId``, ``asOf``,
        ``balance`` $) -- positive magnitudes, exactly as stored today (no re-signing).
        A snapshot dated AFTER ``as_of`` is defensively ignored (NIT-4, 2026-07-28
        review) -- the caller (the endpoint) already pre-filters via ``date_to``, but
        this function no longer trusts that as its only guard, so a future-dated
        snapshot passed directly (e.g. a bulk-import caller, or a test) can never
        leak into "today"'s anchor.
    accounts : list[dict]
        From ``list_accounts`` (``id``, ``name``, ``type``, ``isLiability``,
        ``creditLimit``, ...).
    as_of : str
        ``'YYYY-MM-DD'``; the query date. ``txn_rows`` are still trusted to already be
        ``postedOn <= as_of`` (mirrors ``card_rollup_running``'s "caller pre-filters by
        date_to" contract for flows) -- only the snapshot bound is re-checked here.

    Returns
    -------
    dict
        ``{asOf, netWorth, accounts: [{accountId, name, type, isLiability, opening,
        flowsSince, reconcileDelta, lastReconcileDelta, balance, netWorthContribution,
        pendingDelta, creditLimit, utilization}]}``.

        ``opening`` / ``flowsSince`` / ``reconcileDelta`` are the §3.3 decomposition:
        ``opening`` is the signed value of the FIRST-ever snapshot at/before ``as_of``
        (or 0 if none -- a virtual zero anchor, so the formula is uniform); ``flowsSince``
        is the cumulative settled flow-sum from just after that first snapshot's date
        through ``as_of``; ``reconcileDelta`` is the SUM of every re-anchor's absorbed
        drift between the first snapshot and the latest one (computed as the closed-form
        remainder ``netWorthContribution - opening - flowsSince`` via the §3.3
        telescoping identity, rather than iterating each re-anchor event individually --
        both are cent-exact equal, see invariant I2). It is 0.0 whenever there is at
        most one snapshot ever (nothing has been re-anchored yet).

        ``balance`` is the OWN-CONVENTION display number (asset: cash held, positive;
        liability: amount owed, positive) -- ``= netWorthContribution`` for an asset,
        ``= -netWorthContribution`` for a liability. ``netWorthContribution`` is the raw
        signed nw-space number (asset +, liability -); ``netWorth`` is their sum.

        ``pendingDelta`` sums PENDING flows in the same post-anchor window (parity with
        ``card_rollup_running``'s settled/pending split) -- not folded into ``balance``.

        ``utilization`` is ``balance / creditLimit`` (unfloored, can exceed 1.0 when
        over limit) for ``type == 'credit'`` accounts with a ``creditLimit`` set and
        greater than 0; ``None`` otherwise.

        ``lastReconcileDelta`` (S2.1 Phase B review, 2026-07-28) is the SINGLE-STEP drift
        absorbed by the MOST RECENT re-anchor only -- ``entered(Sk) - (value(Sk-1) +
        flows in (Sk-1, Sk])`` -- unlike the cumulative ``reconcileDelta`` (the Σ of
        EVERY re-anchor since the first-ever snapshot, §3.3's provable telescoping
        identity, left unchanged here since it IS the exit criterion). ``0.0`` when fewer
        than 2 snapshots exist at/before ``as_of`` (nothing has been re-anchored yet --
        same "no prior anchor to diverge from" case as ``reconcileDelta``). Returned
        ALREADY in the account's own display-sign convention (asset/liability flipped the
        same way ``balance`` is, unlike ``reconcileDelta``/``opening``/``flowsSince``
        which stay nw-signed) -- a UI reading it needs no isLiability-aware sign flip of
        its own, only ``balance - lastReconcileDelta`` for "what your logged activity
        alone implied at this save".
    """
    txns_by_acct: dict[int, list[dict]] = {}
    for t in txn_rows:
        txns_by_acct.setdefault(t["accountId"], []).append(t)

    snaps_by_acct: dict[int, list[dict]] = {}
    for sn in snapshot_rows:
        if sn["asOf"] > as_of:            # NIT-4: defensive -- never anchor on the future
            continue
        snaps_by_acct.setdefault(sn["accountId"], []).append(sn)
    for lst in snaps_by_acct.values():
        lst.sort(key=lambda s: s["asOf"])

    def _flow_sum_cents(rows: list[dict], lower_date: str | None, status: str,
                         upper_date: str | None = None) -> int:
        """Σ nw-delta cents over ``rows`` with this ``status``, in the window
        ``(lower_date, upper_date or as_of]`` -- strictly after ``lower_date`` (the
        anchor's own day is assumed already reflected in its hand-entered balance,
        §3.2), or unbounded below when ``lower_date`` is ``None``. When ``upper_date`` is
        omitted, behavior is UNCHANGED from before this window param existed: no upper
        bound is enforced here at all (``txn_rows`` are trusted to already be
        ``postedOn <= as_of``, per the function's own docstring -- this is the existing
        "caller pre-filters" contract, not touched by this change). An explicit
        ``upper_date`` earlier than ``as_of`` is used ONLY by the single-step
        ``lastReconcileDelta`` window below, which must stop at the latest snapshot's own
        date rather than running through to ``as_of``."""
        total = 0
        for r in rows:
            if r["status"] != status:
                continue
            if lower_date is not None and not (r["postedOn"] > lower_date):
                continue
            if upper_date is not None and not (r["postedOn"] <= upper_date):
                continue
            total += _nw_delta_cents(r["direction"], r["kind"], round(r["amount"] * 100))
        return total

    out_accounts: list[dict] = []
    net_worth_cents = 0
    for acct in accounts:
        acct_id = acct["id"]
        is_liability = bool(acct["isLiability"])
        nw_sign = -1 if is_liability else 1
        rows = txns_by_acct.get(acct_id, [])
        snaps = snaps_by_acct.get(acct_id, [])

        if snaps:
            v0_date, v0_val = snaps[0]["asOf"], nw_sign * round(snaps[0]["balance"] * 100)
            vk_date, vk_val = snaps[-1]["asOf"], nw_sign * round(snaps[-1]["balance"] * 100)
        else:
            v0_date, v0_val = None, 0
            vk_date, vk_val = None, 0

        flows_since_cents = _flow_sum_cents(rows, v0_date, "settled")
        flows_after_anchor_cents = _flow_sum_cents(rows, vk_date, "settled")
        nw_contribution_cents = vk_val + flows_after_anchor_cents
        reconcile_delta_cents = nw_contribution_cents - v0_val - flows_since_cents
        pending_delta_cents = _flow_sum_cents(rows, vk_date, "pending")

        balance_cents = -nw_contribution_cents if is_liability else nw_contribution_cents
        display_sign = -1 if is_liability else 1

        # lastReconcileDelta (S2.1 Phase B review, 2026-07-28 SHOULD-FIX): the SINGLE-STEP
        # drift absorbed by the MOST RECENT re-anchor only -- entered(Sk) - (value(Sk-1) +
        # flows in (Sk-1, Sk]) -- as opposed to reconcile_delta_cents above, which is the
        # CUMULATIVE Σ of every re-anchor since the first-ever snapshot (the provable §3.3
        # exit criterion, left untouched). 0 when fewer than 2 snapshots exist (nothing
        # re-anchored yet -- same case reconcile_delta_cents is 0 for). Computed in
        # nw-signed cents first (same convention as reconcile_delta_cents above), THEN
        # flipped to the account's own display-sign convention via `display_sign` --
        # unlike reconcile_delta_cents/opening/flowsSince, which stay nw-signed for the
        # provable identity, this one is meant to be read directly by a UI with no
        # isLiability-aware flip of its own.
        last_reconcile_delta_cents = 0
        if len(snaps) >= 2:
            prior_date = snaps[-2]["asOf"]
            prior_val = nw_sign * round(snaps[-2]["balance"] * 100)
            flows_between_cents = _flow_sum_cents(rows, prior_date, "settled", upper_date=vk_date)
            last_reconcile_delta_cents = vk_val - prior_val - flows_between_cents
        last_reconcile_delta_display_cents = display_sign * last_reconcile_delta_cents

        credit_limit = acct.get("creditLimit")
        utilization = None
        if acct["type"] == "credit" and credit_limit:
            utilization = round((balance_cents / 100.0) / credit_limit, 4)

        out_accounts.append({
            "accountId": acct_id,
            "name": acct["name"],
            "type": acct["type"],
            "isLiability": is_liability,
            "opening": _d(v0_val),
            "flowsSince": _d(flows_since_cents),
            "reconcileDelta": _d(reconcile_delta_cents),
            "lastReconcileDelta": _d(last_reconcile_delta_display_cents),
            "balance": _d(balance_cents),
            "netWorthContribution": _d(nw_contribution_cents),
            "pendingDelta": _d(pending_delta_cents),
            "creditLimit": credit_limit,
            "utilization": utilization,
        })
        net_worth_cents += nw_contribution_cents

    return {
        "asOf": as_of,
        "netWorth": _d(net_worth_cents),
        "accounts": out_accounts,
    }


# ---------- sinking funds (TODO-238, DEC-034) ----------

def fund_rollup(monthly_flows: dict[str, dict], upto_month: str | None = None) -> dict:
    """Per-fund reserve trajectory — the whole-month envelope fold (docs/sinking-funds-
    design.md §4.2). Pure (no I/O), float dollars at the edge; all arithmetic is done in
    integer cents internally (mirrors card_rollup / card_rollup_running's discipline).

    This is a NEW pure function layered ON TOP of the existing aggregator, exactly as
    card_rollup layers on top of aggregate_actuals (§5) — aggregate_actuals and
    plan_vs_actual are never called from here and never call this, so they stay
    byte-unchanged (DEC-009 #1). Phase 1 does not fold this into the month_actuals seam;
    that is Phase 2's job.

    Parameters
    ----------
    monthly_flows : dict
        ``{'YYYY-MM': {'contributeCents': int, 'drawCents': int}}`` — one entry per month
        with ANY contribution or draw activity, as returned by
        ``tracking_store.fund_monthly_flows``. A month absent from this dict is zero
        activity for BOTH roles — omitting it from the returned trajectory is exactly
        equivalent to folding it explicitly (``reserveClosing_M == reserveOpening_M`` when
        ``contrib_M == draw_M == 0``), so the trajectory below is sparse by design, never
        padded with zero rows.
    upto_month : str | None
        ``'YYYY-MM'`` or ``None``. When given, months strictly after ``upto_month`` are
        EXCLUDED from the fold (mirrors ``card_rollup_running``'s ``date_to`` bound) — a
        fund's reserve as of month M must never be influenced by data dated after M.
        ``None`` means "no bound, fold everything" (used by the hard-delete reserve
        guard, where "is there money left, ever" must reflect the fund's entire history).

    Returns
    -------
    dict
        ``trajectory``: chronological list of per-month dicts, each
        ``{month, opening, contribution, draw, fundedDraw, unfundedDraw, closing}``
        (all float dollars).
        ``reserve``: the closing balance of the LAST trajectory entry (0.0 when
        ``monthly_flows`` is empty or every included month has zero closing).
        ``contributionTotal`` / ``fundedDrawTotal`` / ``unfundedDrawTotal``: lifetime sums
        across the trajectory (i.e. bounded by ``upto_month`` exactly like every other
        field here).

    Reconciliation invariants (§5.1, QA-assertable, cent-exact by construction):
        1. Conservation: for every month M, ``closing_M == opening_M + contribution_M -
           fundedDraw_M``, and ``closing_M >= 0`` always (``fundedDraw = min(draw,
           available)`` guarantees the floor — never a separate ``max(0, ...)`` clamp that
           could retroactively rewrite history).
        2. No double-count (lifetime): ``Σ fundedDraw == Σ contribution - reserve`` (the
           final closing balance) — every funded-draw dollar was already counted at
           contribution time, so funded draws add zero NEW counted spend. Holds by
           telescoping: each month's ``closing`` becomes the next month's ``opening``, so
           ``Σ contribution - Σ fundedDraw`` collapses to the last ``closing``.
        3. Zero-fund byte-identity: ``monthly_flows == {}`` → ``trajectory == []`` and
           ``reserve == 0.0``. The regression guard this protects is structural, not just
           this function's output — ``aggregate_actuals``/``plan_vs_actual`` never call
           ``fund_rollup`` inline, so a bucket with no funds is byte-identical to today's
           numbers by construction, not by a special-cased empty branch here.
        4. Per-month floor (the deliberate divergence from ``card_rollup_running``, which
           floors only the FINAL cumulative): an ``unfundedDraw_M > 0`` is never
           retroactively cleared by a LATER month's contribution, because each month's
           fold reads only the PRIOR month's already-floored ``closing`` as its own
           ``opening`` — a later contribution can only grow the reserve going forward, it
           can never rewrite an already-realized overspend in an earlier trajectory entry.
    """
    months = sorted(m for m in monthly_flows if upto_month is None or m <= upto_month)
    trajectory: list[dict] = []
    opening_c = 0
    contrib_total_c = funded_total_c = unfunded_total_c = 0
    for m in months:
        flows = monthly_flows[m]
        contribution_c = int(flows.get("contributeCents", 0))
        draw_c = int(flows.get("drawCents", 0))
        available_c = opening_c + contribution_c
        funded_draw_c = min(draw_c, available_c)
        unfunded_draw_c = draw_c - funded_draw_c
        closing_c = available_c - funded_draw_c        # always >= 0: funded_draw_c <= available_c
        trajectory.append({
            "month": m,
            "opening": _d(opening_c),
            "contribution": _d(contribution_c),
            "draw": _d(draw_c),
            "fundedDraw": _d(funded_draw_c),
            "unfundedDraw": _d(unfunded_draw_c),
            "closing": _d(closing_c),
        })
        contrib_total_c += contribution_c
        funded_total_c += funded_draw_c
        unfunded_total_c += unfunded_draw_c
        opening_c = closing_c
    return {
        "trajectory": trajectory,
        "reserve": _d(opening_c),
        "contributionTotal": _d(contrib_total_c),
        "fundedDrawTotal": _d(funded_total_c),
        "unfundedDrawTotal": _d(unfunded_total_c),
    }


# ---------- sinking funds: yearly recurrence (TODO-238 amendment) ----------
#
# `fund_rollup` above is UNCHANGED by recurrence -- it never even takes a `recurrence`
# argument, so its output for a `recurrence='none'` fund (still the default, still
# "exactly today's behavior") is bit-identical to before this amendment, by
# construction, not by a special case. The two functions below are a NEW, separate
# display/lens layer on top (the same DEC-009 #1 "new pure fn layered on top" pattern
# `fund_rollup` itself follows relative to `aggregate_actuals`): they compute the
# fund's rolled-forward effective target date and a per-cycle contribute/draw summary
# for a 'yearly'-recurring fund, without touching the reserve fold at all. A fund's
# `target_date` is the recurrence anchor (its month/day repeats every year); the year
# stored in `target_date` itself is irrelevant to a 'yearly' fund once it recurs --
# only the month/day matters.

def _roll_anniversary(month: int, day: int, year: int) -> str:
    """'YYYY-MM-DD' for (month, day) in `year`, mapping Feb 29 -> Feb 28 in non-leap
    years so a Feb-29 anchor always resolves to a real calendar date."""
    if month == 2 and day == 29 and not calendar.isleap(year):
        day = 28
    return f"{year:04d}-{month:02d}-{day:02d}"


def fund_effective_target_date(target_date: str | None, recurrence: str, as_of_month: str) -> str | None:
    """The fund's EFFECTIVE target date for trajectory/display purposes as of
    `as_of_month` ('YYYY-MM').

    `recurrence='none'` (or `target_date is None`): returns `target_date` UNCHANGED --
    exactly today's one-time-target behavior.

    `recurrence='yearly'`: rolls `target_date`'s month/day forward to its NEXT
    occurrence STRICTLY AFTER `as_of_month` -- i.e. if the anniversary's own month is
    the as-of month or earlier, that occurrence has already "arrived" for the current
    view and the effective date is the FOLLOWING year's occurrence instead. Handles
    multi-year gaps (a long-stale `target_date`, e.g. set 6 years ago) in O(1)-ish
    iterations because the search starts at `as_of_month`'s own year, not
    `target_date`'s original year -- only `target_date`'s month/day is ever read.
    Feb 29 anchors resolve to Feb 28 in whichever candidate year is not a leap year.
    """
    if recurrence != "yearly" or target_date is None:
        return target_date
    _, mo_s, day_s = target_date.split("-")
    month, day = int(mo_s), int(day_s)
    candidate_year = int(as_of_month.split("-")[0])
    candidate = _roll_anniversary(month, day, candidate_year)
    while candidate[:7] <= as_of_month:
        candidate_year += 1
        candidate = _roll_anniversary(month, day, candidate_year)
    return candidate


def fund_cycle_summary(target_date: str | None, recurrence: str, as_of_month: str,
                        trajectory: list[dict]) -> dict | None:
    """Per-cycle contribute/draw summary for a 'yearly'-recurring fund, or `None` for
    `recurrence='none'` / no `target_date` (nothing to cycle).

    ``cycleEnd`` is `fund_effective_target_date`'s rolled-forward next occurrence;
    ``cycleStart`` is exactly one year earlier (same month/day, independently
    Feb-29/Feb-28 adjusted for ITS OWN year, which need not share `cycleEnd`'s
    leap-ness). ``contributedThisCycle`` / ``drawnThisCycle`` bucket `fund_rollup`'s own
    per-month `trajectory` rows (already-computed contribution/draw dollars -- the
    reserve fold itself is untouched) into the half-open `['cycleStart' month,
    'cycleEnd' month)` window via a plain 'YYYY-MM' string-range test (ISO month
    strings sort lexicographically, so this needs no date parsing).
    """
    if recurrence != "yearly" or target_date is None:
        return None
    _, mo_s, day_s = target_date.split("-")
    month, day = int(mo_s), int(day_s)
    cycle_end = fund_effective_target_date(target_date, recurrence, as_of_month)
    cycle_start = _roll_anniversary(month, day, int(cycle_end[:4]) - 1)
    contributed_c = drawn_c = 0
    for row in trajectory:
        if cycle_start[:7] <= row["month"] < cycle_end[:7]:
            contributed_c += round(row["contribution"] * 100)
            drawn_c += round(row["draw"] * 100)
    return {
        "cycleStart": cycle_start,
        "cycleEnd": cycle_end,
        "contributedThisCycle": _d(contributed_c),
        "drawnThisCycle": _d(drawn_c),
    }


__all__ = [
    "BUCKETS",
    "month_end",
    "aggregate_actuals",
    "build_plan",
    "plan_vs_actual",
    "card_rollup",
    "card_rollup_running",
    "account_balances",
    "fund_rollup",
    "fund_effective_target_date",
    "fund_cycle_summary",
]
