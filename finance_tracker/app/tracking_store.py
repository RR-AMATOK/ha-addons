"""Plan-vs-actual persistence — all SQLite I/O (stdlib `sqlite3`, zero new deps).

The pure comparison logic is in `tracking.py`; this module only stores rows and hands
them to that module's pure aggregator. Functions take an explicit `conn` so the server
can open one short-lived connection per request and tests can use a single `:memory:`
connection.

ISOLATION (DEC-006): our data lives ONLY in our own SQLite file at `ACTUALS_DB_PATH`
(default `/data/actuals.db`, the Home Assistant add-on's private volume). We never open
Home Assistant's recorder DB or any HA file.

Money is stored as integer cents; `tracking.py` converts to float dollars at the edge.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone

import schedules
import tracking

_ACCOUNT_TYPES = ("checking", "savings", "brokerage", "retirement", "hsa", "credit", "loan", "cash", "other")

_BACKUP_APP_TAG = "financial-planning-suite"

# Backups exported before the 2026-07-16 rename carry the old tag; imports accept both forever.
_BACKUP_LEGACY_APP_TAGS = frozenset({"income-tax-calculator"})

# SEC-003 (DEC-016 deferred Low): pre-import safety copies (`.pre-import-<ts>.bak`, written by
# import_all) accumulate forever otherwise. Keep only the newest N next to the live DB file.
MAX_PRE_IMPORT_BACKUPS = 5

# TODO-209 (deferred nit): the `_mig_drop_bucket_checks` copy-drop-rename migration and the
# `CHECK` constraints in `SCHEMA` rely on SQLite >= 3.43 behavior. init_db() asserts this floor
# so an old interpreter fails loudly instead of silently corrupting the schema.
_MIN_SQLITE_VERSION = (3, 43, 0)

# Single source of truth for backup/restore table and column identifiers — PARENT→CHILD order.
# Table and column names come ONLY from this constant; never from the payload.
# S1.1 note: `user_id` was added to the 10 user-owned tables' column tuples below (NOT to
# txn_split/txn_tag, which inherit scope from their parent txn/tag via FK). A pre-S1.1
# backup simply has no `user_id` key in its rows — the allow-list INSERT in import_all
# already only uses columns present in the payload row, so the column's
# `NOT NULL DEFAULT '__owner__'` assigns every restored legacy row to the owner for free.
_BACKUP_TABLES: tuple = (
    # S2.1 (DEC-038): `credit_limit_cents` appended to the EXISTING account column
    # tuple (not a new table entry, no _BACKUP_OPTIONAL_TABLES change) -- mirrors the
    # `invest_group` precedent above. A pre-S2.1 backup row simply lacks the key; the
    # allow-list INSERT in import_all only writes columns present in the payload row,
    # so the column's NULL-by-default nature backfills every restored legacy row to
    # "no limit set" for free (same free-backfill story as `user_id`/`recurrence`).
    ("account",          ("id", "user_id", "name", "type", "is_liability", "currency", "archived", "created_at", "invest_group", "credit_limit_cents")),
    ("tag",              ("id", "user_id", "name", "created_at")),
    ("template",         ("id", "user_id", "name", "direction", "amount_cents", "bucket", "category", "account_id", "description", "created_at")),
    ("txn",              ("id", "user_id", "account_id", "posted_on", "direction", "amount_cents", "bucket", "category", "description", "is_transfer", "transfer_group", "source", "external_id", "partner_owed_cents", "status", "kind", "created_at")),
    ("txn_split",        ("id", "txn_id", "bucket", "category", "amount_cents")),
    ("txn_tag",          ("txn_id", "tag_id")),
    ("balance_snapshot", ("id", "user_id", "account_id", "as_of", "balance_cents", "source", "created_at")),
    ("plan_snapshot",    ("id", "user_id", "month", "status", "engine_version", "payload_json", "created_at", "locked_at")),
    ("recurring",        ("id", "user_id", "bucket", "category", "direction", "due_day", "expected_cents", "active", "created_at")),
    ("scenario",         ("id", "user_id", "name", "status", "payload_json", "created_at", "updated_at", "activated_at")),
    ("goal",             ("id", "user_id", "name", "target_cents", "target_date", "account_id", "manual_saved_cents", "status", "created_at")),
    ("venture",          ("id", "user_id", "name", "tag", "account_id", "items_json", "started_on", "status", "created_at")),
    # Sinking funds (TODO-238, DEC-034, docs/sinking-funds-design.md §7): ordinary user
    # data, no write-time invariants beyond the payload's own value (unlike user_profile
    # below) — a plain verbatim restore is correct. `fund` before `fund_txn` (parent→child).
    # `recurrence` (TODO-238 amendment, yearly recurrence, v12->v13): also plain payload
    # data — a pre-recurrence backup simply lacks the key, and the column's own
    # `NOT NULL DEFAULT 'none'` backfills it for free (same free-backfill story as
    # `user_id` above).
    ("fund",             ("id", "user_id", "name", "bucket", "monthly_contribution_cents", "target_cents", "target_date", "recurrence", "status", "created_at")),
    ("fund_txn",         ("fund_id", "txn_id", "role")),
    # S1.2 (DEC-027/DEC-035, docs/s1_2-migration-design.md §1.3): the per-user server profile.
    # `prev_blob`/`prev_state_version` are DELIBERATELY excluded from this column tuple —
    # they are an ephemeral conflict-recovery buffer, not restore-worthy state; the
    # allow-list intersection drops them for free, so a restore rebuilds a clean
    # current-only profile (no stitched-together undo chain from before the restore).
    ("user_profile",     ("user_id", "blob", "state_version", "updated_at", "created_at")),
    # Household shared-budget layer (Slice A, TODO-232, DEC-041, docs/shared-budget-design.md
    # §9): HOUSEHOLD-scoped, not user_id-scoped (no `user_id` column on the parent — deliberately
    # excluded from `user_scoped_tables` below, same as `users`/`user_alias` are excluded from
    # this whole allow-list). Ordinary data, no write-time invariants beyond the payload's own
    # value (unlike `user_profile` above) — a plain verbatim restore is correct, mirroring the
    # `fund`/`fund_txn` precedent. Parent before child.
    ("household_budget",       ("id", "name", "bucket", "type", "total_cents", "status", "created_by", "created_at")),
    ("household_budget_share", ("line_id", "user_id", "split_ratio_bps", "contribution_cents")),
    # Scheduled money. Ordinary user data with no write-time invariants beyond its own values,
    # so a plain verbatim restore is correct (same story as fund/fund_txn). Parent first:
    # `schedule` before its two children, and `schedule_txn` references `txn` as well, which is
    # already restored earlier in this tuple. `recurring` above is KEPT even though nothing
    # writes to it any more -- an older backup still carries its rows, and _mig_add_schedule_tables
    # converts them on restore.
    ("schedule",           ("id", "user_id", "name", "direction", "amount_cents", "amount_is_estimate", "account_id", "to_account_id", "bucket", "category", "description", "freq", "interval_n", "weekdays", "day_1", "day_2", "month_of_year", "anchor_on", "end_mode", "ends_on", "end_count", "weekend_shift", "auto_post", "active", "parent_id", "created_at")),
    ("schedule_exception", ("id", "schedule_id", "occurrence_on", "action", "amount_cents", "moved_to", "description", "created_at")),
    ("schedule_txn",       ("schedule_id", "occurrence_on", "txn_id")),
    # FIRE progress log. Ordinary user data, plain verbatim restore. It matters MORE than most
    # that this is backed up: the rows cannot be regenerated from anything else, because the FI
    # target they carry was computed from assumptions that are gone. Lose the table and the
    # history is lost for good -- there is no recomputing it from the accounts.
    ("fire_progress",      ("id", "user_id", "on_date", "net_worth_cents", "fi_target_cents", "variant_key", "assumptions", "note", "created_at")),
)

# Tables added AFTER the original 9 — absent in older backups, so restore treats them as empty
# instead of rejecting the file. The original 9 stay strictly required (DEC-016 / DEC-017 #1).
_BACKUP_OPTIONAL_TABLES = frozenset({
    "scenario", "goal", "venture", "user_profile", "fund", "fund_txn",
    "household_budget", "household_budget_share",
    "schedule", "schedule_exception", "schedule_txn",
    "fire_progress",
})


class RestoreError(Exception):
    """Raised by import_all when the backup payload is invalid or incompatible; maps to HTTP 422."""


def resolve_db_path() -> str:
    """Where the actuals DB lives. `ACTUALS_DB_PATH` env wins; else the HA add-on volume
    `/data/actuals.db` when writable; else a repo-local file for development."""
    env = os.environ.get("ACTUALS_DB_PATH")
    if env:
        return env
    if os.path.isdir("/data") and os.access("/data", os.W_OK):
        return "/data/actuals.db"
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "actuals.db")

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS account (
  id           INTEGER PRIMARY KEY,
  user_id      TEXT    NOT NULL DEFAULT '__owner__',
  name         TEXT    NOT NULL,
  type         TEXT    NOT NULL DEFAULT 'other'
                 CHECK (type IN ('checking','savings','brokerage','retirement','hsa','credit','loan','cash','other')),
  is_liability INTEGER NOT NULL DEFAULT 0,
  currency     TEXT    NOT NULL DEFAULT 'USD',
  archived     INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT    NOT NULL,
  invest_group TEXT,                       -- Invest-tab grouping (TODO-222): free text with UI presets; NULL = not an investment account grouping
  credit_limit_cents INTEGER               -- S2.1 (DEC-038): current credit limit, cents. Nullable; meaningful only for type='credit'. NULL = no limit set
);

CREATE TABLE IF NOT EXISTS txn (
  id             INTEGER PRIMARY KEY,
  user_id        TEXT    NOT NULL DEFAULT '__owner__',
  account_id     INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
  posted_on      TEXT    NOT NULL,
  direction      TEXT    NOT NULL CHECK (direction IN ('in','out')),
  amount_cents   INTEGER NOT NULL CHECK (amount_cents >= 0),
  bucket         TEXT,
  category       TEXT,
  description    TEXT,
  is_transfer    INTEGER NOT NULL DEFAULT 0,
  transfer_group TEXT,
  source         TEXT    NOT NULL DEFAULT 'manual',
  external_id    TEXT,
  partner_owed_cents INTEGER NOT NULL DEFAULT 0,   -- partner's (e.g. Venmo) share of a shared expense
  status         TEXT    NOT NULL DEFAULT 'settled' CHECK (status IN ('settled','pending')),
  kind           TEXT    NOT NULL DEFAULT 'charge'  CHECK (kind   IN ('charge','refund')),
  created_at     TEXT    NOT NULL
);
CREATE INDEX        IF NOT EXISTS idx_txn_posted       ON txn(posted_on);
CREATE INDEX        IF NOT EXISTS idx_txn_account      ON txn(account_id);
CREATE INDEX        IF NOT EXISTS idx_txn_month_bucket ON txn(posted_on, bucket);
CREATE UNIQUE INDEX IF NOT EXISTS idx_txn_dedupe       ON txn(source, external_id) WHERE external_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS balance_snapshot (
  id            INTEGER PRIMARY KEY,
  user_id       TEXT    NOT NULL DEFAULT '__owner__',
  account_id    INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
  as_of         TEXT    NOT NULL,
  balance_cents INTEGER NOT NULL,
  source        TEXT    NOT NULL DEFAULT 'manual',
  created_at    TEXT    NOT NULL,
  UNIQUE (account_id, as_of)
);
CREATE INDEX IF NOT EXISTS idx_snap_account_date ON balance_snapshot(account_id, as_of);

CREATE TABLE IF NOT EXISTS plan_snapshot (
  id             INTEGER PRIMARY KEY,
  user_id        TEXT    NOT NULL DEFAULT '__owner__',
  month          TEXT    NOT NULL,
  status         TEXT    NOT NULL DEFAULT 'locked' CHECK (status IN ('draft','locked')),
  engine_version TEXT    NOT NULL,
  payload_json   TEXT    NOT NULL,
  created_at     TEXT    NOT NULL,
  locked_at      TEXT,
  UNIQUE (user_id, month)
);

-- Tags: free, multi, cross-cutting labels. ORTHOGONAL to the bucket rollup (DEC-009) —
-- aggregate_actuals never reads them, so plan-vs-actual is unaffected.
CREATE TABLE IF NOT EXISTS tag (
  id         INTEGER PRIMARY KEY,
  user_id    TEXT    NOT NULL DEFAULT '__owner__',
  name       TEXT    NOT NULL,
  created_at TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_name ON tag(name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS txn_tag (
  txn_id INTEGER NOT NULL REFERENCES txn(id) ON DELETE CASCADE,
  tag_id INTEGER NOT NULL REFERENCES tag(id) ON DELETE CASCADE,
  PRIMARY KEY (txn_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_txntag_tag ON txn_tag(tag_id);

-- Split transactions: one charge → N line items, each its own bucket. The parent txn keeps
-- the full amount; month_actuals UNIONs the children (not the parent) into the SAME flat row
-- shape, so tracking.py::aggregate_actuals stays pure/unchanged (DEC-009). Σ legs == parent.
CREATE TABLE IF NOT EXISTS txn_split (
  id           INTEGER PRIMARY KEY,
  txn_id       INTEGER NOT NULL REFERENCES txn(id) ON DELETE CASCADE,
  bucket       TEXT,
  category     TEXT,
  amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0)
);
CREATE INDEX IF NOT EXISTS idx_split_txn ON txn_split(txn_id);

-- Recurring templates: one-tap presets that PRE-FILL the quick-add form. They never
-- auto-create transactions (DEC-009 — actuals must be real and user-confirmed).
CREATE TABLE IF NOT EXISTS template (
  id           INTEGER PRIMARY KEY,
  user_id      TEXT    NOT NULL DEFAULT '__owner__',
  name         TEXT    NOT NULL,
  direction    TEXT    NOT NULL DEFAULT 'out' CHECK (direction IN ('in','out')),
  amount_cents INTEGER NOT NULL DEFAULT 0,
  bucket       TEXT,
  category     TEXT,
  account_id   INTEGER REFERENCES account(id) ON DELETE SET NULL,
  description  TEXT,
  created_at   TEXT    NOT NULL
);

-- Recurring expectations: monthly bills / income seeded from the Budget line items (matched by
-- bucket + category), with a due-day set here on the Actuals side. The "came through this month?"
-- reconciliation is computed against real transactions; we never auto-create a transaction
-- (DEC-009). expected_cents is the planned estimate (e.g. electricity may bill more or less).
CREATE TABLE IF NOT EXISTS recurring (
  id             INTEGER PRIMARY KEY,
  user_id        TEXT    NOT NULL DEFAULT '__owner__',
  bucket         TEXT,
  category       TEXT    NOT NULL,
  direction      TEXT    NOT NULL DEFAULT 'out' CHECK (direction IN ('in','out')),
  due_day        INTEGER CHECK (due_day IS NULL OR (due_day >= 1 AND due_day <= 31)),
  expected_cents INTEGER NOT NULL DEFAULT 0,
  active         INTEGER NOT NULL DEFAULT 1,
  created_at     TEXT    NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS recurring_key
  ON recurring (direction, IFNULL(bucket,''), category COLLATE NOCASE);

-- Scenario planner (TODO-219, DEC-017): a what-if comp -> budget-plan draft, and the one that is
-- ACTIVE. Additive - CREATE IF NOT EXISTS, no migration (DEC-009 #4). The what-if definition and
-- the revert bookkeeping live in opaque payload_json (like plan_snapshot); only list/filter
-- columns are promoted.
CREATE TABLE IF NOT EXISTS scenario (
  id           INTEGER PRIMARY KEY,
  user_id      TEXT    NOT NULL DEFAULT '__owner__',
  name         TEXT    NOT NULL,
  status       TEXT    NOT NULL DEFAULT 'draft'
                 CHECK (status IN ('draft','active')),
  payload_json TEXT    NOT NULL,
  created_at   TEXT    NOT NULL,
  updated_at   TEXT    NOT NULL,
  activated_at TEXT
);
-- Cardinality (DEC-017 #2): exactly ONE active. Partial UNIQUE index (mirrors idx_txn_dedupe);
-- a concurrent double-activate fails at the status UPDATE (IntegrityError -> rollback -> 409),
-- never two-active.
CREATE UNIQUE INDEX IF NOT EXISTS idx_scenario_active
  ON scenario(status) WHERE status = 'active';

-- Target-savings goals (TODO-226, DEC-019): save target_cents by target_date. Progress comes
-- from the linked account's latest balance snapshot when account_id is set, else from
-- manual_saved_cents. The per-month/per-paycheck math is pure (goals.py) — never stored.
CREATE TABLE IF NOT EXISTS goal (
  id                 INTEGER PRIMARY KEY,
  user_id            TEXT    NOT NULL DEFAULT '__owner__',
  name               TEXT    NOT NULL,
  target_cents       INTEGER NOT NULL CHECK (target_cents > 0),
  target_date        TEXT    NOT NULL,
  account_id         INTEGER REFERENCES account(id) ON DELETE SET NULL,
  manual_saved_cents INTEGER,
  status             TEXT    NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','done','cancelled')),
  created_at         TEXT    NOT NULL
);

-- Venture ROI tracker (TODO-228, DEC-020): earn back a self-investment (course, equipment)
-- from a side venture's real profits. Invested = typed items (items_json, cents) — the
-- stable payback yardstick. Recovered = net of REAL transactions linked by EITHER a tag
-- OR an account (exactly one; enforced in code). Payback math is pure (ventures.py).
CREATE TABLE IF NOT EXISTS venture (
  id         INTEGER PRIMARY KEY,
  user_id    TEXT    NOT NULL DEFAULT '__owner__',
  name       TEXT    NOT NULL,
  tag        TEXT,
  account_id INTEGER REFERENCES account(id) ON DELETE SET NULL,
  items_json TEXT    NOT NULL DEFAULT '[]',
  started_on TEXT    NOT NULL,
  status     TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active','stopped')),
  created_at TEXT    NOT NULL
);

-- Per-user server profile (S1.2, DEC-027/DEC-035, docs/s1_2-migration-design.md §1.1): the
-- boot/sync source of truth for the client's localStorage cache once a device migrates.
-- `blob` IS the versioned fps-backup client-section ({version:3, keys:{...}} over the
-- BACKUP_CLIENT_KEYS allowlist) -- no new format. `state_version` is the SYNC generation
-- (orthogonal to the blob's own `version` field). `prev_blob`/`prev_state_version` are a
-- ONE-level undo the server keeps on every LWW write (DEC-027 §7) -- displaced content,
-- never a history log. No FK to `users` (identity is install-local; this is DATA).
-- Additive CREATE IF NOT EXISTS -- CREATE TABLE IF NOT EXISTS + a mirrored idempotent
-- migration entry, same convention as `goal`/`venture`/`users` above.
CREATE TABLE IF NOT EXISTS user_profile (
  user_id            TEXT    PRIMARY KEY,
  blob               TEXT    NOT NULL,
  state_version      INTEGER NOT NULL DEFAULT 1,
  updated_at         TEXT    NOT NULL,
  prev_blob          TEXT,
  prev_state_version INTEGER,
  created_at         TEXT    NOT NULL
);

-- Household identity roster (multi-user S0.1, DEC-026/031): lazily provisioned by
-- server.py's resolve_user() -- NEVER written to directly from request handlers other
-- than through that one resolver. The first user_id ever seen (via a trusted ingress
-- header or the DEC-022 dev override) becomes 'owner'; every subsequently-seen new id
-- becomes 'member'. Deliberately NOT in _BACKUP_TABLES: identity is install-local, not
-- user financial data -- a backup/restore round-trip must never move or overwrite
-- who's-who between installs (see the constant's comment below).
-- `display_name` (human-readable roster names): captured from the trusted Supervisor
-- ingress header (X-Remote-User-Display-Name, falling back to X-Remote-User-Name) by
-- server.py's resolve_user() at provisioning time, and refreshed whenever the header
-- value changes on a later request. Nullable -- older Supervisor/Core versions never
-- send either header, and the dev override never sets it (the id doubles as the name
-- there). `label` is the owner-editable fallback for that case (PATCH
-- /api/tracking/users/{id}) -- also nullable, wins over display_name when rendering
-- (see index.html's owner-transfer picker: label || displayName || id).
CREATE TABLE IF NOT EXISTS users (
  user_id      TEXT PRIMARY KEY,
  role         TEXT NOT NULL CHECK (role IN ('owner','member')),
  display_name TEXT,
  label        TEXT,
  created_at   TEXT NOT NULL
);
-- At-most-one-owner invariant, enforced at the DB layer (mirrors idx_scenario_active):
-- a concurrent double-provision-as-owner fails the INSERT (IntegrityError) rather than
-- silently producing two owners.
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_one_owner
  ON users(role) WHERE role = 'owner';

-- Sinking funds (TODO-238, DEC-034, docs/sinking-funds-design.md §7): envelope-with-
-- carryover savings for lumpy planned expenses (car maintenance, travel). The reserve is
-- COMPUTED from linked fund_txn flows (contribute/draw), never stored — see
-- tracking.fund_rollup. `bucket` must be a spend bucket (never 'investment', DEC-010 —
-- enforced in store code, not a DB CHECK). Additive, user_id-scoped like goal/venture.
-- Placed AFTER the "Household identity roster" block above deliberately: this table
-- never existed pre-S1.1, so it must sit after the S1.1 QA harness's `_v6_schema()` split
-- marker (tests/test_s1_1_qa_matrix.py — reconstructs a pre-migration schema by cutting
-- SCHEMA's text at that exact comment) or that synthetic v6 DB would wrongly gain a
-- (regex-stripped, indexless) `fund` table it never had. The user_id / fund_id indexes
-- are declared only in `_mig_add_fund_table` below (not here) for the same reason the
-- OTHER user-scoped indexes live only in `_mig_add_user_scoping` — see that migration's
-- index-block comment.
-- `recurrence` (TODO-238 amendment, yearly-recurring funds like car insurance / annual
-- card fees): 'none' (default, today's one-time-target behavior, bit-identical) or
-- 'yearly' (the reserve math is UNCHANGED -- contributions/draws still fold the same
-- way -- only the DISPLAY-layer effective target date rolls `target_date`'s month/day
-- forward to its next occurrence; see tracking.fund_effective_target_date /
-- fund_cycle_summary). The existing `target_date` column is the recurrence anchor; no
-- other column was added.
CREATE TABLE IF NOT EXISTS fund (
  id                         INTEGER PRIMARY KEY,
  user_id                    TEXT NOT NULL DEFAULT '__owner__',
  name                       TEXT NOT NULL,
  bucket                     TEXT,               -- the envelope's spend bucket (not 'investment')
  monthly_contribution_cents INTEGER NOT NULL DEFAULT 0,
  target_cents               INTEGER,            -- optional
  target_date                TEXT,               -- optional eta / recurrence anchor
  recurrence                 TEXT NOT NULL DEFAULT 'none'
                               CHECK (recurrence IN ('none','yearly')),
  status                     TEXT NOT NULL DEFAULT 'active'
                               CHECK (status IN ('active','archived')),
  created_at                 TEXT NOT NULL
);

-- Child link: which transaction is a contribution to / draw from which fund. Inherits
-- scope via FK to fund/txn (NO user_id column — mirrors txn_tag/txn_split). A link-table
-- (not a `txn.fund_id`/`fund_role` column pair) because contributions and draws are both
-- direction='out' — a tag can't express the role without a migration on the hot `txn`
-- table, which this design deliberately avoids (§7). PRIMARY KEY(txn_id) enforces "one
-- fund per txn" so a dollar is never double-counted across funds.
CREATE TABLE IF NOT EXISTS fund_txn (
  fund_id INTEGER NOT NULL REFERENCES fund(id) ON DELETE CASCADE,
  txn_id  INTEGER NOT NULL REFERENCES txn(id)  ON DELETE CASCADE,
  role    TEXT NOT NULL CHECK (role IN ('contribute','draw')),
  PRIMARY KEY (txn_id)
);

-- Account linking / identity aliases ("appoint admins" via linking -- N HA accounts ->
-- 1 profile): `alias_id` is a raw HA user id that, from this row's insertion onward,
-- resolves to `primary_user_id` for EVERY identity purpose (role, scopeId, name) --
-- server.py's resolve_user() performs this resolution (via resolve_identity() below)
-- BEFORE any provisioning/role lookup, so an aliased id is never independently
-- provisioned as its own user again. Chains are FORBIDDEN by construction (an alias's
-- primary must never itself be an alias) -- enforced in code by redeem_link_code()'s
-- no-chain guard, not by a DB constraint (SQLite has no clean "not a key elsewhere in
-- the same table" CHECK). Deliberately NOT in _BACKUP_TABLES (mirrors `users`):
-- identity/linking is install-local, never moved by a backup/restore round-trip.
-- Placed at the very end of SCHEMA (after the "Sinking funds" block) so the S1.1 QA
-- harness's synthetic pre-migration schema simulations (tests/test_s1_1_qa_matrix.py's
-- `_v6_schema`/`_v7_schema`, cut at the "Sinking funds" marker) never see these tables
-- either -- they didn't exist that far back, same reasoning as `fund`/`fund_txn`'s own
-- placement note above.
CREATE TABLE IF NOT EXISTS user_alias (
  alias_id        TEXT PRIMARY KEY,
  primary_user_id TEXT NOT NULL,
  linked_at       TEXT NOT NULL
);

-- Single-use link codes: the two-sided handshake proving control of both accounts
-- before a link is created (POST /api/tracking/link-code issues one; POST
-- /api/tracking/link redeems it). Only the SHA-256 HASH is stored -- never the
-- plaintext code -- so a DB read (or backup, though this table is excluded from
-- backups entirely) can never recover a still-live code. 10-minute TTL, single-use
-- (`used` flips 0->1 on redemption, never reused); a fresh link-code generation
-- invalidates the issuer's prior outstanding code (see create_link_code()).
CREATE TABLE IF NOT EXISTS link_code (
  code_hash      TEXT PRIMARY KEY,
  issuer_user_id TEXT NOT NULL,
  expires_at     TEXT NOT NULL,
  used           INTEGER NOT NULL DEFAULT 0
);

-- Household shared-budget layer, Slice A (TODO-232, DEC-041, docs/shared-budget-design.md
-- §4/§9): a shared budget line -- split (rent divided 60/40) or pooled (groceries: per-
-- member contributions summed into one envelope). HOUSEHOLD-scoped (DEC-030 implicit
-- singleton), deliberately NOT user_id-scoped like fund/goal/venture above -- one row
-- both members read AND write (§4's rejection of the "shared section inside each
-- profile blob" alternative). A PURE expenses layer (DEC-041): never reads or writes
-- filing status, never touches tax computation -- keep it that way in any future edit.
-- `bucket` doubles as the actuals matching key (DEC-010, §6.3) -- deliberately no
-- framework `kind` column; kind (need/want/investment) stays per-profile so each member
-- can fold their share honestly into their own framework (§4).
-- Placed at the very END of SCHEMA (after `link_code`, itself after the "Sinking funds"
-- block) so the S1.1 QA harness's synthetic pre-migration schema simulations
-- (tests/test_s1_1_qa_matrix.py's `_v6_schema`/`_v7_schema`, cut at the "Sinking funds"
-- marker) never see these tables either -- same reasoning as `fund`/`fund_txn`'s and
-- `user_alias`/`link_code`'s own placement notes above; they didn't exist that far back.
-- AUTOINCREMENT on `id` is load-bearing, not tidiness (BUG-0017). Existing databases are
-- converted by migration 17 rather than by this line, so removing it does not immediately break
-- anything -- which is exactly why it is worth stating here: this declaration is where the schema
-- tells the truth about itself, and a reader should not have to find a migration to learn it. A plain INTEGER PRIMARY KEY
-- lets SQLite reissue a deleted row's id to the next insert: reproduced by creating line 2
-- "Netflix", hard-deleting it, and creating an unrelated line, which came back as id 2. Anything
-- holding a bare line id for later validation -- which the invite redemption path in
-- docs/household-invite-design.md does exactly -- would then resolve to a line nobody chose to
-- share. AUTOINCREMENT keeps a high-water mark in sqlite_sequence so an id is never reused.
CREATE TABLE IF NOT EXISTS household_budget (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  name        TEXT    NOT NULL,                 -- "Rent", "Groceries"
  bucket      TEXT    NOT NULL,                 -- server bucket key (DEC-010) = the actuals matching key
  type        TEXT    NOT NULL CHECK (type IN ('split','pooled')),
  total_cents INTEGER,                          -- split: full line ($2000). pooled: NULL (derived = sum of contributions)
  status      TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
  created_by  TEXT    NOT NULL,                 -- scopeId who created it (audit/honesty; not an owner gate)
  created_at  TEXT    NOT NULL
);

-- ── FIRE progress log ───────────────────────────────────────────────────────────────────────
-- One row per time the user says "this is where I am now". It stores the net worth AND the FI
-- target as it stood at that moment, with the assumptions that produced the target.
--
-- STORING THE TARGET IS THE WHOLE POINT, and it is why this cannot be derived after the fact.
-- The FI number is a function of spending, withdrawal rate and variant choices, all of which
-- change and none of which were ever recorded. There is no way to know what the target WAS last
-- March. Back-filling today's target across old net-worth snapshots would draw a confident line
-- describing a plan the user never had -- the same class as the sparkline that opened on six
-- virtual zeros, removed for exactly that reason (BUG-0057).
--
-- So history starts the day logging starts. The chart is empty on day one, and that is honest.
--
-- `assumptions` is the JSON the target was computed from. Kept verbatim rather than as columns
-- because the FIRE engine's inputs have changed twice already and will change again; a reader
-- needs to know what the number MEANT, and a column set frozen at today's engine would quietly
-- stop describing it.
CREATE TABLE IF NOT EXISTS fire_progress (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id         TEXT    NOT NULL,
  on_date         TEXT    NOT NULL,          -- 'YYYY-MM-DD', the day being claimed
  net_worth_cents INTEGER NOT NULL,
  fi_target_cents INTEGER NOT NULL,          -- the FI number AS IT STOOD, never recomputed
  variant_key     TEXT    NOT NULL DEFAULT '',  -- '' = the main target; else 'lean'/'fat'/custom
  assumptions     TEXT,                      -- JSON: what produced fi_target_cents
  note            TEXT,
  created_at      TEXT    NOT NULL,
  UNIQUE (user_id, on_date, variant_key)     -- one reading per day per target; re-logging replaces
);

-- Child: each member's participation. user_id = the member's DATA-SCOPE id (the same
-- scopeId resolve_user() hands out -- '__owner__' for the owner role regardless of their
-- real id, the real id for every other member -- see tracking_store.household_member_scopes,
-- the join key the funding rollup will reuse in a later slice).
CREATE TABLE IF NOT EXISTS household_budget_share (
  line_id            INTEGER NOT NULL REFERENCES household_budget(id) ON DELETE CASCADE,
  user_id            TEXT    NOT NULL,          -- member scopeId (join key for the funding rollup)
  split_ratio_bps    INTEGER,                   -- split type: basis points (6000 = 60%, 0 valid for 100/0). Sum must = 10000. NULL for pooled
  contribution_cents INTEGER,                   -- pooled type: this member's contribution; NULL for split
  PRIMARY KEY (line_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_hbshare_user ON household_budget_share(user_id);

-- Scheduled money (docs/mockups/scheduled-money-app.html, schedules.py): paychecks, bills and
-- standing transfers, with a real recurrence rule, an optional end, and per-occurrence edits.
-- Supersedes the narrow `recurring` bill checklist above, whose rows migrate in
-- (_mig_add_schedule_tables); `recurring` itself is KEPT and kept in _BACKUP_TABLES so older
-- backups still restore, but nothing writes to it any more.
--
-- FUTURE OCCURRENCES ARE NEVER ROWS. Only the rule is stored; dates are expanded on demand by
-- schedules.occurrences() across a bounded window. That is what keeps DEC-009 #3 ("never
-- auto-generate actuals") true for everything that has not happened yet -- an open-ended
-- schedule cannot pollute history because it has no history to pollute. Materialisation only
-- ever runs for dates <= today, and only for `auto_post` schedules.
--
-- Placed at the very END of SCHEMA on purpose: tests/test_s1_1_qa_matrix.py reconstructs
-- synthetic pre-migration schemas by cutting this text at the "-- Sinking funds" and
-- "-- Household identity roster" markers above, so anything after both is correctly absent
-- from those old-DB simulations. Same reasoning as `fund`'s and `household_budget`'s own
-- placement notes.
CREATE TABLE IF NOT EXISTS schedule (
  id                 INTEGER PRIMARY KEY,
  user_id            TEXT    NOT NULL DEFAULT '__owner__',
  name               TEXT    NOT NULL,
  -- 'transfer' moves between two of your own accounts and writes TWO txn legs sharing a
  -- transfer_group, exactly like record_card_payment; 'in'/'out' write one.
  direction          TEXT    NOT NULL CHECK (direction IN ('in','out','transfer')),
  amount_cents       INTEGER NOT NULL DEFAULT 0 CHECK (amount_cents >= 0),
  -- A variable bill (electricity). Estimates never auto-post: the figure is a guess, so it
  -- waits for confirmation with the guess pre-filled.
  amount_is_estimate INTEGER NOT NULL DEFAULT 0,
  account_id         INTEGER REFERENCES account(id) ON DELETE CASCADE,
  to_account_id      INTEGER REFERENCES account(id) ON DELETE CASCADE,   -- transfer destination; NULL otherwise
  bucket             TEXT,
  category           TEXT,
  description        TEXT,
  -- ---- the rule (see schedules.Rule, which reads exactly these columns) ----
  freq               TEXT    NOT NULL CHECK (freq IN ('daily','weekly','semimonthly','monthly','yearly')),
  interval_n         INTEGER NOT NULL DEFAULT 1 CHECK (interval_n >= 1),
  weekdays           TEXT,                    -- 'FR' or 'MO,WE' (Sunday-based codes); weekly only
  -- 32 is the "last day of the month" sentinel -- deliberately outside 1..31 so it can never
  -- collide with a real day. A day beyond the month's length clamps (31 Feb -> 28/29).
  day_1              INTEGER CHECK (day_1 IS NULL OR (day_1 >= 1 AND day_1 <= 32)),
  day_2              INTEGER CHECK (day_2 IS NULL OR (day_2 >= 1 AND day_2 <= 32)),
  month_of_year      INTEGER CHECK (month_of_year IS NULL OR (month_of_year >= 1 AND month_of_year <= 12)),  -- 1-BASED (January = 1)
  anchor_on          TEXT    NOT NULL,        -- first occurrence / phase anchor; biweekly parity depends on it
  end_mode           TEXT    NOT NULL DEFAULT 'never' CHECK (end_mode IN ('never','on','after')),
  ends_on            TEXT,                    -- end_mode='on'; INCLUSIVE
  end_count          INTEGER,                 -- end_mode='after'; counted from anchor_on, never from a view window
  weekend_shift      TEXT    NOT NULL DEFAULT 'none' CHECK (weekend_shift IN ('none','before','after')),
  -- ---- behaviour ----
  auto_post          INTEGER NOT NULL DEFAULT 0,   -- opt-in; posts as status='pending' on the day
  active             INTEGER NOT NULL DEFAULT 1,
  -- "Change this one and all future" ends the current series and starts a successor; parent_id
  -- keeps the lineage visible rather than leaving two unrelated-looking schedules.
  parent_id          INTEGER REFERENCES schedule(id) ON DELETE SET NULL,
  created_at         TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_schedule_user   ON schedule(user_id);
CREATE INDEX IF NOT EXISTS idx_schedule_active ON schedule(user_id, active);

-- Per-occurrence edits. `occurrence_on` is the date the RULE produced (schedules.py's `raw`),
-- NOT the date it lands on after a weekend shift -- keying on the shifted date would silently
-- orphan every exception the moment weekend_shift changed.
CREATE TABLE IF NOT EXISTS schedule_exception (
  id            INTEGER PRIMARY KEY,
  schedule_id   INTEGER NOT NULL REFERENCES schedule(id) ON DELETE CASCADE,
  occurrence_on TEXT    NOT NULL,
  action        TEXT    NOT NULL CHECK (action IN ('skip','override')),
  amount_cents  INTEGER,
  moved_to      TEXT,
  description   TEXT,
  created_at    TEXT    NOT NULL,
  UNIQUE (schedule_id, occurrence_on)
);

-- Which occurrences have already become real transactions. The PRIMARY KEY *is* the
-- idempotency guarantee: catch-up can run on every page load and cannot double-post. For a
-- transfer this points at the primary leg; the pair is found via the txn's transfer_group,
-- exactly as card payments already work (delete_txn removes every leg in the group).
CREATE TABLE IF NOT EXISTS schedule_txn (
  schedule_id   INTEGER NOT NULL REFERENCES schedule(id) ON DELETE CASCADE,
  occurrence_on TEXT    NOT NULL,
  txn_id        INTEGER NOT NULL REFERENCES txn(id) ON DELETE CASCADE,
  PRIMARY KEY (schedule_id, occurrence_on)
);
CREATE INDEX IF NOT EXISTS idx_schedtxn_txn ON schedule_txn(txn_id);
"""

# Future migrations append to this list; each takes a conn and upgrades by one step.
# Idempotent (guard with PRAGMA table_info) so they're safe on fresh DBs that already
# have the column from the CREATE TABLE above and on older DBs that don't.
def _mig_add_partner_owed(conn) -> None:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(txn)").fetchall()]
    if "partner_owed_cents" not in cols:
        conn.execute("ALTER TABLE txn ADD COLUMN partner_owed_cents INTEGER NOT NULL DEFAULT 0")

def _mig_drop_bucket_checks(conn) -> None:
    """Migration 2: recreate txn/txn_split/template/recurring without the bucket CHECK
    constraint so any non-empty string is a valid bucket.  SQLite cannot ALTER DROP a
    CHECK, so we do the standard copy-drop-rename dance with FK enforcement suspended.
    Per-table idempotency: skip a table whose DDL no longer contains the bucket CHECK."""
    fk_state = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        # ---- txn ----
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='txn'").fetchone()
        if row and "bucket IN (" in row[0]:
            conn.execute("DROP TABLE IF EXISTS txn_new")  # crash-safety: remove any leftover from a prior interrupted run
            conn.execute("""
                CREATE TABLE txn_new (
                  id                 INTEGER PRIMARY KEY,
                  account_id         INTEGER NOT NULL REFERENCES account(id) ON DELETE CASCADE,
                  posted_on          TEXT    NOT NULL,
                  direction          TEXT    NOT NULL CHECK (direction IN ('in','out')),
                  amount_cents       INTEGER NOT NULL CHECK (amount_cents >= 0),
                  bucket             TEXT,
                  category           TEXT,
                  description        TEXT,
                  is_transfer        INTEGER NOT NULL DEFAULT 0,
                  transfer_group     TEXT,
                  source             TEXT    NOT NULL DEFAULT 'manual',
                  external_id        TEXT,
                  partner_owed_cents INTEGER NOT NULL DEFAULT 0,
                  created_at         TEXT    NOT NULL
                )""")
            conn.execute("""
                INSERT INTO txn_new
                  SELECT id, account_id, posted_on, direction, amount_cents, bucket, category,
                         description, is_transfer, transfer_group, source, external_id,
                         partner_owed_cents, created_at
                  FROM txn""")
            conn.execute("DROP TABLE txn")
            conn.execute("ALTER TABLE txn_new RENAME TO txn")
            conn.execute("CREATE INDEX        IF NOT EXISTS idx_txn_posted       ON txn(posted_on)")
            conn.execute("CREATE INDEX        IF NOT EXISTS idx_txn_account      ON txn(account_id)")
            conn.execute("CREATE INDEX        IF NOT EXISTS idx_txn_month_bucket ON txn(posted_on, bucket)")
            conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_txn_dedupe       ON txn(source, external_id) WHERE external_id IS NOT NULL")

        # ---- txn_split ----
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='txn_split'").fetchone()
        if row and "bucket IN (" in row[0]:
            conn.execute("DROP TABLE IF EXISTS txn_split_new")  # crash-safety: remove any leftover from a prior interrupted run
            conn.execute("""
                CREATE TABLE txn_split_new (
                  id           INTEGER PRIMARY KEY,
                  txn_id       INTEGER NOT NULL REFERENCES txn(id) ON DELETE CASCADE,
                  bucket       TEXT,
                  category     TEXT,
                  amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0)
                )""")
            conn.execute("""
                INSERT INTO txn_split_new
                  SELECT id, txn_id, bucket, category, amount_cents FROM txn_split""")
            conn.execute("DROP TABLE txn_split")
            conn.execute("ALTER TABLE txn_split_new RENAME TO txn_split")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_split_txn ON txn_split(txn_id)")

        # ---- template ----
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='template'").fetchone()
        if row and "bucket IN (" in row[0]:
            conn.execute("DROP TABLE IF EXISTS template_new")  # crash-safety: remove any leftover from a prior interrupted run
            conn.execute("""
                CREATE TABLE template_new (
                  id           INTEGER PRIMARY KEY,
                  name         TEXT    NOT NULL,
                  direction    TEXT    NOT NULL DEFAULT 'out' CHECK (direction IN ('in','out')),
                  amount_cents INTEGER NOT NULL DEFAULT 0,
                  bucket       TEXT,
                  category     TEXT,
                  account_id   INTEGER REFERENCES account(id) ON DELETE SET NULL,
                  description  TEXT,
                  created_at   TEXT    NOT NULL
                )""")
            conn.execute("""
                INSERT INTO template_new
                  SELECT id, name, direction, amount_cents, bucket, category,
                         account_id, description, created_at FROM template""")
            conn.execute("DROP TABLE template")
            conn.execute("ALTER TABLE template_new RENAME TO template")

        # ---- recurring ----
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='recurring'").fetchone()
        if row and "bucket IN (" in row[0]:
            conn.execute("DROP TABLE IF EXISTS recurring_new")  # crash-safety: remove any leftover from a prior interrupted run
            conn.execute("""
                CREATE TABLE recurring_new (
                  id             INTEGER PRIMARY KEY,
                  bucket         TEXT,
                  category       TEXT    NOT NULL,
                  direction      TEXT    NOT NULL DEFAULT 'out' CHECK (direction IN ('in','out')),
                  due_day        INTEGER CHECK (due_day IS NULL OR (due_day >= 1 AND due_day <= 31)),
                  expected_cents INTEGER NOT NULL DEFAULT 0,
                  active         INTEGER NOT NULL DEFAULT 1,
                  created_at     TEXT    NOT NULL
                )""")
            conn.execute("""
                INSERT INTO recurring_new
                  SELECT id, bucket, category, direction, due_day,
                         expected_cents, active, created_at FROM recurring""")
            conn.execute("DROP TABLE recurring")
            conn.execute("ALTER TABLE recurring_new RENAME TO recurring")
            conn.execute("""
                CREATE UNIQUE INDEX IF NOT EXISTS recurring_key
                  ON recurring (direction, IFNULL(bucket,''), category COLLATE NOCASE)""")
    finally:
        # NOTE: by this point an INSERT has opened an implicit transaction; PRAGMA foreign_keys
        # is silently ignored inside a transaction. Harmless — connect() re-asserts
        # PRAGMA foreign_keys=ON per request, so FK enforcement is never skipped at steady state.
        conn.execute(f"PRAGMA foreign_keys = {fk_state}")


def _mig_add_txn_status_kind(conn) -> None:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(txn)").fetchall()]
    if "status" not in cols:
        conn.execute("ALTER TABLE txn ADD COLUMN status TEXT NOT NULL DEFAULT 'settled' CHECK (status IN ('settled','pending'))")
    if "kind" not in cols:
        conn.execute("ALTER TABLE txn ADD COLUMN kind TEXT NOT NULL DEFAULT 'charge' CHECK (kind IN ('charge','refund'))")


def _mig_add_invest_group(conn) -> None:
    """Migration 4 (TODO-222): optional Invest-tab account grouping. Additive column;
    idempotent via the PRAGMA guard (fresh DBs already have it from CREATE TABLE)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(account)").fetchall()]
    if "invest_group" not in cols:
        conn.execute("ALTER TABLE account ADD COLUMN invest_group TEXT")


def _mig_add_goal_table(conn) -> None:
    """Migration 5 (TODO-226): target-savings goals. Additive table; CREATE IF NOT EXISTS
    makes it idempotent on fresh DBs that already have it from SCHEMA."""
    conn.execute("""CREATE TABLE IF NOT EXISTS goal (
  id                 INTEGER PRIMARY KEY,
  name               TEXT    NOT NULL,
  target_cents       INTEGER NOT NULL CHECK (target_cents > 0),
  target_date        TEXT    NOT NULL,
  account_id         INTEGER REFERENCES account(id) ON DELETE SET NULL,
  manual_saved_cents INTEGER,
  status             TEXT    NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','done','cancelled')),
  created_at         TEXT    NOT NULL
)""")


def _mig_add_venture_table(conn) -> None:
    """Migration 6 (TODO-228): venture ROI tracker. Additive table; idempotent."""
    conn.execute("""CREATE TABLE IF NOT EXISTS venture (
  id         INTEGER PRIMARY KEY,
  name       TEXT    NOT NULL,
  tag        TEXT,
  account_id INTEGER REFERENCES account(id) ON DELETE SET NULL,
  items_json TEXT    NOT NULL DEFAULT '[]',
  started_on TEXT    NOT NULL,
  status     TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active','stopped')),
  created_at TEXT    NOT NULL
)""")


def _mig_add_users_table(conn) -> None:
    """Migration 6 (multi-user S0.1, DEC-026/031): household identity roster. Additive
    table; CREATE IF NOT EXISTS makes it idempotent on fresh DBs that already have it
    (plus the one-owner unique index) from SCHEMA above. See SCHEMA's comment for why
    this table is deliberately excluded from _BACKUP_TABLES."""
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
  user_id    TEXT PRIMARY KEY,
  role       TEXT NOT NULL CHECK (role IN ('owner','member')),
  created_at TEXT NOT NULL
)""")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_one_owner ON users(role) WHERE role = 'owner'")


# Sentinel every existing (single-tenant) row is backfilled to — the owner's data-scope id
# (S1.1, docs/multiuser-household-plan.md, per-user data separation). Duplicated here (not
# imported from the identity section below) because migrations must not depend on forward
# references; kept textually identical to `_SENTINEL_OWNER_ID`.
_MIGRATION_OWNER_SENTINEL = "__owner__"

# The 9 tables that get `user_id` via a plain ALTER ADD COLUMN. `plan_snapshot` is handled
# separately (_mig_add_user_scoping) because its month UNIQUE must become UNIQUE(user_id, month),
# which SQLite cannot express via ALTER — it needs the copy-drop-rename rebuild.
_USER_SCOPED_ALTER_TABLES = (
    "account", "txn", "balance_snapshot", "tag", "template", "recurring", "scenario", "goal", "venture",
)


def _mig_add_user_scoping(conn) -> None:
    """Migration 8 (S1.1, per-user data separation): additive `user_id` on every
    user-owned table, backfilling ALL existing rows to the owner sentinel.

    Why the DEFAULT *is* the backfill: SQLite requires a default to ADD a NOT NULL
    column to a populated table, and every pre-existing row was single-tenant owner
    data anyway -- `DEFAULT '__owner__'` assigns it correctly in one statement, and
    makes restoring an old (pre-user_id) backup correct for free (see `import_all`'s
    allow-list INSERT, which simply omits the column and lets the DEFAULT apply).

    Store code must always pass `user_id` explicitly on every insert going forward --
    the DEFAULT is a backfill/migration safety net, not a substitute for scoping.

    Idempotent: each ALTER is guarded by a `PRAGMA table_info` check; the
    `plan_snapshot` rebuild guards on the same check; the index drop/recreate block
    uses `DROP INDEX IF EXISTS` / `CREATE ... IF NOT EXISTS` throughout.
    """
    # ---- 2a. plain ALTER ADD COLUMN on 9 tables ----
    for tbl in _USER_SCOPED_ALTER_TABLES:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})").fetchall()]
        if "user_id" not in cols:
            conn.execute(
                f"ALTER TABLE {tbl} ADD COLUMN user_id TEXT NOT NULL DEFAULT '{_MIGRATION_OWNER_SENTINEL}'")

    # ---- 2b. plan_snapshot rebuild: month UNIQUE -> UNIQUE(user_id, month) ----
    cols = [r[1] for r in conn.execute("PRAGMA table_info(plan_snapshot)").fetchall()]
    if "user_id" not in cols:
        fk_state = conn.execute("PRAGMA foreign_keys").fetchone()[0]
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.execute("DROP TABLE IF EXISTS plan_snapshot_new")  # crash-safety: prior interrupted run
            conn.execute("""
                CREATE TABLE plan_snapshot_new (
                  id             INTEGER PRIMARY KEY,
                  user_id        TEXT    NOT NULL DEFAULT '__owner__',
                  month          TEXT    NOT NULL,
                  status         TEXT    NOT NULL DEFAULT 'locked' CHECK (status IN ('draft','locked')),
                  engine_version TEXT    NOT NULL,
                  payload_json   TEXT    NOT NULL,
                  created_at     TEXT    NOT NULL,
                  locked_at      TEXT,
                  UNIQUE (user_id, month)
                )""")
            conn.execute("""
                INSERT INTO plan_snapshot_new (id, user_id, month, status, engine_version, payload_json, created_at, locked_at)
                  SELECT id, '__owner__', month, status, engine_version, payload_json, created_at, locked_at
                  FROM plan_snapshot""")
            conn.execute("DROP TABLE plan_snapshot")
            conn.execute("ALTER TABLE plan_snapshot_new RENAME TO plan_snapshot")
        finally:
            # NOTE: by this point an INSERT has opened an implicit transaction; PRAGMA foreign_keys
            # is silently ignored inside a transaction. Harmless — connect() re-asserts
            # PRAGMA foreign_keys=ON per request, so FK enforcement is never skipped at steady state.
            conn.execute(f"PRAGMA foreign_keys = {fk_state}")

    # ---- 2c. index plan: drop the four uniques that must become per-user, recreate
    # per-user, plus plain scan indexes. These live ONLY here (never in SCHEMA) — see
    # SCHEMA's ordering comment: a standalone CREATE INDEX referencing user_id would
    # crash executescript(SCHEMA) on a v6 device DB, since it runs before this ALTER. ----
    conn.execute("DROP INDEX IF EXISTS idx_tag_name")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_tag_name ON tag(user_id, name COLLATE NOCASE)")
    conn.execute("DROP INDEX IF EXISTS recurring_key")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS recurring_key "
        "ON recurring(user_id, direction, IFNULL(bucket,''), category COLLATE NOCASE)")
    conn.execute("DROP INDEX IF EXISTS idx_scenario_active")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_scenario_active ON scenario(user_id, status) WHERE status='active'")
    conn.execute("DROP INDEX IF EXISTS idx_txn_dedupe")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_txn_dedupe "
        "ON txn(user_id, source, external_id) WHERE external_id IS NOT NULL")

    conn.execute("DROP INDEX IF EXISTS idx_txn_posted")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_txn_user_posted ON txn(user_id, posted_on)")
    conn.execute("DROP INDEX IF EXISTS idx_txn_account")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_txn_user_account ON txn(user_id, account_id)")
    conn.execute("DROP INDEX IF EXISTS idx_txn_month_bucket")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_txn_user_mo_bucket ON txn(user_id, posted_on, bucket)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_account_user  ON account(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_user     ON balance_snapshot(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_template_user ON template(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_goal_user     ON goal(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_venture_user  ON venture(user_id)")


def _mig_add_user_profile(conn) -> None:
    """Migration 8 (S1.2, DEC-027/DEC-035, docs/s1_2-migration-design.md §1.2): additive
    per-user server profile table -- v8 -> v9. `CREATE TABLE IF NOT EXISTS` makes it
    idempotent on fresh DBs that already have it from SCHEMA above (mirrors the
    goal/venture/users convention exactly). Pure additive `CREATE` -- no ALTER, no data
    rewrite, no copy-drop-rename (DEC-009 #4). A v8 device booting v9 code runs SCHEMA
    (creates the table) then this migration (idempotent no-op) and stamps user_version=9.
    A v9 DB opened by OLDER (v8) code runs `range(9, 8)` -- empty, no crash -- and v8
    code never references `user_profile`, so rollback (profiles are feature-flagged
    until R1) is safe with no down-migration needed."""
    conn.execute("""CREATE TABLE IF NOT EXISTS user_profile (
  user_id            TEXT    PRIMARY KEY,
  blob               TEXT    NOT NULL,
  state_version      INTEGER NOT NULL DEFAULT 1,
  updated_at         TEXT    NOT NULL,
  prev_blob          TEXT,
  prev_state_version INTEGER,
  created_at         TEXT    NOT NULL
)""")


def _mig_add_fund_table(conn) -> None:
    """Migration 9 (TODO-238, DEC-034, docs/sinking-funds-design.md §7): additive sinking-
    fund tables -- v9 -> v10. `CREATE TABLE IF NOT EXISTS` makes it idempotent on fresh DBs
    that already have both tables from SCHEMA above (mirrors the goal/venture/user_profile
    convention exactly). Pure additive `CREATE` -- no ALTER, no data rewrite (DEC-009 #4).
    `txn` itself is untouched (§7 — deliberately avoids an ALTER on the hot table)."""
    conn.execute("""CREATE TABLE IF NOT EXISTS fund (
  id                         INTEGER PRIMARY KEY,
  user_id                    TEXT NOT NULL DEFAULT '__owner__',
  name                       TEXT NOT NULL,
  bucket                     TEXT,
  monthly_contribution_cents INTEGER NOT NULL DEFAULT 0,
  target_cents               INTEGER,
  target_date                TEXT,
  status                     TEXT NOT NULL DEFAULT 'active'
                               CHECK (status IN ('active','archived')),
  created_at                 TEXT NOT NULL
)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fund_user ON fund(user_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS fund_txn (
  fund_id INTEGER NOT NULL REFERENCES fund(id) ON DELETE CASCADE,
  txn_id  INTEGER NOT NULL REFERENCES txn(id)  ON DELETE CASCADE,
  role    TEXT NOT NULL CHECK (role IN ('contribute','draw')),
  PRIMARY KEY (txn_id)
)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fund_txn_fund ON fund_txn(fund_id)")


def _mig_add_user_display_name(conn) -> None:
    """Migration 10 (household roster human-readable names): additive `display_name` +
    `label` columns on `users` -- v10 -> v11. `CREATE TABLE IF NOT EXISTS` makes fresh
    DBs already have both columns from SCHEMA above; this ALTER path only fires on an
    existing device DB that predates this change. Both columns are nullable, so no
    DEFAULT is needed for the ALTER (existing rows backfill to NULL for free) -- unlike
    the S1.1 `user_id` scoping columns, there's no "what should every old row become"
    question here. Idempotent via the standard PRAGMA table_info guard.

    `display_name` is captured/refreshed by server.py's resolve_user() from the trusted
    Supervisor-peer X-Remote-User-Display-Name/-Name headers; `label` is the
    owner-editable fallback (PATCH /api/tracking/users/{id}) for Supervisor versions
    that never send those headers, or for service accounts. `label` wins over
    `display_name` at render time (index.html's owner-transfer picker)."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "display_name" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN display_name TEXT")
    if "label" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN label TEXT")


def _mig_add_alias_tables(conn) -> None:
    """Migration 11 (account linking / identity aliases -- "appoint admins" via linking):
    additive `user_alias` + `link_code` tables -- v11 -> v12. `CREATE TABLE IF NOT EXISTS`
    makes it idempotent on fresh DBs that already have both from SCHEMA above (mirrors the
    goal/venture/user_profile/fund convention exactly). Pure additive `CREATE` -- no ALTER,
    no data rewrite (DEC-009 #4). Indexes live ONLY here (never in SCHEMA) -- same
    reasoning as `_mig_add_fund_table`'s index-block comment: a synthetic pre-migration
    schema simulation (tests/test_s1_1_qa_matrix.py) that creates these tables via a
    stripped/partial SCHEMA text must never see a CREATE INDEX referencing a column that
    stripping removed."""
    conn.execute("""CREATE TABLE IF NOT EXISTS user_alias (
  alias_id        TEXT PRIMARY KEY,
  primary_user_id TEXT NOT NULL,
  linked_at       TEXT NOT NULL
)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_alias_primary ON user_alias(primary_user_id)")
    conn.execute("""CREATE TABLE IF NOT EXISTS link_code (
  code_hash      TEXT PRIMARY KEY,
  issuer_user_id TEXT NOT NULL,
  expires_at     TEXT NOT NULL,
  used           INTEGER NOT NULL DEFAULT 0
)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_link_code_issuer ON link_code(issuer_user_id)")


def _mig_add_fund_recurrence(conn) -> None:
    """Migration 12 (TODO-238 amendment, yearly-recurring sinking funds -- car insurance,
    annual card fees -- docs/sinking-funds-design.md §amendment): additive
    `fund.recurrence` column -- v12 -> v13. Idempotent via the standard PRAGMA
    table_info guard (fresh DBs already have the column from SCHEMA above, mirroring
    `_mig_add_user_display_name`'s ALTER-guard convention, not `_mig_add_fund_table`'s
    CREATE-guard one, since this is a column addition to an EXISTING table, not a new
    table). `NOT NULL DEFAULT 'none'` means every pre-existing fund row converges to
    exactly the one-time-target behavior it already had -- recurrence is purely
    opt-in, no existing fund's semantics change underneath it. The reserve math
    (tracking.fund_rollup) never reads this column at all; only the new display-layer
    `fund_effective_target_date` / `fund_cycle_summary` functions do."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(fund)").fetchall()]
    if "recurrence" not in cols:
        conn.execute(
            "ALTER TABLE fund ADD COLUMN recurrence TEXT NOT NULL DEFAULT 'none' "
            "CHECK (recurrence IN ('none','yearly'))")


def _mig_add_credit_limit(conn) -> None:
    """Migration 13 (S2.1, DEC-038, docs/reports-accounts-design.md §5): additive
    `account.credit_limit_cents` column -- v13 -> v14. Idempotent via the standard
    PRAGMA table_info guard (fresh DBs already have the column from SCHEMA above,
    mirroring `_mig_add_fund_recurrence`'s ALTER-guard convention -- a column addition
    to an EXISTING table, not a new table). Nullable, no DEFAULT: every pre-existing
    account row converges to `credit_limit_cents IS NULL` ("no limit set"), which is
    exactly its current (unmodeled) behavior -- the running-balance math
    (`tracking.account_balances`) and credit-card rollup (`tracking.card_rollup_running`)
    never read this column at all; only the new S2.2 utilization display will."""
    cols = [r[1] for r in conn.execute("PRAGMA table_info(account)").fetchall()]
    if "credit_limit_cents" not in cols:
        conn.execute("ALTER TABLE account ADD COLUMN credit_limit_cents INTEGER")


def _mig_add_household_budget(conn) -> None:
    """Migration 14 (Slice A, TODO-232, DEC-041, docs/shared-budget-design.md §9/§12):
    additive household shared-budget-layer tables -- v14 -> v15. `CREATE TABLE IF NOT
    EXISTS` makes it idempotent on fresh DBs that already have both tables (+ the
    idx_hbshare_user index) from SCHEMA above (mirrors the goal/venture/user_profile/
    fund/alias convention exactly). Pure additive `CREATE` -- no ALTER, no data rewrite,
    no down-migration needed: an older-code boot against an already-migrated DB simply
    never references these tables, so rollback is safe with nothing further to do.
    HOUSEHOLD-scoped (DEC-030 implicit singleton), deliberately NOT user_id-scoped like
    `fund`/`goal` -- see SCHEMA's comment on `household_budget` above."""
    conn.execute("""CREATE TABLE IF NOT EXISTS household_budget (
  id          INTEGER PRIMARY KEY,
  name        TEXT    NOT NULL,
  bucket      TEXT    NOT NULL,
  type        TEXT    NOT NULL CHECK (type IN ('split','pooled')),
  total_cents INTEGER,
  status      TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
  created_by  TEXT    NOT NULL,
  created_at  TEXT    NOT NULL
)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS household_budget_share (
  line_id            INTEGER NOT NULL REFERENCES household_budget(id) ON DELETE CASCADE,
  user_id            TEXT    NOT NULL,
  split_ratio_bps    INTEGER,
  contribution_cents INTEGER,
  PRIMARY KEY (line_id, user_id)
)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hbshare_user ON household_budget_share(user_id)")


def _mig_add_schedule_tables(conn) -> None:
    """Migration 16 (scheduled money) -- v15 -> v16. Additive tables plus a one-time backfill
    of the old `recurring` bill checklist into `schedule`.

    `CREATE TABLE IF NOT EXISTS` makes the structural half idempotent on fresh DBs that already
    have the tables from SCHEMA (the goal/venture/fund/household convention).

    The BACKFILL is the only part with data in it, and it is guarded to run **only when
    `schedule` is empty**. That is what makes it safe to re-enter: `import_all` runs pending
    migrations after restoring a backup, so an older backup's `recurring` rows convert for free
    on restore -- but a backup that already contains `schedule` rows must never have them
    duplicated by a second conversion of the `recurring` rows sitting beside them.

    `recurring` is deliberately NOT dropped. It stays in `_BACKUP_TABLES` so older backups keep
    validating, and dropping a table is the one migration shape that cannot be rolled back by
    booting older code. Nothing writes to it after this point.

    Conversion, per row: a monthly rule on the recorded `due_day`, the expected amount treated
    as an ESTIMATE (that was always its meaning -- "electricity may bill more or less"), and
    `auto_post` off, because a checklist entry never implied permission to write to the ledger.
    A row with no `due_day` has no date to recur on, so it becomes an inactive schedule rather
    than being silently dropped or silently assigned the 1st.
    """
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule (
  id                 INTEGER PRIMARY KEY,
  user_id            TEXT    NOT NULL DEFAULT '__owner__',
  name               TEXT    NOT NULL,
  direction          TEXT    NOT NULL CHECK (direction IN ('in','out','transfer')),
  amount_cents       INTEGER NOT NULL DEFAULT 0 CHECK (amount_cents >= 0),
  amount_is_estimate INTEGER NOT NULL DEFAULT 0,
  account_id         INTEGER REFERENCES account(id) ON DELETE CASCADE,
  to_account_id      INTEGER REFERENCES account(id) ON DELETE CASCADE,
  bucket             TEXT,
  category           TEXT,
  description        TEXT,
  freq               TEXT    NOT NULL CHECK (freq IN ('daily','weekly','semimonthly','monthly','yearly')),
  interval_n         INTEGER NOT NULL DEFAULT 1 CHECK (interval_n >= 1),
  weekdays           TEXT,
  day_1              INTEGER CHECK (day_1 IS NULL OR (day_1 >= 1 AND day_1 <= 32)),
  day_2              INTEGER CHECK (day_2 IS NULL OR (day_2 >= 1 AND day_2 <= 32)),
  month_of_year      INTEGER CHECK (month_of_year IS NULL OR (month_of_year >= 1 AND month_of_year <= 12)),
  anchor_on          TEXT    NOT NULL,
  end_mode           TEXT    NOT NULL DEFAULT 'never' CHECK (end_mode IN ('never','on','after')),
  ends_on            TEXT,
  end_count          INTEGER,
  weekend_shift      TEXT    NOT NULL DEFAULT 'none' CHECK (weekend_shift IN ('none','before','after')),
  auto_post          INTEGER NOT NULL DEFAULT 0,
  active             INTEGER NOT NULL DEFAULT 1,
  parent_id          INTEGER REFERENCES schedule(id) ON DELETE SET NULL,
  created_at         TEXT    NOT NULL
)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_user   ON schedule(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_schedule_active ON schedule(user_id, active)")
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_exception (
  id            INTEGER PRIMARY KEY,
  schedule_id   INTEGER NOT NULL REFERENCES schedule(id) ON DELETE CASCADE,
  occurrence_on TEXT    NOT NULL,
  action        TEXT    NOT NULL CHECK (action IN ('skip','override')),
  amount_cents  INTEGER,
  moved_to      TEXT,
  description   TEXT,
  created_at    TEXT    NOT NULL,
  UNIQUE (schedule_id, occurrence_on)
)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS schedule_txn (
  schedule_id   INTEGER NOT NULL REFERENCES schedule(id) ON DELETE CASCADE,
  occurrence_on TEXT    NOT NULL,
  txn_id        INTEGER NOT NULL REFERENCES txn(id) ON DELETE CASCADE,
  PRIMARY KEY (schedule_id, occurrence_on)
)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_schedtxn_txn ON schedule_txn(txn_id)")

    # ---- one-time backfill, only into an empty `schedule` table ----
    have = conn.execute("SELECT 1 FROM schedule LIMIT 1").fetchone()
    if have:
        return
    tables = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "recurring" not in tables:
        return
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(recurring)").fetchall()}
    if not {"category", "direction", "expected_cents"} <= cols:
        return
    has_user = "user_id" in cols
    now = _now()
    rows = conn.execute("SELECT * FROM recurring").fetchall()
    for r in rows:
        due = r["due_day"] if "due_day" in cols else None
        # An entry with no due day cannot be given one honestly -- it arrives paused, named, and
        # visible, so it can be finished by hand rather than quietly guessing the 1st.
        active = 1 if (due and r["active"]) else 0
        anchor = f"2000-{1:02d}-{min(int(due or 1), 28):02d}"
        conn.execute(
            "INSERT INTO schedule (user_id, name, direction, amount_cents, amount_is_estimate,"
            " bucket, category, freq, interval_n, day_1, anchor_on, end_mode, weekend_shift,"
            " auto_post, active, created_at)"
            " VALUES (?,?,?,?,1,?,?,'monthly',1,?,?,'never','none',0,?,?)",
            (r["user_id"] if has_user else "__owner__",
             r["category"], r["direction"], int(r["expected_cents"] or 0),
             r["bucket"], r["category"], int(due) if due else 1, anchor, active, now))


def _mig_household_budget_autoincrement(conn) -> None:
    """Migration 17 (BUG-0017) -- v16 -> v17. Stop SQLite reusing a deleted line's id.

    Verified before the fix: created "Netflix" at id 2, hard-deleted it, created an unrelated
    line, and got id 2 back. Any stored reference to that id then points at a line nobody chose to
    share, which is why the invite design has been gated on this bug.

    AUTOINCREMENT cannot be added by ALTER, so this is the standard SQLite table rebuild. Three
    things make it safe to re-enter, which matters because `import_all` runs pending migrations
    after restoring a backup:

      * a NO-OP when the table already declares AUTOINCREMENT -- checked against sqlite_master
        rather than user_version, so even a half-applied state converges;
      * `household_budget_share.line_id` REFERENCES this table ON DELETE CASCADE, so the drop must
        happen with foreign keys OFF or every child row cascades away with it. The pragma is
        restored in a `finally` whatever happens;
      * ids are copied VERBATIM, so every existing reference stays valid, and the high-water mark
        is seeded from MAX(id) so the next insert cannot collide with a live row either.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='household_budget'").fetchone()
    if row is None:
        return                       # table not created yet; the base schema ships it correct
    if "AUTOINCREMENT" in (row[0] or "").upper():
        return                       # already converted (fresh DB, or a re-entered restore)

    # PRAGMA foreign_keys IS SILENTLY IGNORED INSIDE A TRANSACTION. That is not a footnote here:
    # the first version of this migration turned them off, did the rebuild (which opens an implicit
    # transaction on the first write), and then "restored" them in the finally -- where the pragma
    # did nothing at all. Its own test caught it: foreign keys were left OFF for the rest of the
    # connection's life, which disables integrity enforcement everywhere, long after this function
    # returns. Commit on both sides so each pragma is executed outside a transaction.
    fk_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    conn.commit()
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.execute("""CREATE TABLE household_budget__new (
          id          INTEGER PRIMARY KEY AUTOINCREMENT,
          name        TEXT    NOT NULL,
          bucket      TEXT    NOT NULL,
          type        TEXT    NOT NULL CHECK (type IN ('split','pooled')),
          total_cents INTEGER,
          status      TEXT    NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived')),
          created_by  TEXT    NOT NULL,
          created_at  TEXT    NOT NULL
        )""")
        conn.execute(
            "INSERT INTO household_budget__new (id, name, bucket, type, total_cents, status, created_by, created_at) "
            "SELECT id, name, bucket, type, total_cents, status, created_by, created_at FROM household_budget")
        conn.execute("DROP TABLE household_budget")
        conn.execute("ALTER TABLE household_budget__new RENAME TO household_budget")
        # This was an INSERT seeding sqlite_sequence, with a comment claiming the sequence would
        # otherwise start below the live rows. That claim was FALSE and a surviving mutant proved
        # it: SQLite maintains the high-water mark itself on an explicit-id insert (verified —
        # copying ids 1, 5, 9 leaves seq=9 and the next auto id is 10). The write could not fail
        # and could not matter.
        #
        # A check that CAN fail is worth more than a write that cannot. If a future edit changes
        # the copy above to anything that does not carry ids verbatim, this stops the migration
        # instead of letting the next insert collide with a live row.
        top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM household_budget").fetchone()[0]
        seq_row = conn.execute(
            "SELECT seq FROM sqlite_sequence WHERE name='household_budget'").fetchone()
        seq = seq_row[0] if seq_row else 0
        if top and seq < top:
            raise RuntimeError(
                f"migration 17: sqlite_sequence is {seq} but the highest live id is {top} -- "
                "the next insert would collide with an existing row")
    finally:
        conn.commit()
        conn.execute(f"PRAGMA foreign_keys={'ON' if fk_on else 'OFF'}")
        # Verified, not assumed. A pragma that silently no-ops is exactly what went wrong the
        # first time, and a migration that leaves integrity checking off must fail loudly rather
        # than hand back a connection that quietly accepts orphans.
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != fk_on:
            raise RuntimeError(
                "migration 17 could not restore PRAGMA foreign_keys -- refusing to continue with "
                "integrity enforcement in the wrong state")


def _mig_add_fire_progress(conn) -> None:
    """Migration 18 -- v17 -> v18. The FIRE progress log.

    Purely additive: CREATE TABLE IF NOT EXISTS, which is a no-op on any DB that already got the
    table from SCHEMA (the goal/venture/fund/household/schedule convention). Nothing is backfilled
    and nothing could be -- the whole reason this table exists is that historical FI targets are
    unreconstructable, so a backfill would have to invent them.
    """
    conn.executescript(SCHEMA[SCHEMA.index("CREATE TABLE IF NOT EXISTS fire_progress ("):
                              SCHEMA.index(");", SCHEMA.index("CREATE TABLE IF NOT EXISTS fire_progress (")) + 2])


_MIGRATIONS: list = [_mig_add_partner_owed, _mig_drop_bucket_checks, _mig_add_txn_status_kind, _mig_add_invest_group, _mig_add_goal_table, _mig_add_venture_table, _mig_add_users_table, _mig_add_user_scoping, _mig_add_user_profile, _mig_add_fund_table, _mig_add_user_display_name, _mig_add_alias_tables, _mig_add_fund_recurrence, _mig_add_credit_limit, _mig_add_household_budget, _mig_add_schedule_tables, _mig_household_budget_autoincrement, _mig_add_fire_progress]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- connection / init ----------

def connect(path: str | None = None) -> sqlite3.Connection:
    """Open a connection with the project's standard pragmas. `path=':memory:'` for tests."""
    target = path or resolve_db_path()
    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if target != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")      # concurrent readers; not valid for :memory:
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Idempotent: create tables + indexes if absent, then run any pending migrations.
    Safe to call on every startup.

    `SCHEMA` is all `CREATE … IF NOT EXISTS`, so additive tables/indexes apply on every
    boot and need NO migration. `_MIGRATIONS` is only for changes `executescript` can't
    express (ALTER, data backfill); `user_version` tracks how many have run. The runner
    applies `_MIGRATIONS[version:]` and stamps `user_version = count applied` — fresh and
    existing DBs converge (no premature stamp, so the first real migration always runs).

    Raises
    ------
    RuntimeError
        If the linked SQLite is older than `_MIN_SQLITE_VERSION` — `_mig_drop_bucket_checks`
        and the `CHECK` constraints in `SCHEMA` need it (TODO-209). Fails loudly before any
        DDL runs rather than risk a silently-broken schema on an old interpreter.
    """
    if sqlite3.sqlite_version_info < _MIN_SQLITE_VERSION:
        required = ".".join(str(p) for p in _MIN_SQLITE_VERSION)
        raise RuntimeError(
            f"tracking_store requires SQLite >= {required}; this interpreter is linked "
            f"against SQLite {sqlite3.sqlite_version}. Upgrade Python/SQLite before starting "
            "the app — the schema's CHECK constraints and column migrations depend on it."
        )
    conn.executescript(SCHEMA)
    conn.commit()
    version = conn.execute("PRAGMA user_version").fetchone()[0]

    # S1.1 pre-migration safety copy: an established DB (0 < version < 8) is about to
    # cross into multi-tenancy (the `plan_snapshot` rebuild + 9 ALTER ADD COLUMNs).
    # version == 0 means either a genuinely fresh DB (executescript(SCHEMA) never
    # stamps user_version) or one so old no migration has ever run -- either way there
    # is nothing irreplaceable to protect yet, so the copy is skipped. Mirrors
    # import_all's `.pre-import-<ts>.bak` online-backup-API technique (WAL-consistent).
    if 0 < version < 8:
        db_file = _main_db_file(conn)
        if db_file:                                    # '' for :memory: -- nothing to copy
            bak = f"{db_file}.pre-multiuser.bak"
            if not os.path.exists(bak):                 # preserve the TRUE pre-migration snapshot on re-runs
                with closing(sqlite3.connect(bak)) as dest:
                    conn.backup(dest)                   # OSError propagates -> abort boot before any mutation

    # Household shared-budget layer (Slice A) pre-migration safety copy: the spec
    # (docs/shared-budget-design.md §9) explicitly calls for the SAME idempotent
    # pre-migration `.bak` discipline as S1.1/S1.2 above, even though this migration is
    # purely additive CREATE TABLE (no ALTER, no rewrite, no data-loss risk on its own)
    # -- this is money-adjacent household data under mandatory adversarial review
    # (DEC-041), so it gets the belt-and-suspenders treatment those higher-risk
    # migrations get. Same version==0 (fresh DB, nothing to protect) skip and
    # not-exists guard (preserve the TRUE pre-migration snapshot on re-runs) as above.
    # 14 is `_mig_add_household_budget`'s FIXED historical position in `_MIGRATIONS`
    # (mirrors the `8` literal above, a fixed position too, not a computed one) --
    # deliberately NOT `_MIGRATIONS.index(_mig_add_household_budget)`: several tests
    # monkeypatch `store._MIGRATIONS` to a shorter, OLDER-app-version list to simulate
    # upgrade scenarios (e.g. test_profile_store.py's `older_migrations =
    # store._MIGRATIONS[:9]`), and a live `.index()` lookup would raise ValueError the
    # moment that list no longer contains this function at all.
    if 0 < version <= 14:
        db_file = _main_db_file(conn)
        if db_file:
            bak = f"{db_file}.pre-household-budget.bak"
            if not os.path.exists(bak):
                with closing(sqlite3.connect(bak)) as dest:
                    conn.backup(dest)

    for i in range(version, len(_MIGRATIONS)):
        _MIGRATIONS[i](conn)
        # Commit the migration's changes BEFORE stamping the version, each step atomically, so a
        # half-applied migration can never leave user_version ahead of the actual schema (which would
        # permanently skip the migration on the next boot). Migrations are idempotent regardless.
        conn.commit()
        conn.execute(f"PRAGMA user_version = {i + 1}")
        conn.commit()


# ---------- identity (multi-user S0.1, DEC-026/031) ----------

# Stable sentinel user_id for the canonical owner profile, provisioned only when the
# `users` table is otherwise empty and identity resolves to the OWNER fallback (no
# trusted ingress header, no dev override -- DEC-031 §3). Deliberately NOT a valid
# Supervisor-issued 32-hex-char UUID, so it can never collide with a real header id.
_SENTINEL_OWNER_ID = "__owner__"


def resolve_or_provision_user(conn: sqlite3.Connection, user_id: str, display_name: str | None = None) -> dict:
    """Look up *user_id* in the `users` table; provision it if this is the first time
    it's been seen. The very first id ever provisioned becomes 'owner'; every
    subsequently-seen new id becomes 'member' -- at most one owner (also enforced by
    the idx_users_one_owner unique index as a concurrency backstop). Idempotent: an
    id that's already provisioned just returns its stored role unchanged, regardless
    of how many times it's seen again.

    Called ONLY from server.py's resolve_user() for ids obtained via a trusted ingress
    header or the DEC-022 dev override -- never for the no-identity-signal case (see
    resolve_owner_fallback below).

    *display_name* (human-readable roster names): the trimmed, single-valued
    X-Remote-User-Display-Name/-Name header value server.py's resolve_user() captured
    for this request, or None when neither header was present (older Supervisor/Core,
    or the DEC-022 dev override path, which never supplies one). Stored on first
    provisioning; on an ALREADY-provisioned row, a truthy, CHANGED value is written
    over the stored one (people rename their HA account) -- a None or unchanged value
    never overwrites what's on disk, so a request that happens to omit the header
    (or repeats the same name) can't blank out a name learned on an earlier request.
    Deliberately NOT part of this function's RETURN VALUE (still exactly {id, role})
    -- callers that need the roster's display_name/label read it back via list_users()
    or get_user(), keeping this function's contract (and every existing exact-dict-
    equality test against it) unchanged.

    Raises
    ------
    ValueError
        If *user_id* is the reserved owner sentinel (SEV-S1.1-001). server.py's
        resolve_user() already rejects this before ever calling here -- this is
        defense-in-depth so the store itself refuses to provision it even if a future
        caller path forgets that check. Provisioning it would let a real member be
        assigned the literal `"__owner__"` id, which would then collide with (and be
        indistinguishable from) the owner's data-scope slot.
    """
    if user_id == _SENTINEL_OWNER_ID:
        raise ValueError(
            f"resolve_or_provision_user() refuses to provision the reserved owner "
            f"sentinel ({_SENTINEL_OWNER_ID!r}); callers must reject this id before "
            "calling here (SEV-S1.1-001)."
        )
    row = conn.execute(
        "SELECT user_id, role, display_name FROM users WHERE user_id = ?", (user_id,)
    ).fetchone()
    if row is not None:
        if display_name and row["display_name"] != display_name:
            conn.execute(
                "UPDATE users SET display_name = ? WHERE user_id = ?", (display_name, user_id)
            )
            conn.commit()
        return {"id": row["user_id"], "role": row["role"]}
    existing_owner = conn.execute("SELECT 1 FROM users WHERE role = 'owner'").fetchone()
    role = "member" if existing_owner is not None else "owner"
    try:
        conn.execute(
            "INSERT INTO users (user_id, role, display_name, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, display_name, _now()),
        )
        conn.commit()
        return {"id": user_id, "role": role}
    except sqlite3.IntegrityError:
        conn.rollback()

    if role == "owner":
        # Lost the idx_users_one_owner race: another id was committed as owner between
        # our existing_owner check and this INSERT. This id was never written by the
        # failed attempt above, so retry it as 'member' -- the now-correct role given an
        # owner exists.
        try:
            conn.execute(
                "INSERT INTO users (user_id, role, display_name, created_at) VALUES (?, 'member', ?, ?)",
                (user_id, display_name, _now()),
            )
            conn.commit()
            return {"id": user_id, "role": "member"}
        except sqlite3.IntegrityError:
            conn.rollback()

    # Either the 'member' INSERT above also collided, or the original INSERT attempted
    # role='member' and hit the user_id PRIMARY KEY (another caller provisioned this
    # exact id concurrently) -- in both cases the row now exists; return what's on disk.
    row = conn.execute("SELECT user_id, role FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row is not None:
        return {"id": row["user_id"], "role": row["role"]}
    raise


def resolve_owner_fallback(conn: sqlite3.Connection) -> dict:
    """DEC-031 §3: no trusted ingress header AND no dev override -> resolve to the
    OWNER profile, never an unauthenticated picker. Returns whichever user_id is
    currently the household owner if one has already been provisioned (e.g. via an
    earlier header-bearing request); otherwise provisions the canonical sentinel owner
    row so this always yields a concrete {id, role:'owner'} -- covers first boot,
    the sandbox, and pre-header HA Core versions (Supervisor < 2023.08.2 / Core <
    2023.9).

    First-boot sentinel race (same class as SEV-005, see resolve_or_provision_user):
    two concurrent no-header requests can both observe "no owner yet" (our own SELECT
    above) and both attempt to INSERT the sentinel owner row. idx_users_one_owner lets
    only one such INSERT succeed; the loser's INSERT raises sqlite3.IntegrityError. The
    winner already committed a real owner row, so the loser just re-SELECTs and returns
    it -- idempotent, no 500."""
    row = conn.execute("SELECT user_id FROM users WHERE role = 'owner' LIMIT 1").fetchone()
    if row is not None:
        return {"id": row["user_id"], "role": "owner"}
    try:
        conn.execute(
            "INSERT INTO users (user_id, role, created_at) VALUES (?, 'owner', ?)",
            (_SENTINEL_OWNER_ID, _now()),
        )
        conn.commit()
        return {"id": _SENTINEL_OWNER_ID, "role": "owner"}
    except sqlite3.IntegrityError:
        conn.rollback()

    # Lost the idx_users_one_owner race: another request's owner INSERT (sentinel or
    # real id) committed between our existing_owner check and this INSERT. That row is
    # the household's owner now -- return it instead of surfacing a 500.
    row = conn.execute("SELECT user_id FROM users WHERE role = 'owner' LIMIT 1").fetchone()
    if row is not None:
        return {"id": row["user_id"], "role": "owner"}
    raise


def list_users(conn: sqlite3.Connection) -> list[dict]:
    """Every provisioned household member, owner first then by provisioning order.
    Used by GET /api/whoami's owner-only roster view -- never exposed to members
    (server.py enforces that). `displayName`/`label` are the human-readable-names
    columns (header-captured / owner-edited respectively) -- either may be None;
    render-time precedence (label wins) is the caller's job (index.html's
    owner-transfer picker), not this function's.

    ALIASED IDS ARE FILTERED OUT (account linking): a `user_id` that currently appears
    as a `user_alias.alias_id` is no longer an independent household member -- every
    request that resolves to it now resolves to its PRIMARY's {id, role, scopeId}
    instead (server.py's resolve_user() -> tracking_store.resolve_identity()), so
    listing it here as its own roster row would be misleading (it can't be transferred
    ownership to, labeled meaningfully, or distinguished from its primary in practice).
    The underlying `users` row is NOT deleted (still reachable via get_user(), and its
    own get_user()-shape data is used by list_linked_accounts() to name it in the
    owning persona's "linked accounts" list) -- only this OWNER-ROSTER view hides it."""
    rows = conn.execute(
        "SELECT user_id, role, display_name, label, created_at FROM users "
        "WHERE user_id NOT IN (SELECT alias_id FROM user_alias) "
        "ORDER BY (role != 'owner'), created_at"
    ).fetchall()
    return [
        {
            "id": r["user_id"],
            "role": r["role"],
            "displayName": r["display_name"],
            "label": r["label"],
            "createdAt": r["created_at"],
        }
        for r in rows
    ]


def get_user(conn: sqlite3.Connection, user_id: str) -> dict | None:
    """Single roster row (id/role/displayName/label/createdAt), or None if *user_id*
    has never been provisioned. `list_users()` is owner-only (server.py never exposes
    the full roster to a member); this is the per-id lookup GET /api/whoami uses to
    attach a MEMBER's own displayName/label to their self-only response."""
    row = conn.execute(
        "SELECT user_id, role, display_name, label, created_at FROM users WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": row["user_id"],
        "role": row["role"],
        "displayName": row["display_name"],
        "label": row["label"],
        "createdAt": row["created_at"],
    }


class UserLabelError(ValueError):
    """400: *user_id* is the reserved `__owner__` data-scope sentinel -- never a real
    household member (SEV-S1.1-001), so it can never receive an owner-editable label.
    Mirrors OwnerTransferError's same rejection for the same reason."""


class UnknownUserError(LookupError):
    """404: *user_id* has never been provisioned in the `users` roster. Identity is
    lazily provisioned (DEC-026/031) -- a member must open the app at least once
    before the owner can label them."""


def set_user_label(conn: sqlite3.Connection, user_id: str, label: str | None) -> dict:
    """Owner-editable label fallback (server.py's PATCH /api/tracking/users/{id}) --
    covers pre-header Supervisor versions (never send X-Remote-User-Display-Name/-Name)
    and service accounts that have no meaningful HA display name. `label` wins over
    `display_name` at render time.

    *label* must already be stripped and length-validated by the caller (server.py) --
    this function only enforces the identity-level invariants below. Pass None (not
    "") to clear a previously-set label.

    Raises
    ------
    UserLabelError
        *user_id* is the reserved owner sentinel (SEV-S1.1-001) -- it is the owner's
        internal data-scope slot, never a real household member, so it can never
        receive a label (mirrors transfer_ownership's identical rejection).
    UnknownUserError
        *user_id* has never been provisioned.
    """
    if user_id == _SENTINEL_OWNER_ID:
        raise UserLabelError(
            f"{_SENTINEL_OWNER_ID!r} is the reserved owner data-scope sentinel, not a "
            "real household member -- it can never receive a label."
        )
    row = conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row is None:
        raise UnknownUserError(
            f"{user_id!r} has never opened the app -- a member must be seen at least "
            "once (lazily provisioned) before the owner can label them."
        )
    conn.execute("UPDATE users SET label = ? WHERE user_id = ?", (label, user_id))
    conn.commit()
    return {"id": user_id, "label": label}


class OwnerTransferError(ValueError):
    """400: the requested transfer target is semantically invalid -- either it's already
    the current owner (no-op) or it's the reserved `__owner__` data-scope sentinel, which
    is never a real household member (SEV-S1.1-001)."""


class UnknownTransferTargetError(LookupError):
    """404: *to_user_id* has never been provisioned in the `users` roster. Identity is
    lazily provisioned (DEC-026/031) -- a member must open the app at least once before
    they can be handed the owner seat."""


class OwnerTransferConflictError(RuntimeError):
    """409: either (a) a concurrent/stale transfer lost the race -- the promote collided
    with `idx_users_one_owner` (exactly one owner survived, invariant intact); or (b) the
    household shared-budget layer (Slice A, TODO-232, DEC-041) found a share-remap
    collision -- see transfer_ownership's docstring "HOUSEHOLD_BUDGET_SHARE IS
    PERSON-SCOPED, NOT SEAT-SCOPED" section. Reload and retry from whoever currently
    holds the seat (case a), or resolve the colliding share manually first (case b)."""


def transfer_ownership(conn: sqlite3.Connection, current_owner_id: str, to_user_id: str) -> dict:
    """Move the household owner seat from *current_owner_id* to *to_user_id* -- the
    in-app owner reassignment SEV-004 explicitly deferred (0.2.1, addon/DOCS.md). Called
    ONLY from server.py's owner-only POST /api/tracking/owner-transfer.

    ZERO DATA MOVEMENT for every EXCLUSIVELY-scoped table (the entire point of this
    design, and still true for account/txn/goal/venture/fund/user_profile/etc.): each of
    those tables is scoped by `resolve_user()["scopeId"]`, which is `'__owner__'` for the
    owner role and a member's raw HA id otherwise -- NEVER by `id` (see resolve_user()'s
    scopeId formula and DEC-033), and EACH ROW belongs to exactly one scope. Once
    *to_user_id* holds `role='owner'`, their very next request resolves `scopeId =
    '__owner__'` and they see EVERY bit of the data the old owner had -- transactions,
    accounts, funds, their synced profile blob, all of it -- with not one such row
    copied, moved, or touched. Callers/tests should assert this by checking the `users`
    table diff plus the household_budget_share remap below (documented next) is the
    COMPLETE diff -- no other table's row count or content changes.

    HOUSEHOLD_BUDGET_SHARE IS PERSON-SCOPED, NOT SEAT-SCOPED -- the ONE exception (Slice
    A, TODO-232, DEC-041, review finding 2026-08-06): unlike every table above,
    `household_budget_share` puts MULTIPLE members' scopeIds as VALUES on the SAME
    `line_id` row (Alice's 60% and Bob's 40% are sibling rows on one shared line) --
    "the seat carries the data" is exactly WRONG here. Left alone, a transfer would
    reattribute the outgoing owner's committed share to whoever now holds `'__owner__'`
    (real money misattributed to the wrong person) WHILE orphaning the incoming owner's
    own pre-existing share under their old raw id (a share they still owe becomes
    invisible, and un-editable -- `_validate_household_shares` rejects any further edit
    referencing an id no longer held by a current member). So money must follow the
    PERSON here: this function SWAPS the two scope values in `household_budget_share`
    (`'__owner__'` <-> *to_user_id*) in the SAME transaction as the role flip, so a
    share keyed to whichever identity a person now holds still resolves to THEM,
    not to whoever now sits in the seat. Two footnotes:
      - If *current_owner_id* itself IS the literal `'__owner__'` sentinel (the
        no-header/no-override fallback identity, DEC-031 §3 -- `users.user_id ==
        '__owner__'` even though `role` is changing), their post-transfer `scopeId` is
        STILL `'__owner__'` (scopeId is role-derived, and their raw `id` happens to
        equal that string too) -- no half of the swap is needed for them; only
        *to_user_id*'s shares move onto `'__owner__'`.
      - COLLISION GUARD: if a shared line has a share row for BOTH `'__owner__'` and
        *to_user_id* already (i.e. the outgoing and incoming owner already co-participate
        in the same line), the swap would try to land two different amounts on the same
        `(line_id, '__owner__')` slot. Rather than silently overwrite or merge money,
        this is checked BEFORE any mutation and raises `OwnerTransferConflictError`
        (409) -- warn-never-overwrite, same posture as DEC-037. Resolve the colliding
        share manually (e.g. archive/edit one side) before retrying the transfer.

    ORPHANED MEMBER-SCOPE DATA on every OTHER (exclusively-scoped) table (documented,
    not merged -- deliberately out of scope; see addon/DOCS.md): if *to_user_id* had
    already logged data while still a member (their own transactions, their own synced
    profile blob), that data lives under THEIR RAW ID, which after this swap belongs to
    nobody's current scope (the demoted owner's new member scope is their OWN raw id --
    never the promoted member's). That data is not deleted; it simply becomes
    unreachable unless *to_user_id* is later demoted back to member (a reverse
    transfer), at which point it reappears exactly as left. No automatic merge is
    performed, and none is planned -- flag as a follow-up if a real household ever needs
    it. (household_budget_share does NOT have this orphaning problem -- see above; that
    is precisely why it needs the opposite treatment.)

    Atomicity: the household_budget_share remap AND both `users` UPDATEs run in ONE
    transaction. Python's `sqlite3` module opens an implicit transaction before the
    first DML statement after a commit and holds it open until `commit()`/`rollback()`
    -- so a process crash midway can never be observed as a half-swapped state: either
    everything lands together (on `commit()`) or nothing does (nothing was durably
    written; the prior owner and every share are unchanged on the next connection).
    ORDER MATTERS for the `users` UPDATEs: the current owner is demoted to 'member'
    FIRST (transiently zero owners), THEN the target is promoted to 'owner' (back to
    exactly one) -- reversing the order would collide with `idx_users_one_owner`, the
    partial unique index that enforces at-most-one-owner on every individual statement,
    not just at commit. That same index also stands as a concurrency backstop: if some
    future caller bug ever left two rows claiming 'owner' mid-transaction, SQLite
    refuses the second UPDATE outright (IntegrityError) rather than silently producing
    two owners.

    Raises
    ------
    OwnerTransferError
        Maps to 400 in server.py. *to_user_id* is *current_owner_id* (no-op transfer) or
        the reserved `_SENTINEL_OWNER_ID` sentinel.
    UnknownTransferTargetError
        Maps to 404 in server.py. *to_user_id* has never been provisioned -- they must
        open the app at least once first. ALSO raised (fail-closed, account linking) when
        *to_user_id* is currently a `user_alias.alias_id`: an aliased id never
        independently resolves through resolve_user() any more (every request using it
        resolves to its PRIMARY instead -- resolve_identity()), so promoting its `users`
        row to `role='owner'` would strand the owner seat on an id nobody can ever
        activate live. Transfer to the alias's PRIMARY id instead -- which, once owner,
        already carries the seat for every account linked to it (see
        docs/multiuser-household-plan.md and tests/test_account_linking.py for the
        "transfer to a primary with aliases moves the seat for all of them" property).
    """
    if to_user_id == _SENTINEL_OWNER_ID:
        raise OwnerTransferError(
            f"{_SENTINEL_OWNER_ID!r} is the reserved owner data-scope sentinel, not a "
            "real household member -- it can never be a transfer target."
        )
    if to_user_id == current_owner_id:
        raise OwnerTransferError("toUserId is already the current owner; nothing to transfer.")
    alias_row = conn.execute(
        "SELECT primary_user_id FROM user_alias WHERE alias_id = ?", (to_user_id,)
    ).fetchone()
    if alias_row is not None:
        raise UnknownTransferTargetError(
            f"{to_user_id!r} is a linked account, not an independent member -- transfer "
            f"to its primary account ({alias_row['primary_user_id']!r}) instead."
        )
    target = conn.execute(
        "SELECT user_id FROM users WHERE user_id = ?", (to_user_id,)
    ).fetchone()
    if target is None:
        raise UnknownTransferTargetError(
            f"{to_user_id!r} has never opened the app -- a member must be seen at least "
            "once (lazily provisioned) before they can receive the owner seat."
        )
    # NOTE: to_user_id is intentionally NOT trimmed/normalized here — the target must
    # exactly match a stored users.user_id, which the resolver already trimmed at
    # provisioning time. Normalizing here could open a resolver/transfer mismatch.

    # Household shared-budget layer (Slice A, TODO-232, DEC-041) collision guard -- see
    # this function's "HOUSEHOLD_BUDGET_SHARE IS PERSON-SCOPED" docstring section.
    # Checked BEFORE any mutation (fail-closed, warn-never-overwrite): a shared line
    # where BOTH the outgoing owner and the incoming owner already have a share row
    # cannot be swapped without landing two different amounts on the same
    # (line_id, '__owner__') slot.
    colliding = conn.execute(
        "SELECT a.line_id FROM household_budget_share a "
        "JOIN household_budget_share b ON b.line_id = a.line_id "
        "WHERE a.user_id = '__owner__' AND b.user_id = ?",
        (to_user_id,),
    ).fetchall()
    if colliding:
        raise OwnerTransferConflictError(
            f"{to_user_id!r} already has a share on shared budget line(s) "
            f"{[r['line_id'] for r in colliding]} that the outgoing owner also "
            "participates in -- resolve or remove one side's share before "
            "transferring ownership (money cannot be silently merged)."
        )

    try:
        # Household shared-budget layer: swap the two scope values so a share stays
        # attributed to the PERSON, not the seat (the collision guard above already
        # proved this cannot collide). Skip the '__owner__' -> current_owner_id half
        # when current_owner_id IS the literal sentinel -- their scopeId is unchanged.
        if current_owner_id != _SENTINEL_OWNER_ID:
            conn.execute(
                "UPDATE household_budget_share SET user_id = ? WHERE user_id = '__owner__'",
                (current_owner_id,),
            )
        conn.execute(
            "UPDATE household_budget_share SET user_id = '__owner__' WHERE user_id = ?",
            (to_user_id,),
        )
        conn.execute(
            "UPDATE users SET role = 'member' WHERE user_id = ? AND role = 'owner'",
            (current_owner_id,),
        )
        conn.execute(
            "UPDATE users SET role = 'owner' WHERE user_id = ?",
            (to_user_id,),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        # A losing concurrent/stale transfer collides with idx_users_one_owner on the
        # promote. The one-owner invariant held; surface it as a typed conflict (409 at
        # the endpoint) instead of an unhandled 500.
        conn.rollback()
        raise OwnerTransferConflictError(
            "ownership changed concurrently -- reload and retry from the current owner"
        ) from exc
    except Exception:
        conn.rollback()
        raise
    return {"previousOwnerId": current_owner_id, "newOwnerId": to_user_id}


# ---------- account linking (identity aliases -- "appoint admins" via linking) ----------
#
# User requirement this satisfies (condensed): "I want both my admin accounts in HA to
# be the same profile because both are me, but I want other users to have their own
# dedicated instance." I.e. N HA accounts -> 1 profile (persona), opt-in per account via
# a link-code handshake; unlinked accounts stay dedicated personas. Every account linked
# to a persona shares that persona's role wholesale -- linking the owner's second login
# to their primary IS how "appoint an admin" falls out of this design, with no separate
# admin-grant concept needed.

_LINK_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O/1/I -- typo-resistant
_LINK_CODE_LENGTH = 8
_LINK_CODE_TTL_SECONDS = 600  # 10 minutes


class LinkError(ValueError):
    """400: the link-code redemption request is structurally invalid -- an unknown,
    expired, or already-used code, or joiner == issuer (linking an account to itself).
    Codes are looked up by hash only (the plaintext is never stored), so "unknown",
    "expired", and "already used" are DELIBERATELY not distinguished in the message --
    there is no oracle for telling an attacker which failure mode they hit."""


class LinkOwnerSeatConflictError(RuntimeError):
    """409: the joiner currently holds the household owner seat. Linking the owner away
    from their own seat would silently strand `scopeId='__owner__'` -- nobody would
    resolve to it any more until a future owner-transfer, an availability bug dressed as
    a feature. The fix is directional: issue the code FROM the owner account (so the
    owner becomes the primary and the other account becomes its alias), not the reverse."""


class LinkChainConflictError(RuntimeError):
    """409: the joiner is ALREADY a primary for one or more existing aliases. Linking it
    to a NEW primary would leave those existing aliases pointing at an id that is now
    itself an alias -- a two-hop chain, forbidden by design (see redeem_link_code's
    docstring for the full scenario this prevents). Also raised on a genuine concurrency
    collision (two requests linking the same joiner at once) -- same remediation either
    way: reload and retry from one of the already-linked accounts."""


class UnknownAliasError(LookupError):
    """404: the given alias id has no `user_alias` row -- never linked, or already
    unlinked."""


class AliasNotOwnedError(RuntimeError):
    """403: the caller's resolved identity does not own this alias (isn't its
    `primary_user_id`) -- only the persona that owns a link (from any of ITS OWN
    sessions) may remove it."""


def _hash_link_code(code: str) -> str:
    """SHA-256 of the code, UPPERCASED first so client-side casing (or a raw API caller
    that skips the UI's uppercase-on-submit) never causes a false "invalid code" --
    codes are generated uppercase-only (see `_LINK_CODE_ALPHABET`), so normalizing here
    makes comparison case-insensitive without weakening the code's entropy."""
    return hashlib.sha256(code.strip().upper().encode("utf-8")).hexdigest()


def resolve_alias(conn: sqlite3.Connection, seen_id: str) -> str:
    """One-hop alias -> primary lookup. Returns *seen_id* unchanged when it has no
    `user_alias` row (the overwhelmingly common case -- an ordinary, unlinked identity).

    Deliberately does NOT loop/recurse to walk a chain: chains are structurally
    forbidden by construction (redeem_link_code() refuses to create one -- see its
    docstring), so a second hop should never exist. If one somehow did (a bug, or a
    hand-edited DB), resolving only one hop fails towards the alias's IMMEDIATE primary
    rather than silently walking an unbounded/cyclic chain -- a safer failure mode than
    "resolve identity by looping until it stops changing."""
    row = conn.execute(
        "SELECT primary_user_id FROM user_alias WHERE alias_id = ?", (seen_id,)
    ).fetchone()
    return row["primary_user_id"] if row is not None else seen_id


def resolve_identity(conn: sqlite3.Connection, seen_id: str, display_name: str | None = None) -> dict:
    """The identity-resolution step server.py's resolve_user() delegates to for every
    concrete *seen_id* (trusted header or DEC-022 dev override) -- collapses an alias id
    to its primary BEFORE any provisioning/role lookup, so a linked account NEVER gets
    its own `users` row from this point on: it resolves to the EXACT SAME {id, role}
    the primary itself would get on its own request. This is what makes linking
    transitive for role: once the primary is (or later becomes) the household owner,
    EVERY account aliased to it resolves `role='owner'` too, with zero extra code at the
    role-lookup layer -- see transfer_ownership()'s docstring for the "moves the seat for
    all linked accounts" property this produces.

    Ordinary (never-aliased) ids pass through unchanged to resolve_or_provision_user() --
    byte-identical to this function not existing, preserving every existing test/caller
    of that function.

    The `_SENTINEL_OWNER_ID` special case: resolve_or_provision_user() REFUSES to
    provision the sentinel (SEV-S1.1-001) -- it only ever exists via
    resolve_owner_fallback()'s no-header path. If an alias's primary IS the sentinel (the
    household owner, resolved via the no-signal fallback, issued the link code), this
    function routes to resolve_owner_fallback() instead so that case still resolves
    cleanly. *display_name* is not applied in that branch -- the fallback path has never
    captured one (no header exists to supply it there either)."""
    primary_id = resolve_alias(conn, seen_id)
    if primary_id == _SENTINEL_OWNER_ID:
        return resolve_owner_fallback(conn)
    return resolve_or_provision_user(conn, primary_id, display_name=display_name)


def create_link_code(conn: sqlite3.Connection, issuer_user_id: str) -> dict:
    """Issue a single-use, 10-minute account-linking code for *issuer_user_id* -- ANY
    signed-in persona may call this (owner or member; there is no owner-only gate --
    linking is "make another one of MY accounts share this profile," not a household-
    admin action). *issuer_user_id* is already a PRIMARY by construction: server.py's
    resolve_user() collapses any pre-existing alias to its primary before an endpoint
    ever sees a caller's id, so this function never has to re-check that itself.

    The plaintext code is returned exactly ONCE, here -- only its SHA-256 hash
    (`_hash_link_code`) is ever persisted, so a DB read or backup snapshot can never
    recover a still-live code. Generating a new code DELETES the issuer's prior
    outstanding code first (rate-limit sanity: at most one live code per issuer at a
    time -- an old, possibly-shared code can't linger as a second valid path in).

    Returns ``{"code": <8-char plaintext>, "expiresAt": <iso8601>}``.
    """
    conn.execute("DELETE FROM link_code WHERE issuer_user_id = ?", (issuer_user_id,))
    code = "".join(secrets.choice(_LINK_CODE_ALPHABET) for _ in range(_LINK_CODE_LENGTH))
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=_LINK_CODE_TTL_SECONDS)
    ).isoformat(timespec="seconds")
    conn.execute(
        "INSERT INTO link_code (code_hash, issuer_user_id, expires_at, used) VALUES (?, ?, ?, 0)",
        (_hash_link_code(code), issuer_user_id, expires_at),
    )
    conn.commit()
    return {"code": code, "expiresAt": expires_at}


def _collapse_household_budget_shares_onto_primary(conn: sqlite3.Connection, alias_scope: str,
                                                    primary_scope: str) -> None:
    """Household shared-budget layer (Slice A, TODO-232, DEC-041, review finding
    2026-08-06) counterpart to `transfer_ownership`'s share swap: `redeem_link_code`
    collapses *alias_scope*'s IDENTITY onto *primary_scope* going forward, and every
    OTHER exclusively-scoped table's data legitimately orphans under the alias's old raw
    id (documented, recoverable on unlink) -- but `household_budget_share` cannot follow
    that pattern: an orphaned share on a MULTI-person row becomes an id no current
    member holds, which permanently blocks any further edit to that line
    (`_validate_household_shares`' membership check) and hides money someone still
    owes. So this table's rows are COLLAPSED (moved), not orphaned, in the same
    transaction as the alias INSERT:

      - *primary_scope* has no row on a given line yet: plain rename
        (alias_scope -> primary_scope).
      - *primary_scope* ALREADY has a row on that line (both HA logins had
        independently committed as if they were two different household members,
        before linking revealed they're the same real person): the two entries are the
        SAME human's money counted under two identities -- SUM the type-appropriate
        money field into *primary_scope*'s row (preserves the line's
        Σ ratio_bps == 10000 invariant: two summands already part of the total collapse
        into one addend of the same combined size) and drop the now-redundant alias row.

    ONE-WAY, unlike every other table's orphan-then-reappear-on-unlink semantics
    (`unlink_alias`'s docstring): once two rows are summed together there is no general
    way to un-sum them, so a later `unlink_alias` does NOT restore *alias_scope*'s
    separate share -- they simply have none until a household member re-adds them as a
    participant. This is the deliberate, narrower trade-off DEC-041's review accepted
    over the alternative (freezing the line) -- see docs/shared-budget-design.md's
    Slice A notes and TODO-247-style follow-up "#6 stale-share repair affordance"."""
    if alias_scope == primary_scope:
        return
    alias_rows = conn.execute(
        "SELECT line_id, split_ratio_bps, contribution_cents FROM household_budget_share "
        "WHERE user_id = ?", (alias_scope,)
    ).fetchall()
    for r in alias_rows:
        existing = conn.execute(
            "SELECT split_ratio_bps, contribution_cents FROM household_budget_share "
            "WHERE line_id = ? AND user_id = ?", (r["line_id"], primary_scope)
        ).fetchone()
        if existing is None:
            conn.execute(
                "UPDATE household_budget_share SET user_id = ? WHERE line_id = ? AND user_id = ?",
                (primary_scope, r["line_id"], alias_scope))
        else:
            merged_ratio = None
            if r["split_ratio_bps"] is not None or existing["split_ratio_bps"] is not None:
                merged_ratio = (r["split_ratio_bps"] or 0) + (existing["split_ratio_bps"] or 0)
            merged_contribution = None
            if r["contribution_cents"] is not None or existing["contribution_cents"] is not None:
                merged_contribution = (r["contribution_cents"] or 0) + (existing["contribution_cents"] or 0)
            conn.execute(
                "UPDATE household_budget_share SET split_ratio_bps = ?, contribution_cents = ? "
                "WHERE line_id = ? AND user_id = ?",
                (merged_ratio, merged_contribution, r["line_id"], primary_scope))
            conn.execute(
                "DELETE FROM household_budget_share WHERE line_id = ? AND user_id = ?",
                (r["line_id"], alias_scope))


def redeem_link_code(conn: sqlite3.Connection, joiner_user_id: str, code: str) -> dict:
    """Redeem a link code (POST /api/tracking/link) -- the second half of the two-sided
    handshake proving control of both accounts. *joiner_user_id* is the CALLER's
    already-resolved identity (server.py's resolve_user() ran first, so this is already
    collapsed through any pre-existing alias of the JOINER'S OWN to a primary -- see
    resolve_identity()'s docstring). On success, *joiner_user_id* becomes an alias of the
    code's issuer's primary.

    EFFECT: every future request that resolves to *joiner_user_id* (any header/dev-
    override that used to reach it) instead resolves to the issuer's primary's {id,
    role, scopeId} -- see resolve_identity(). *joiner_user_id*'s own `users` row, if one
    exists (they may have already been a provisioned member before linking), is left
    completely untouched by this function: it becomes ORPHANED, not deleted or merged --
    identical semantics to transfer_ownership()'s orphaned member-scope data, for every
    EXCLUSIVELY-scoped table (account/txn/goal/venture/fund/user_profile/etc). It is
    recoverable via DELETE /api/tracking/link/{aliasId} (unlink()), at which point
    *joiner_user_id* reverts to resolving as its own persona and that row (and any data
    scoped to it) reappears exactly as left.

    EXCEPTION -- household_budget_share (Slice A, TODO-232, DEC-041, review finding
    2026-08-06): orphaning would block every further edit to a line the joiner
    participated in (`_validate_household_shares` rejects a stale id) and hide money
    they still owe. `_collapse_household_budget_shares_onto_primary` (called BEFORE the
    alias INSERT, same transaction) moves -- and, where the primary already has a share
    on the same line, SUMS -- the joiner's shares onto the primary's scope instead. This
    is ONE-WAY: unlike every other table, a later unlink does NOT restore the joiner's
    separate share (see that function's docstring).

    Validations, ALL fail-closed, checked in this order:
      1. *joiner_user_id* is the reserved `_SENTINEL_OWNER_ID` -- rejected outright.
         Belt-and-braces alongside #4 below: the sentinel only ever IS the current owner
         (resolve_owner_fallback's no-header path), so #4 already catches it
         structurally -- this is defense-in-depth against a future refactor of #4.
      2. The code: looked up by hash, must exist, be unexpired, and unused. Any failure
         raises `LinkError` (400) with one generic message -- unknown, expired, and
         already-used are deliberately NOT distinguished (no oracle for enumerating
         which failure mode a guessed code hit).
      3. `joiner_user_id == issuer_user_id` (self-link) -- `LinkError` (400).
      4. *joiner_user_id* currently holds the household owner seat -- raises
         `LinkOwnerSeatConflictError` (409): see that class's docstring for why.
      5. *joiner_user_id* is ALREADY a primary for one or more existing `user_alias`
         rows -- raises `LinkChainConflictError` (409). Concrete scenario this blocks:
         suppose A is already aliased to B (`user_alias(alias_id=A, primary_user_id=B)`).
         B's OWN session (which now resolves as B, per resolve_identity) tries to redeem
         a code issued by C. Without this check, the code would succeed and insert
         `user_alias(alias_id=B, primary_user_id=C)` -- now A resolves to B, which itself
         resolves to C: a two-hop chain, silently re-parenting A's identity onto C, who
         A's actual human never agreed to share a profile with. Checked via
         `EXISTS (SELECT 1 FROM user_alias WHERE primary_user_id = joiner_user_id)`,
         which also correctly catches the "joiner's raw id is itself already an alias"
         case for free: if joiner_user_id's raw id already resolved to a primary before
         reaching here, THAT primary is what's being tested here as "joiner_user_id" --
         and if IT has other aliases pointing at it, the same chain risk applies.

    The code row is marked `used = 1` (not deleted) so redemption is auditable and a
    second redemption attempt with the same code always fails validation #2, never
    silently re-links. On a genuine concurrent collision at the final INSERT (two
    requests linking the same joiner at once -- `user_alias.alias_id` PRIMARY KEY),
    the whole transaction (including the `used = 1` flip) rolls back and
    `LinkChainConflictError` is raised so the code remains valid for a clean retry.

    Returns ``{"aliasId": <joiner_user_id>, "primaryUserId": <issuer's primary id>}``.
    """
    if joiner_user_id == _SENTINEL_OWNER_ID:
        raise LinkOwnerSeatConflictError(
            "you hold the owner seat -- issue the code from this account instead, or "
            "transfer the seat first"
        )
    row = conn.execute(
        "SELECT issuer_user_id, expires_at, used FROM link_code WHERE code_hash = ?",
        (_hash_link_code(code),),
    ).fetchone()
    if row is None or row["used"] or row["expires_at"] < _now():
        raise LinkError("this link code is invalid, expired, or already used")
    issuer_user_id = row["issuer_user_id"]
    if joiner_user_id == issuer_user_id:
        raise LinkError("cannot link an account to itself")
    owner_row = conn.execute("SELECT user_id FROM users WHERE role = 'owner'").fetchone()
    if owner_row is not None and owner_row["user_id"] == joiner_user_id:
        raise LinkOwnerSeatConflictError(
            "you hold the owner seat -- issue the code from this account instead, or "
            "transfer the seat first"
        )
    chain_row = conn.execute(
        "SELECT 1 FROM user_alias WHERE primary_user_id = ?", (joiner_user_id,)
    ).fetchone()
    if chain_row is not None:
        raise LinkChainConflictError(
            "this account already has other accounts linked to it -- unlink them first, "
            "or issue the code from one of those linked accounts instead"
        )
    # SEV-001 (2026-07-23 audit): the no-chain invariant must hold on the ISSUER side too.
    # A code issued while standalone must die the moment its issuer links away — otherwise
    # a stale code forms user_alias(A->B) while user_alias(B->C) exists.
    if conn.execute(
        "SELECT 1 FROM user_alias WHERE alias_id = ?", (issuer_user_id,)
    ).fetchone() is not None:
        raise LinkChainConflictError(
            "the account that issued this code has since been linked to another profile -- "
            "generate a fresh code from that profile"
        )
    # SEV-002 (2026-07-23 audit): the consume IS the single-use gate — atomic
    # compare-and-swap so two concurrent redeems of one code can never both pass the
    # earlier SELECT and both link.
    cur = conn.execute(
        "UPDATE link_code SET used = 1 WHERE code_hash = ? AND used = 0",
        (_hash_link_code(code),),
    )
    if cur.rowcount != 1:
        conn.rollback()
        raise LinkError("this link code is invalid, expired, or already used")
    try:
        # Household shared-budget layer (Slice A, TODO-232, DEC-041): collapse the
        # joiner's shares onto the issuer's primary scope BEFORE the alias insert, in
        # the SAME transaction -- see _collapse_household_budget_shares_onto_primary's
        # docstring for why this table cannot use the orphan-on-link pattern every other
        # table uses. joiner_user_id is confirmed not the owner seat above (check #4),
        # so their own scopeId IS their raw id; the issuer's current scopeId is
        # '__owner__' if they hold the owner seat, else their own raw id (same
        # role-derived formula resolve_user() uses).
        issuer_role_row = conn.execute(
            "SELECT role FROM users WHERE user_id = ?", (issuer_user_id,)
        ).fetchone()
        issuer_scope = _SENTINEL_OWNER_ID if (issuer_role_row and issuer_role_row["role"] == "owner") else issuer_user_id
        _collapse_household_budget_shares_onto_primary(conn, joiner_user_id, issuer_scope)

        conn.execute(
            "INSERT INTO user_alias (alias_id, primary_user_id, linked_at) VALUES (?, ?, ?)",
            (joiner_user_id, issuer_user_id, _now()),
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise LinkChainConflictError(
            "this account was linked concurrently by another request -- reload and retry"
        ) from exc
    return {"aliasId": joiner_user_id, "primaryUserId": issuer_user_id}


def unlink_alias(conn: sqlite3.Connection, caller_id: str, alias_id: str) -> dict:
    """Remove a link (DELETE /api/tracking/link/{aliasId}). Callable by the persona that
    owns the alias, from ANY of that persona's currently-linked sessions -- including the
    primary's own session, or a DIFFERENT alias of the same primary -- because
    server.py's resolve_user() has already collapsed *caller_id* through any alias of ITS
    OWN before this is ever called, so *caller_id* here is always the caller's resolved
    persona id, regardless of which physical HA login made the request.

    After removal, *alias_id* reverts to resolving as its OWN persona again on its very
    next request -- its pre-link `users` row (never deleted, only orphaned by
    redeem_link_code()) becomes reachable again, byte-for-byte unchanged (identical
    orphan-then-reappear semantics to transfer_ownership()'s reverse-transfer case).

    Raises
    ------
    UnknownAliasError
        404 -- *alias_id* has no `user_alias` row (never linked, or already unlinked).
    AliasNotOwnedError
        403 -- *caller_id* does not own this alias (isn't its `primary_user_id`).
    """
    row = conn.execute(
        "SELECT primary_user_id FROM user_alias WHERE alias_id = ?", (alias_id,)
    ).fetchone()
    if row is None:
        raise UnknownAliasError(f"{alias_id!r} is not a linked account.")
    if row["primary_user_id"] != caller_id:
        raise AliasNotOwnedError("you do not own this linked account.")
    conn.execute("DELETE FROM user_alias WHERE alias_id = ?", (alias_id,))
    conn.commit()
    return {"aliasId": alias_id, "unlinkedFrom": row["primary_user_id"]}


def list_linked_accounts(conn: sqlite3.Connection, primary_user_id: str) -> list[dict]:
    """Every alias currently pointing at *primary_user_id* -- GET /api/whoami's
    `linkedAccounts` field. Unlike `list_users()` (owner-only, whole-household), this is
    ALWAYS scoped to the CALLER'S OWN resolved persona -- any signed-in user (owner or
    member) sees only their own linked accounts, never anyone else's.

    `displayName`/`label` are read from the alias id's own (now-orphaned) `users` row --
    it always has one by construction (redeem_link_code() only ever aliases an id that
    just resolved ITS OWN identity via resolve_user(), which lazily provisions it first),
    but the lookup defensively falls back to id-only fields (None/None) if that invariant
    is ever violated, rather than raising."""
    rows = conn.execute(
        "SELECT alias_id, linked_at FROM user_alias WHERE primary_user_id = ? "
        "ORDER BY linked_at", (primary_user_id,)
    ).fetchall()
    out = []
    for r in rows:
        u = get_user(conn, r["alias_id"])
        out.append({
            "id": r["alias_id"],
            "displayName": u["displayName"] if u else None,
            "label": u["label"] if u else None,
            "linkedAt": r["linked_at"],
        })
    return out


# ---------- accounts ----------

# S2.1 (DEC-038 §4/§13-Q2, SHOULD-FIX 2026-07-28 review): types whose liability-ness is
# NOT ambiguous. 'other' is deliberately excluded -- an unclassified account is the one
# genuinely ambiguous case (legitimately either an asset or a liability), so it always
# respects whatever the caller/stored value already is.
_UNAMBIGUOUS_ASSET_TYPES = frozenset({"checking", "savings", "brokerage", "retirement", "hsa", "cash"})

_UNSET = object()   # sentinel: distinguishes "caller omitted is_liability" from "caller passed False"


def _normalize_liability(type: str, is_liability_supplied: bool) -> bool | None:
    """The FORCED is_liability value for `type`, or ``None`` when nothing should be
    forced (use the caller-supplied/stored value as-is). Shared by create_account and
    update_account so a type change is never one-directional -- the original guard only
    forced credit/loan -> True, which left `is_liability=1` STRANDED on a credit->checking
    retype that didn't also touch isLiability (a checking account with nwSign=-1: the
    blast radius includes the EXISTING _net_worth_at/account_liability_map, not just the
    new tracking.account_balances -- live net-worth corruption, the R5 hazard).

    - credit/loan: ALWAYS force True, regardless of whether the caller supplied a value
      (a type='credit' account persisted with is_liability=0 would invert its sign
      everywhere that reads it).
    - An unambiguous asset type (checking/savings/brokerage/retirement/hsa/cash) forces
      False, but ONLY when the caller did not explicitly supply is_liability -- an
      explicit request to flag e.g. a checking account as a liability is still honored.
    - 'other': never forced either way -- legitimately either, always respects the
      caller-supplied/stored value.
    """
    if type in ("credit", "loan"):
        return True
    if type in _UNAMBIGUOUS_ASSET_TYPES and not is_liability_supplied:
        return False
    return None


def create_account(conn, user_id, name, type="other", is_liability=_UNSET, currency="USD", invest_group=None,
                   credit_limit_cents=None) -> dict:
    if type not in _ACCOUNT_TYPES:
        raise ValueError(f"invalid account type: {type!r}")
    invest_group = (invest_group or "").strip() or None
    supplied = is_liability is not _UNSET
    forced = _normalize_liability(type, supplied)
    is_liability = forced if forced is not None else bool(False if is_liability is _UNSET else is_liability)
    cur = conn.execute(
        "INSERT INTO account (user_id, name, type, is_liability, currency, created_at, invest_group, credit_limit_cents) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (user_id, name, type, int(bool(is_liability)), currency, _now(), invest_group,
         (int(credit_limit_cents) if credit_limit_cents is not None else None)),
    )
    conn.commit()
    return get_account(conn, user_id, cur.lastrowid)


def get_account(conn, user_id, account_id) -> dict | None:
    row = conn.execute(
        "SELECT * FROM account WHERE id = ? AND user_id = ?", (account_id, user_id)).fetchone()
    return _account_dict(row) if row else None


def list_accounts(conn, user_id, include_archived=False) -> list[dict]:
    sql = "SELECT * FROM account WHERE user_id = ?"
    vals = [user_id]
    if not include_archived:
        sql += " AND archived = 0"
    sql += " ORDER BY name"
    return [_account_dict(r) for r in conn.execute(sql, vals).fetchall()]


def update_account(conn, user_id, account_id, **fields) -> dict | None:
    allowed = {"name", "type", "is_liability", "archived", "currency", "invest_group", "credit_limit_cents"}
    if "type" in fields and fields["type"] not in _ACCOUNT_TYPES:
        raise ValueError(f"invalid account type: {fields['type']!r}")
    # S2.1 (DEC-038 §4/§13-Q2, SHOULD-FIX 2026-07-28 review): the SAME symmetric
    # _normalize_liability used by create_account, applied at update time too -- e.g.
    # switching an existing account's `type` to 'credit'/'loan' forces True, but
    # switching AWAY from credit/loan to an unambiguous asset type (checking/savings/
    # brokerage/retirement/hsa/cash) with no explicit isLiability in this same PATCH now
    # ALSO clears is_liability back to False -- a retype is no longer one-directional.
    # Effective type = the incoming `type` if this call is changing it, else the
    # account's current type (one extra SELECT, only when needed to resolve it).
    effective_type = fields.get("type")
    if effective_type is None and "is_liability" in fields:
        row = conn.execute(
            "SELECT type FROM account WHERE id = ? AND user_id = ?", (account_id, user_id)).fetchone()
        effective_type = row["type"] if row else None
    if effective_type is not None:
        forced = _normalize_liability(effective_type, "is_liability" in fields)
        if forced is not None:
            fields["is_liability"] = forced
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "invest_group":
            v = (v or "").strip() or None   # empty string clears the group
        if k in ("is_liability", "archived"):
            v = int(bool(v))
        if k == "credit_limit_cents" and v is not None:
            v = int(v)
        sets.append(f"{k} = ?")
        vals.append(v)
    if sets:
        vals.append(account_id)
        vals.append(user_id)
        conn.execute(f"UPDATE account SET {', '.join(sets)} WHERE id = ? AND user_id = ?", vals)
        conn.commit()
    return get_account(conn, user_id, account_id)


def delete_account(conn, user_id, account_id) -> None:
    # An account-linked venture would be orphaned to NO linkage (ON DELETE SET NULL breaks
    # the exactly-one invariant, review finding 2) — make the user relink or delete it first.
    row = conn.execute(
        "SELECT name FROM venture WHERE account_id = ? AND user_id = ?", (account_id, user_id)).fetchone()
    if row is not None:
        raise ValueError(
            f"account is linked to venture {row['name']!r} — switch that venture to a tag "
            "or delete it first")
    conn.execute("DELETE FROM account WHERE id = ? AND user_id = ?", (account_id, user_id))
    conn.commit()


def account_liability_map(conn, user_id) -> dict[int, bool]:
    return {r["id"]: bool(r["is_liability"]) for r in conn.execute(
        "SELECT id, is_liability FROM account WHERE user_id = ?", (user_id,))}


def _account_dict(r) -> dict:
    limit_c = r["credit_limit_cents"]
    return {
        "id": r["id"], "name": r["name"], "type": r["type"],
        "isLiability": bool(r["is_liability"]), "currency": r["currency"],
        "archived": bool(r["archived"]), "createdAt": r["created_at"],
        "investGroup": r["invest_group"],
        "creditLimit": (round(limit_c / 100.0, 2) if limit_c is not None else None),
    }


# ---------- transactions ----------

# ----- tags (orthogonal to the bucket rollup; aggregate_actuals never reads them) -----

def _set_txn_tags(conn, user_id, txn_id, names) -> None:
    """Replace a transaction's tags with `names` (upserting tags case-insensitively,
    scoped to the caller's own tags -- txn_tag inherits scope from its parent txn/tag,
    but the tag lookup/creation itself must stay within the caller's user_id)."""
    conn.execute("DELETE FROM txn_tag WHERE txn_id = ?", (txn_id,))
    for raw in names or []:
        name = str(raw).strip()
        if not name:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO tag (user_id, name, created_at) VALUES (?, ?, ?)",
            (user_id, name, _now()))
        tid = conn.execute(
            "SELECT id FROM tag WHERE user_id = ? AND name = ? COLLATE NOCASE",
            (user_id, name)).fetchone()["id"]
        conn.execute("INSERT OR IGNORE INTO txn_tag (txn_id, tag_id) VALUES (?, ?)", (txn_id, tid))


def _attach_tags(conn, d: dict) -> dict:
    rows = conn.execute(
        "SELECT t.name FROM txn_tag jt JOIN tag t ON t.id = jt.tag_id WHERE jt.txn_id = ? ORDER BY t.name",
        (d["id"],)).fetchall()
    d["tags"] = [r["name"] for r in rows]
    return d


def list_tags(conn, user_id) -> list[dict]:
    return [{"id": r["id"], "name": r["name"], "count": r["n"]} for r in conn.execute(
        "SELECT t.id, t.name, COUNT(jt.txn_id) AS n FROM tag t "
        "LEFT JOIN txn_tag jt ON jt.tag_id = t.id WHERE t.user_id = ? "
        "GROUP BY t.id ORDER BY n DESC, t.name", (user_id,)).fetchall()]


def _validate_splits(direction, is_transfer, amount_cents, splits) -> list:
    """Splits are only valid on non-transfer 'out' rows and must sum to the parent total.
    Returns the normalized leg tuples (bucket, category, amount_cents)."""
    if direction != "out":
        raise ValueError("only 'out' transactions can be split")
    if is_transfer:
        raise ValueError("transfers cannot be split")
    legs = []
    for sp in splits:
        b = sp.get("bucket")
        if b is not None and not str(b).strip():
            raise ValueError(f"split bucket must not be empty: {b!r}")
        ac = int(sp.get("amount_cents", 0))
        if ac < 0:
            raise ValueError("split amount must be >= 0")
        legs.append((b, sp.get("category"), ac))
    if sum(l[2] for l in legs) != int(amount_cents):
        raise ValueError("split amounts must sum to the transaction total")
    return legs


def _attach_splits(conn, d: dict) -> dict:
    rows = conn.execute(
        "SELECT bucket, category, amount_cents FROM txn_split WHERE txn_id = ? ORDER BY id", (d["id"],)).fetchall()
    d["splits"] = [{"bucket": r["bucket"], "category": r["category"], "amount": round(r["amount_cents"] / 100.0, 2)} for r in rows]
    return d


def create_txn(conn, user_id, account_id, posted_on, direction, amount_cents, *, bucket=None,
               category=None, description=None, is_transfer=False, transfer_group=None,
               source="manual", external_id=None, tags=None, splits=None,
               partner_owed_cents=0, status='settled', kind='charge') -> dict:
    if direction not in ("in", "out"):
        raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
    if amount_cents < 0:
        raise ValueError("amount_cents must be >= 0 (direction carries the sign)")
    if not (0 <= int(partner_owed_cents or 0) <= int(amount_cents)):
        raise ValueError("partner_owed_cents must be between 0 and the amount")
    if bucket is not None and not str(bucket).strip():
        raise ValueError(f"bucket must not be empty")
    if status not in ("settled", "pending"):
        raise ValueError(f"status must be 'settled' or 'pending', got {status!r}")
    if kind not in ("charge", "refund"):
        raise ValueError(f"kind must be 'charge' or 'refund', got {kind!r}")
    if splits and kind == "refund":
        raise ValueError("refunds cannot be split")
    legs = _validate_splits(direction, is_transfer, amount_cents, splits) if splits else []
    cur = conn.execute(
        """INSERT INTO txn (user_id, account_id, posted_on, direction, amount_cents, bucket, category,
               description, is_transfer, transfer_group, source, external_id, partner_owed_cents,
               status, kind, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, account_id, posted_on, direction, int(amount_cents), bucket, category, description,
         int(bool(is_transfer)), transfer_group, source, external_id, int(partner_owed_cents or 0),
         status, kind, _now()),
    )
    txn_id = cur.lastrowid
    if tags:
        _set_txn_tags(conn, user_id, txn_id, tags)
    for (b, cat, ac) in legs:
        conn.execute("INSERT INTO txn_split (txn_id, bucket, category, amount_cents) VALUES (?,?,?,?)", (txn_id, b, cat, ac))
    conn.commit()
    return _attach_splits(conn, _attach_tags(conn, _txn_dict(conn.execute("SELECT * FROM txn WHERE id = ?", (txn_id,)).fetchone())))


def record_card_payment(conn, user_id, card_account_id, amount_cents, posted_on, transfer_group, *,
                        from_account_id=None, description=None, bucket=None) -> list[int]:
    """Insert a credit-card payment transfer (one or two legs).

    Leg 1 (always): direction='in' on the card account — the payment credits the card balance.
        The optional ``bucket`` earmarks this leg to a spending category (e.g. "groceries").
        Any non-empty string is accepted; None means no earmark.
    Leg 2 (optional): direction='out' on the funding account — the cash leaves checking/savings.
        bucket is always None on Leg 2; the funding leg carries no category.

    Both legs share the same transfer_group so they can be matched as a pair.
    A single conn.commit() covers both inserts atomically. Both legs are stamped with the
    caller's own `user_id` — the caller (server.py) must have already verified ownership of
    both card_account_id and from_account_id via `_require_own_account`.

    Returns
    -------
    list[int]
        [card_in_id] when from_account_id is None, else [card_in_id, funding_out_id].
        Card leg is always first.
    """
    if not isinstance(amount_cents, int) or amount_cents <= 0:
        raise ValueError(f"amount_cents must be a positive int, got {amount_cents!r}")
    if not transfer_group or not str(transfer_group).strip():
        raise ValueError("transfer_group must be a non-empty string")
    if bucket is not None and not str(bucket).strip():
        raise ValueError("bucket must not be empty")
    cur = conn.execute(
        """INSERT INTO txn (user_id, account_id, posted_on, direction, amount_cents, bucket, category,
               description, is_transfer, transfer_group, source, external_id, partner_owed_cents,
               status, kind, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (user_id, card_account_id, posted_on, "in", int(amount_cents), bucket, None, description,
         1, transfer_group, "manual", None, 0, "settled", "charge", _now()),
    )
    card_in_id = cur.lastrowid
    ids: list[int] = [card_in_id]
    if from_account_id is not None:
        cur2 = conn.execute(
            """INSERT INTO txn (user_id, account_id, posted_on, direction, amount_cents, bucket, category,
                   description, is_transfer, transfer_group, source, external_id, partner_owed_cents,
                   status, kind, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, from_account_id, posted_on, "out", int(amount_cents), None, None, description,
             1, transfer_group, "manual", None, 0, "settled", "charge", _now()),
        )
        ids.append(cur2.lastrowid)
    conn.commit()
    return ids


def list_txns(conn, user_id, *, month=None, account_id=None, bucket=None, direction=None, tag=None,
              date_to=None, account_ids=None, status=None, date_before=None) -> list[dict]:
    """Return transactions matching the given filters, scoped to `user_id`.

    New optional params (backward-compatible; existing callers unaffected):
      date_to     — ISO date string; keeps rows where posted_on <= date_to.
      account_ids — iterable of account ids; keeps rows where account_id IN (...).
                    An empty iterable yields no rows (safe: uses ``1=0`` instead of
                    ``IN ()`` which SQLite rejects).
      status      — string; keeps rows where status = status (e.g. 'pending', 'settled').
      date_before — ISO date string; EXCLUSIVE upper bound: keeps rows where
                    posted_on < date_before.  Distinct from date_to (which is inclusive).
                    Pass f"{month}-01" to mean "strictly before the current month" via
                    lexical ISO-date comparison — no date arithmetic required.
    """
    sql = "SELECT t.* FROM txn t"
    if tag:
        sql += " JOIN txn_tag jt ON jt.txn_id = t.id JOIN tag tg ON tg.id = jt.tag_id"
    where, vals = ["t.user_id = ?"], [user_id]
    if tag:
        where.append("tg.name = ? COLLATE NOCASE"); vals.append(tag)
    if month:
        where.append("t.posted_on LIKE ?"); vals.append(f"{month}-%")
    if account_id is not None:
        where.append("t.account_id = ?"); vals.append(account_id)
    if bucket is not None:
        where.append("t.bucket = ?"); vals.append(bucket)
    if direction is not None:
        where.append("t.direction = ?"); vals.append(direction)
    if date_to is not None:
        where.append("t.posted_on <= ?"); vals.append(date_to)
    if account_ids is not None:
        _ids_list = list(account_ids)
        if not _ids_list:
            where.append("1=0")          # empty iterable → no rows (IN () is invalid SQL)
        else:
            acct_ph = ",".join("?" * len(_ids_list))
            where.append(f"t.account_id IN ({acct_ph})")
            vals.extend(_ids_list)
    if status is not None:
        where.append("t.status = ?"); vals.append(status)
    if date_before is not None:
        where.append("t.posted_on < ?"); vals.append(date_before)
    sql += " WHERE " + " AND ".join(where) + " ORDER BY t.posted_on DESC, t.id DESC"
    dicts = [_txn_dict(r) for r in conn.execute(sql, vals).fetchall()]
    ids = [d["id"] for d in dicts]
    if ids:                                                 # batch-attach tags + splits (no N+1)
        ph = ",".join("?" * len(ids))
        tagmap: dict = {}
        for r in conn.execute(
            "SELECT jt.txn_id, t.name FROM txn_tag jt JOIN tag t ON t.id = jt.tag_id "
            f"WHERE jt.txn_id IN ({ph}) ORDER BY t.name", ids).fetchall():
            tagmap.setdefault(r["txn_id"], []).append(r["name"])
        splitmap: dict = {}
        for r in conn.execute(
            f"SELECT txn_id, bucket, category, amount_cents FROM txn_split WHERE txn_id IN ({ph}) ORDER BY id",
            ids).fetchall():
            splitmap.setdefault(r["txn_id"], []).append(
                {"bucket": r["bucket"], "category": r["category"], "amount": round(r["amount_cents"] / 100.0, 2)})
        for d in dicts:
            d["tags"] = tagmap.get(d["id"], [])
            d["splits"] = splitmap.get(d["id"], [])
    return dicts


def update_txn(conn, user_id, txn_id, **fields) -> dict | None:
    """Patch a transaction in place (edit a mistyped amount, re-bucket, fix a category).
    Only known columns are updated; bucket/direction are validated. Scoped to the
    caller's own row: the UPDATE/SELECT both filter `AND user_id = ?`, so an id
    belonging to another user is indistinguishable from a nonexistent one (returns
    None -> the endpoint layer maps this to 404, never leaking existence)."""
    tags = fields.pop("tags", None)                          # tags aren't a txn column — set separately
    allowed = {"posted_on", "direction", "amount_cents", "bucket", "category",
               "description", "is_transfer", "transfer_group", "partner_owed_cents",
               "status", "kind", "account_id"}
    if "direction" in fields and fields["direction"] not in ("in", "out"):
        raise ValueError(f"direction must be 'in' or 'out', got {fields['direction']!r}")
    if "account_id" in fields:                               # validate here for a clean 422 (FK would 500)
        _require_own_account(conn, user_id, fields["account_id"])
    if fields.get("bucket") is not None and not str(fields["bucket"]).strip():
        raise ValueError(f"bucket must not be empty")
    if "amount_cents" in fields and fields["amount_cents"] is not None and fields["amount_cents"] < 0:
        raise ValueError("amount_cents must be >= 0")
    if fields.get("partner_owed_cents") is not None and fields["partner_owed_cents"] < 0:
        raise ValueError("partner_owed_cents must be >= 0")
    if "status" in fields and fields["status"] not in ("settled", "pending"):
        raise ValueError(f"status must be 'settled' or 'pending', got {fields['status']!r}")
    if "kind" in fields and fields["kind"] not in ("charge", "refund"):
        raise ValueError(f"kind must be 'charge' or 'refund', got {fields['kind']!r}")
    # create_txn's "refunds cannot be split" guard (line ~1787) only fires at INSERT time --
    # re-review of the BUG-0005 fix round found this reachable via update: create a split-as-
    # charge txn, then PATCH kind='refund' onto it, and this function accepted it (no splits
    # column is writable here, so the only way to reach the invalid state is flipping kind on a
    # row that ALREADY has splits). Numerically harmless either way -- the month_actuals flatten
    # query and the client's acSignedLegAmount both still sign a split leg correctly regardless of
    # the parent's kind -- but the client's own acSignedLegAmount warns "server rejects this"
    # about a state a user could actually reach, which would send a future debugger hunting for
    # DB corruption that isn't there. Scoped to this user's own row (joins txn.user_id) so the
    # existence check can't leak whether a foreign txn_id has splits.
    if fields.get("kind") == "refund":
        has_splits = conn.execute(
            "SELECT 1 FROM txn_split s JOIN txn t ON t.id = s.txn_id "
            "WHERE s.txn_id = ? AND t.user_id = ? LIMIT 1",
            (txn_id, user_id),
        ).fetchone()
        if has_splits:
            raise ValueError("refunds cannot be split")
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "is_transfer":
            v = int(bool(v))
        sets.append(f"{k} = ?")
        vals.append(v)
    if sets:
        vals.append(txn_id)
        vals.append(user_id)
        conn.execute(f"UPDATE txn SET {', '.join(sets)} WHERE id = ? AND user_id = ?", vals)
        conn.commit()
    row = conn.execute("SELECT * FROM txn WHERE id = ? AND user_id = ?", (txn_id, user_id)).fetchone()
    if row is None:
        return None
    if tags is not None:
        _set_txn_tags(conn, user_id, txn_id, tags)           # replace; only after we know the row exists
        conn.commit()
    return _attach_tags(conn, _txn_dict(row))


def delete_txn(conn, user_id, txn_id) -> list[int]:
    """Delete a transaction and — when it belongs to a transfer_group — every leg in that
    group, scoped to the caller's own rows (`AND user_id = ?` on both SELECTs and the
    DELETE) so a foreign id is a no-op, not a leak.

    This fixes the orphan-leg bug: deleting one side of a paired card payment now atomically
    removes both legs in a single DELETE statement and a single commit.

    Returns
    -------
    list[int]
        Ids of every row removed, in the order returned by the SELECT.  Empty when the id
        does not exist (or belongs to another user — idempotent no-op).  The caller
        (delete_txn_endpoint) surfaces this as `deletedIds` / `rows` in the response so the
        client can reconcile both legs.

    Notes
    -----
    * txn_tag and txn_split cascade via ON DELETE CASCADE — no extra DELETE needed.
    * SELECT-then-DELETE (not DELETE…RETURNING) for portability to minimal SQLite images.
    """
    row = conn.execute(
        "SELECT transfer_group FROM txn WHERE id = ? AND user_id = ?", (txn_id, user_id)).fetchone()
    if row is None:
        return []
    tg = row["transfer_group"]
    if tg is None:
        conn.execute("DELETE FROM txn WHERE id = ? AND user_id = ?", (txn_id, user_id))
        conn.commit()
        return [txn_id]
    ids_rows = conn.execute(
        "SELECT id FROM txn WHERE transfer_group = ? AND user_id = ?", (tg, user_id)).fetchall()
    ids = [r["id"] for r in ids_rows]
    ph = ",".join("?" * len(ids))
    conn.execute(f"DELETE FROM txn WHERE id IN ({ph}) AND user_id = ?", ids + [user_id])
    conn.commit()
    return ids


def update_card_payment(conn, user_id, in_leg_id, *, amount_cents, bucket) -> dict | None:
    """Edit the amount and/or earmark on a card-payment IN-leg (DEC-014).

    This is a FULL-REPLACE contract:
    * ``bucket=None`` clears an existing earmark (means "whole card").
    * ``bucket`` is written only on the IN-leg; the OUT-leg's bucket is never touched.
    * ``amount_cents`` is applied to the whole transfer_group so both legs stay balanced.

    Parameters
    ----------
    in_leg_id : int
        The ``id`` of the card-payment transfer-IN row (``direction='in'``, ``is_transfer=1``).
    amount_cents : int
        New payment amount; must be a positive ``int``.
    bucket : str | None
        New earmark bucket string, or ``None`` to clear.  An empty/whitespace string is
        rejected with ``ValueError`` (bucket must be meaningful or absent).

    Returns
    -------
    dict | None
        The updated IN-leg as a ``_txn_dict`` dict, or ``None`` when ``in_leg_id`` does
        not exist, or belongs to another user (scoped lookup — never leaks existence).

    Raises
    ------
    ValueError
        * Row is not a transfer-IN (guard — caller passed the wrong leg).
        * ``amount_cents`` is not a positive ``int``.
        * ``bucket`` is a non-None empty/whitespace string.
    """
    row = conn.execute(
        "SELECT * FROM txn WHERE id = ? AND user_id = ?", (in_leg_id, user_id)).fetchone()
    if row is None:
        return None
    if not row["is_transfer"] or row["direction"] != "in":
        raise ValueError("not a card-payment IN-leg")
    if not isinstance(amount_cents, int) or isinstance(amount_cents, bool) or amount_cents <= 0:
        raise ValueError(f"amount_cents must be a positive int, got {amount_cents!r}")
    if bucket is not None and not str(bucket).strip():
        raise ValueError("bucket must not be empty or whitespace")
    # Earmark on IN-leg only
    conn.execute("UPDATE txn SET bucket = ? WHERE id = ? AND user_id = ?", (bucket, in_leg_id, user_id))
    # Amount on both legs (via transfer_group when present; otherwise just this row)
    tg = row["transfer_group"]
    if tg is not None:
        conn.execute(
            "UPDATE txn SET amount_cents = ? WHERE transfer_group = ? AND user_id = ?",
            (amount_cents, tg, user_id))
    else:
        conn.execute(
            "UPDATE txn SET amount_cents = ? WHERE id = ? AND user_id = ?",
            (amount_cents, in_leg_id, user_id))
    conn.commit()
    updated = conn.execute(
        "SELECT * FROM txn WHERE id = ? AND user_id = ?", (in_leg_id, user_id)).fetchone()
    return _txn_dict(updated)


def _txn_dict(r) -> dict:
    return {
        "id": r["id"], "accountId": r["account_id"], "postedOn": r["posted_on"],
        "direction": r["direction"], "amount": round(r["amount_cents"] / 100.0, 2),
        "bucket": r["bucket"], "category": r["category"], "description": r["description"],
        "isTransfer": bool(r["is_transfer"]), "transferGroup": r["transfer_group"],
        "partnerOwed": round((r["partner_owed_cents"] or 0) / 100.0, 2),
        "source": r["source"], "externalId": r["external_id"], "createdAt": r["created_at"],
        "status": r["status"], "kind": r["kind"],
    }


# ---------- balance snapshots (upsert per account+date) ----------

def upsert_snapshot(conn, user_id, account_id, as_of, balance_cents, source="manual") -> dict:
    # Caller (server.py) must already have verified account_id belongs to user_id via
    # _require_own_account. The UNIQUE(account_id, as_of) conflict target is unchanged —
    # account_id alone already implies a single user, so it needs no user_id in the key.
    conn.execute(
        """INSERT INTO balance_snapshot (user_id, account_id, as_of, balance_cents, source, created_at)
           VALUES (?,?,?,?,?,?)
           ON CONFLICT(account_id, as_of)
           DO UPDATE SET balance_cents = excluded.balance_cents, source = excluded.source""",
        (user_id, account_id, as_of, int(balance_cents), source, _now()),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM balance_snapshot WHERE account_id = ? AND as_of = ? AND user_id = ?",
        (account_id, as_of, user_id)
    ).fetchone()
    return _snapshot_dict(row)


def list_snapshots(conn, user_id, *, account_id=None, date_from=None, date_to=None) -> list[dict]:
    sql, vals = "SELECT * FROM balance_snapshot WHERE user_id = ?", [user_id]
    if account_id is not None:
        sql += " AND account_id = ?"
        vals.append(account_id)
    if date_from:
        sql += " AND as_of >= ?"
        vals.append(date_from)
    if date_to:
        sql += " AND as_of <= ?"
        vals.append(date_to)
    sql += " ORDER BY as_of, account_id"
    return [_snapshot_dict(r) for r in conn.execute(sql, vals).fetchall()]


def delete_snapshot(conn, user_id, snapshot_id) -> None:
    conn.execute("DELETE FROM balance_snapshot WHERE id = ? AND user_id = ?", (snapshot_id, user_id))
    conn.commit()


def _snapshot_dict(r) -> dict:
    return {
        "id": r["id"], "accountId": r["account_id"], "asOf": r["as_of"],
        "balance": round(r["balance_cents"] / 100.0, 2), "source": r["source"],
        "createdAt": r["created_at"],
    }


# ---------- plan snapshots ----------

def _save_plan_row(conn, user_id, month, payload: dict, status="locked", engine_version="1.0",
                   locked_at: str | None = None) -> None:
    """The save_plan upsert WITHOUT the commit, so multi-month writers (scenario
    activate/revert, DEC-017) can batch it inside one transaction. `locked_at`
    override lets revert restore the original lock timestamp faithfully."""
    if locked_at is None:
        locked_at = _now() if status == "locked" else None
    conn.execute(
        """INSERT INTO plan_snapshot (user_id, month, status, engine_version, payload_json, created_at, locked_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(user_id, month) DO UPDATE SET
               status = excluded.status, engine_version = excluded.engine_version,
               payload_json = excluded.payload_json, locked_at = excluded.locked_at""",
        (user_id, month, status, engine_version, json.dumps(payload), _now(), locked_at),
    )


def save_plan(conn, user_id, month, payload: dict, status="locked", engine_version="1.0") -> dict:
    """Upsert the month's plan baseline. status='draft' (mutable, open month) or
    'locked' (immutable history). Re-saving a locked month replaces its payload."""
    _save_plan_row(conn, user_id, month, payload, status, engine_version)
    conn.commit()
    return get_plan(conn, user_id, month)


def get_plan(conn, user_id, month) -> dict | None:
    row = conn.execute(
        "SELECT * FROM plan_snapshot WHERE user_id = ? AND month = ?", (user_id, month)).fetchone()
    if not row:
        return None
    return {
        "month": row["month"], "status": row["status"], "engineVersion": row["engine_version"],
        "payload": json.loads(row["payload_json"]), "createdAt": row["created_at"],
        "lockedAt": row["locked_at"],
    }


def delete_plan(conn, user_id, month) -> int:
    """Remove a month's plan baseline. Used by scenario revert to undo a plan row that
    activation created where none existed (DEC-017 #6). Returns rows deleted (0 or 1)."""
    cur = conn.execute("DELETE FROM plan_snapshot WHERE user_id = ? AND month = ?", (user_id, month))
    conn.commit()
    return cur.rowcount


# ---------- the aggregate the dashboard endpoint consumes ----------

def _fund_draw_bucket_cents(conn, user_id, fund_id, month: str) -> dict:
    """{bucket_or_None: cents} for the RAW amount of this fund's `role='draw'` txns
    actually posted in `month` — grouped by the DRAW TXN'S OWN `bucket` column, not the
    fund's configured bucket. Summed the same way `fund_monthly_flows` sums `drawCents`
    (plain `SUM(t.amount_cents)`, no refund sign-flip), so these per-bucket weights
    always sum to EXACTLY that same month's `drawCents` total — the invariant the
    proportional split below relies on. `ORDER BY t.bucket` makes iteration order (and
    therefore any largest-remainder tie-break) deterministic."""
    rows = conn.execute(
        "SELECT t.bucket AS bucket, SUM(t.amount_cents) AS c "
        "FROM fund_txn ft JOIN txn t ON t.id = ft.txn_id "
        "WHERE ft.fund_id = ? AND ft.role = 'draw' AND t.user_id = ? "
        "AND substr(t.posted_on, 1, 7) = ? GROUP BY t.bucket ORDER BY t.bucket",
        (fund_id, user_id, month)).fetchall()
    return {r["bucket"]: r["c"] for r in rows}


def _allocate_cents_by_weight(total_c: int, weights: dict) -> dict:
    """Split `total_c` integer cents across `weights` (key -> a non-negative raw cents
    weight) proportionally, CENT-EXACT via the largest-remainder method: each key's base
    share is `floor(total_c * weight / sum(weights))`, then the `total_c - Σ(base)`
    leftover cents (always < len(weights)) go one each to the keys with the largest
    fractional remainder. Two structural guarantees this relies on elsewhere (proved by
    the caller's own invariant `total_c <= sum(weights)`):
      1. Output sums to EXACTLY `total_c` (never a stray rounding cent lost or invented).
      2. Every key's allocated share is `<= weights[key]` — a key can never be allocated
         MORE than its own raw weight, because `total_c/sum(weights) <= 1` scales every
         share down (or leaves it equal only when `total_c == sum(weights)`, the
         fully-funded case, where remainders are all zero and no leftover cents exist to
         push any share past its own weight).
    (Same largest-remainder apportionment family as the Next-Dollar election-aware
    waterfall's trad/Roth split in investing.py -- that one streams a cumulative delta
    across steps, this one splits a single total across buckets in one pass; both exist
    to guarantee a whole-cents split reconciles EXACTLY to its total.)"""
    weight_sum = sum(weights.values())
    if weight_sum <= 0 or total_c <= 0:
        return {k: 0 for k in weights}
    shares: dict = {}
    remainders: list = []
    allocated = 0
    for k, w in weights.items():
        exact = total_c * w / weight_sum
        base = int(exact)                     # floor (weights/total_c are both >= 0)
        shares[k] = base
        allocated += base
        remainders.append((exact - base, k))
    leftover = total_c - allocated
    remainders.sort(key=lambda pair: pair[0], reverse=True)   # largest fractional remainder first
    for i in range(leftover):
        shares[remainders[i][1]] += 1
    return shares


def month_actuals(conn, user_id, month: str) -> dict:
    """Fetch the month's transactions + ALL snapshots (scoped to `user_id`) and hand
    them to the pure aggregator. (Snapshots span history because the net-worth overlay
    is a trajectory.) All user filtering happens HERE in the store query layer —
    `tracking.aggregate_actuals` itself stays byte-unchanged (DEC-009 #1): it still
    receives the same flat row shape and never sees `user_id`."""
    like = f"{month}-%"
    # Flatten splits in the STORE so the pure aggregator never changes (DEC-009): a txn WITH
    # splits is excluded from the first SELECT; its children (own bucket/amount, parent's
    # date/direction/transfer-flag) come from the second. Σ children == parent total.
    txn_rows = [dict(r) for r in conn.execute(
        """SELECT t.account_id, t.posted_on, t.direction,
                  CASE WHEN t.kind='refund' THEN -t.amount_cents ELSE t.amount_cents END AS amount_cents,
                  t.bucket, t.is_transfer
             FROM txn t WHERE t.posted_on LIKE ? AND t.user_id = ?
               AND NOT EXISTS (SELECT 1 FROM txn_split s WHERE s.txn_id = t.id)
           UNION ALL
           SELECT t.account_id, t.posted_on, t.direction,
                  CASE WHEN t.kind='refund' THEN -s.amount_cents ELSE s.amount_cents END AS amount_cents,
                  s.bucket, t.is_transfer
             FROM txn_split s JOIN txn t ON t.id = s.txn_id WHERE t.posted_on LIKE ? AND t.user_id = ?""",
        (like, user_id, like, user_id)).fetchall()]

    # ---- Sinking funds Phase 2 fold (TODO-238, DEC-034 §5, docs/sinking-funds-design.md
    # §amendment-following "Phase 2" section) ----
    # A contribution is ALREADY counted as spend in its fund's bucket the moment it's
    # logged (it's an ordinary out-txn like any other). Drawing the reserve back down to
    # pay the big expense would double-count that same money as spend a second time unless
    # excused. The excusal is expressed EXACTLY like DEC-011 #2's refund sign-flip: one or
    # more synthetic negative out-lines, sized in total to that fund's `fundedDraw` for
    # THIS month only (never the whole reserve). `tracking.fund_rollup`'s per-month floor
    # already guarantees an `unfundedDraw` is never retroactively excused by a later
    # contribution (§5.1 invariant #4), so folding month-by-month here is exactly correct
    # — no separate clamping needed at this layer.
    #
    # This is the ONLY place the fold happens: `tracking.aggregate_actuals` /
    # `tracking.plan_vs_actual` never see a fund and stay byte-unchanged (DEC-009 #1). A
    # user with zero funds gets a `txn_rows` list identical to pre-Phase-2 output — the
    # loop below simply never appends anything — so the headline is byte-identical to
    # today's numbers by construction (§5.1 invariant #3), not by a special case.
    #
    # SHOULD-FIX from code review (2026-07-28): the offset now targets the bucket(s) of
    # the fund's ACTUAL `role='draw'` txns THIS month (`_fund_draw_bucket_cents`), split
    # proportionally by `_allocate_cents_by_weight` when a month's draws span more than
    # one bucket — NOT `fund["bucket"]` (the fund's merely-configured "home" bucket, kept
    # below only as descriptive metadata on `byFund`). The prior draft targeted the
    # configured bucket unconditionally, which could drive an unrelated bucket negative
    # (a fund configured for "need" but drawn against "travel" would wrongly credit
    # "need"), or even the SAME bucket negative when refunds there had nothing to do with
    # the fund's own draws. Attributing to the draw's own bucket makes the offset
    # STRUCTURALLY bounded: `_allocate_cents_by_weight` never assigns a bucket more than
    # its own share of the fund's draws THIS month (proof in that function's docstring),
    # and `sum(that bucket's linked draws) <= that bucket's raw spend` (a draw txn is
    # itself one of the addends aggregate_actuals sums into the bucket) — so the offset
    # can never exceed, and thus never drive negative, the portion of that bucket's total
    # actually contributed by THIS fund's own draws. It does NOT protect against a large
    # UNRELATED refund in that same bucket outweighing everything else there (a pre-
    # existing, fund-independent DEC-011 property: any bucket can go negative from
    # refunds exceeding its charges) — that is a different, already-accepted behavior,
    # not a new failure mode introduced by funds.
    #
    # NIT 5 (cheap guard, verified rather than special-cased): a fund with `bucket=None`
    # cannot spuriously post an offset into "uncategorized" anymore — the offset's bucket
    # now comes from the DRAW TXN's own `bucket` column via `_fund_draw_bucket_cents`,
    # never from `fund["bucket"]`. It lands in "uncategorized" (bucket key `None`) if and
    # ONLY if that is genuinely where the draw's own spend was categorized — symmetric
    # with how that same spend was counted in the first place, so no special case needed.
    #
    # include_archived=True: an archived fund's PAST funded draws must stay excused (§4.3
    # "archive... preserves history and past excusals") — archiving only stops NEW flows,
    # it must never silently re-blow a month that already relied on the fold.
    fund_excusal_c = 0
    fund_contrib_c = 0
    fund_breakdown: list[dict] = []
    for f in list_funds(conn, user_id, include_archived=True):
        flows = fund_monthly_flows(conn, user_id, f["id"], upto_month=month)
        if month not in flows:
            continue                                  # no contribute/draw activity this month
        contribution_c = int(flows[month].get("contributeCents", 0))
        trajectory = tracking.fund_rollup(flows, upto_month=month)["trajectory"]
        # `month` is present in `flows` and `upto_month=month` bounds the fold, so the
        # trajectory for `month` always exists — searched explicitly (not `[-1]`) so this
        # stays correct even if fund_rollup's internal ordering ever changes.
        entry = next((row for row in trajectory if row["month"] == month), None)
        funded_draw_c = round(entry["fundedDraw"] * 100) if entry else 0
        if contribution_c <= 0 and funded_draw_c <= 0:
            continue
        if funded_draw_c > 0:
            draw_by_bucket_c = _fund_draw_bucket_cents(conn, user_id, f["id"], month)
            for bucket, offset_c in _allocate_cents_by_weight(funded_draw_c, draw_by_bucket_c).items():
                if offset_c <= 0:
                    continue
                txn_rows.append({
                    "account_id": None, "posted_on": f"{month}-01", "direction": "out",
                    "amount_cents": -offset_c, "bucket": bucket, "is_transfer": 0,
                })
            fund_excusal_c += funded_draw_c
        fund_contrib_c += contribution_c
        fund_breakdown.append({
            "fundId": f["id"], "name": f["name"], "bucket": f["bucket"],  # descriptive only — see above
            "contribution": round(contribution_c / 100.0, 2),
            "fundedDraw": round(funded_draw_c / 100.0, 2),
        })

    snap_rows = [dict(r) for r in conn.execute(
        "SELECT account_id, as_of, balance_cents FROM balance_snapshot WHERE user_id = ?",
        (user_id,)).fetchall()]
    result = tracking.aggregate_actuals(txn_rows, snap_rows, account_liability_map(conn, user_id), month)
    # Additive breakdown for the client's "Am I on track?" annotation — rides ALONGSIDE
    # aggregate_actuals' own (untouched) output dict, never inside it.
    result["fundExcusal"] = {
        "total": round(fund_excusal_c / 100.0, 2),
        "contributions": round(fund_contrib_c / 100.0, 2),
        "byFund": fund_breakdown,
    }
    return result


def suggestions(conn, user_id) -> dict:
    """Drives quick-add autocomplete + payee memory. `payees`: for each description seen,
    the most-frequent {bucket, category} the user chose (→ overridable auto-fill).
    `categoriesByBucket`: distinct categories per bucket, most-used first (→ datalist).
    Every GROUP BY is scoped to the caller's own transactions."""
    payees: dict[str, dict] = {}
    for r in conn.execute(
        "SELECT description, bucket, category, COUNT(*) AS n, MAX(posted_on) AS last "
        "FROM txn WHERE user_id = ? AND description IS NOT NULL AND TRIM(description) <> '' "
        "GROUP BY LOWER(description), bucket, category", (user_id,)
    ).fetchall():
        key = r["description"].strip().lower()
        cur = payees.get(key)
        if cur is None or r["n"] > cur["count"]:
            payees[key] = {"description": r["description"], "bucket": r["bucket"],
                           "category": r["category"], "count": r["n"], "last": r["last"]}
    cats: dict[str, list] = {}
    for r in conn.execute(
        "SELECT bucket, category, COUNT(*) AS n FROM txn "
        "WHERE user_id = ? AND category IS NOT NULL AND TRIM(category) <> '' "
        "GROUP BY bucket, category ORDER BY n DESC", (user_id,)
    ).fetchall():
        cats.setdefault(r["bucket"] or "", []).append(r["category"])
    # Tags usually applied to each payee (most-frequent first) → auto-fill the tag chips too.
    payee_tags: dict[str, list] = {}
    for r in conn.execute(
        "SELECT LOWER(t.description) AS dkey, tg.name AS tag, COUNT(*) AS n "
        "FROM txn t JOIN txn_tag jt ON jt.txn_id = t.id JOIN tag tg ON tg.id = jt.tag_id "
        "WHERE t.user_id = ? AND t.description IS NOT NULL AND TRIM(t.description) <> '' "
        "GROUP BY LOWER(t.description), tg.name ORDER BY n DESC, tg.name", (user_id,)
    ).fetchall():
        payee_tags.setdefault(r["dkey"], []).append(r["tag"])
    for p in payees.values():
        p["tags"] = payee_tags.get(p["description"].strip().lower(), [])
    return {"payees": list(payees.values()), "categoriesByBucket": cats}


# ----- recurring templates (pre-fill only; never auto-create) -----

def _template_dict(r) -> dict:
    return {"id": r["id"], "name": r["name"], "direction": r["direction"],
            "amount": round(r["amount_cents"] / 100.0, 2), "bucket": r["bucket"],
            "category": r["category"], "accountId": r["account_id"], "description": r["description"]}


def create_template(conn, user_id, name, *, direction="out", amount_cents=0, bucket=None,
                    category=None, account_id=None, description=None) -> dict:
    if direction not in ("in", "out"):
        raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
    if bucket is not None and not str(bucket).strip():
        raise ValueError(f"bucket must not be empty")
    cur = conn.execute(
        """INSERT INTO template (user_id, name, direction, amount_cents, bucket, category, account_id, description, created_at)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (user_id, name, direction, int(amount_cents), bucket, category, account_id, description, _now()))
    conn.commit()
    return _template_dict(conn.execute("SELECT * FROM template WHERE id = ?", (cur.lastrowid,)).fetchone())


def list_templates(conn, user_id) -> list[dict]:
    return [_template_dict(r) for r in conn.execute(
        "SELECT * FROM template WHERE user_id = ? ORDER BY name", (user_id,)).fetchall()]


def delete_template(conn, user_id, template_id) -> None:
    conn.execute("DELETE FROM template WHERE id = ? AND user_id = ?", (template_id, user_id))
    conn.commit()


# ---------- target-savings goals (TODO-226, DEC-019) ----------

def _valid_goal_date(s) -> str:
    try:
        return datetime.strptime(str(s), "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"target_date must be YYYY-MM-DD, got {s!r}")


def _goal_dict(row) -> dict:
    return {
        "id": row["id"], "name": row["name"],
        "target": row["target_cents"] / 100.0,
        "targetDate": row["target_date"],
        "accountId": row["account_id"],
        "manualSaved": None if row["manual_saved_cents"] is None else row["manual_saved_cents"] / 100.0,
        "status": row["status"], "createdAt": row["created_at"],
    }


def _require_own_account(conn, user_id, account_id) -> None:
    """Cross-entity ownership guard (S1.1): any account_id supplied in a request body
    must belong to the caller's own scope before use. Replaces the old existence-only
    `_require_account` — a foreign account_id must be rejected exactly like a
    nonexistent one (ValueError -> 422), never silently linked."""
    if not conn.execute(
            "SELECT 1 FROM account WHERE id = ? AND user_id = ?", (account_id, user_id)).fetchone():
        raise ValueError(f"account {account_id} does not exist")


def create_goal(conn, user_id, name, target_cents, target_date, account_id=None, manual_saved_cents=None) -> dict:
    if not str(name or "").strip():
        raise ValueError("name must not be empty")
    if not isinstance(target_cents, int) or target_cents <= 0:
        raise ValueError(f"target_cents must be an int > 0, got {target_cents!r}")
    target_date = _valid_goal_date(target_date)
    if account_id is not None:
        _require_own_account(conn, user_id, account_id)
    if manual_saved_cents is not None and (not isinstance(manual_saved_cents, int) or manual_saved_cents < 0):
        raise ValueError(f"manual_saved_cents must be an int >= 0, got {manual_saved_cents!r}")
    cur = conn.execute(
        """INSERT INTO goal (user_id, name, target_cents, target_date, account_id, manual_saved_cents, status, created_at)
           VALUES (?,?,?,?,?,?,'active',?)""",
        (user_id, str(name).strip(), target_cents, target_date, account_id, manual_saved_cents, _now()))
    conn.commit()
    return _goal_dict(conn.execute("SELECT * FROM goal WHERE id = ?", (cur.lastrowid,)).fetchone())


def list_goals(conn, user_id, include_inactive=False) -> list[dict]:
    q = "SELECT * FROM goal WHERE user_id = ?" + ("" if include_inactive else " AND status = 'active'") + " ORDER BY target_date, id"
    return [_goal_dict(r) for r in conn.execute(q, (user_id,)).fetchall()]


def update_goal(conn, user_id, goal_id, **fields) -> dict | None:
    """Patch a goal. account_id=None explicitly unlinks (manual progress takes over)."""
    allowed = {"name", "target_cents", "target_date", "account_id", "manual_saved_cents", "status"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown goal fields: {sorted(unknown)}")
    if "name" in fields and not str(fields["name"] or "").strip():
        raise ValueError("name must not be empty")
    if "target_cents" in fields and (not isinstance(fields["target_cents"], int) or fields["target_cents"] <= 0):
        raise ValueError(f"target_cents must be an int > 0, got {fields['target_cents']!r}")
    if "target_date" in fields:
        fields["target_date"] = _valid_goal_date(fields["target_date"])
    if fields.get("account_id") is not None:
        _require_own_account(conn, user_id, fields["account_id"])
    if fields.get("manual_saved_cents") is not None and (
            not isinstance(fields["manual_saved_cents"], int) or fields["manual_saved_cents"] < 0):
        raise ValueError(f"manual_saved_cents must be an int >= 0, got {fields['manual_saved_cents']!r}")
    if "status" in fields and fields["status"] not in ("active", "done", "cancelled"):
        raise ValueError(f"status must be active/done/cancelled, got {fields['status']!r}")
    sets, vals = [], []
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        vals.append(str(v).strip() if k == "name" else v)
    if sets:
        vals.append(goal_id)
        vals.append(user_id)
        conn.execute(f"UPDATE goal SET {', '.join(sets)} WHERE id = ? AND user_id = ?", vals)
        conn.commit()
    row = conn.execute("SELECT * FROM goal WHERE id = ? AND user_id = ?", (goal_id, user_id)).fetchone()
    return None if row is None else _goal_dict(row)


def delete_goal(conn, user_id, goal_id) -> None:
    conn.execute("DELETE FROM goal WHERE id = ? AND user_id = ?", (goal_id, user_id))
    conn.commit()


# ---------- venture ROI tracker (TODO-228, DEC-020) ----------

def _venture_norm_tag(tag) -> str:
    t = str(tag or "").strip().lstrip("#").strip()
    if not t:
        raise ValueError("tag must not be empty")
    return t


def _venture_items(items) -> str:
    """Validate + serialize investment items -> items_json. Items are TYPED, never
    tagged transactions (DEC-020: the split that makes double-counting structurally
    hard). Cents in storage."""
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list of {label, amountCents}")
    clean = []
    for it in items:
        label = str((it or {}).get("label") or "").strip()
        cents = (it or {}).get("amountCents")
        if not label:
            raise ValueError("every investment item needs a label")
        if not isinstance(cents, int) or cents <= 0:
            raise ValueError(f"investment item {label!r} needs amountCents > 0")
        clean.append({"label": label, "amountCents": cents})
    return json.dumps(clean)


def _venture_dict(row) -> dict:
    try:
        items = json.loads(row["items_json"] or "[]")
    except (ValueError, TypeError):
        items = []    # corrupt hand-edited JSON must not 500 the whole list
    return {
        "id": row["id"], "name": row["name"],
        "tag": row["tag"], "accountId": row["account_id"],
        "items": [{"label": i["label"], "amount": i["amountCents"] / 100.0} for i in items],
        "invested": sum(i["amountCents"] for i in items) / 100.0,
        "startedOn": row["started_on"], "status": row["status"], "createdAt": row["created_at"],
    }


def create_venture(conn, user_id, name, items, started_on, tag=None, account_id=None) -> dict:
    if not str(name or "").strip():
        raise ValueError("name must not be empty")
    started_on = _valid_goal_date(started_on)
    if (tag is None) == (account_id is None):
        raise ValueError("link the venture to exactly one of: a tag OR an account")
    if tag is not None:
        tag = _venture_norm_tag(tag)
    if account_id is not None:
        _require_own_account(conn, user_id, account_id)
    items_json = _venture_items(items)
    cur = conn.execute(
        """INSERT INTO venture (user_id, name, tag, account_id, items_json, started_on, status, created_at)
           VALUES (?,?,?,?,?,?,'active',?)""",
        (user_id, str(name).strip(), tag, account_id, items_json, started_on, _now()))
    conn.commit()
    return _venture_dict(conn.execute("SELECT * FROM venture WHERE id = ?", (cur.lastrowid,)).fetchone())


def list_ventures(conn, user_id, include_stopped=False) -> list[dict]:
    q = "SELECT * FROM venture WHERE user_id = ?" + ("" if include_stopped else " AND status = 'active'") + " ORDER BY started_on, id"
    return [_venture_dict(r) for r in conn.execute(q, (user_id,)).fetchall()]


def update_venture(conn, user_id, venture_id, **fields) -> dict | None:
    """Patch a venture. Setting `tag` clears the account link and vice versa (a venture
    always has exactly one linkage); passing both raises."""
    allowed = {"name", "items", "started_on", "tag", "account_id", "status"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown venture fields: {sorted(unknown)}")
    if "tag" in fields and "account_id" in fields:
        raise ValueError("link the venture to exactly one of: a tag OR an account")
    sets, vals = [], []
    if "name" in fields:
        if not str(fields["name"] or "").strip():
            raise ValueError("name must not be empty")
        sets.append("name = ?"); vals.append(str(fields["name"]).strip())
    if "items" in fields:
        sets.append("items_json = ?"); vals.append(_venture_items(fields["items"]))
    if "started_on" in fields:
        sets.append("started_on = ?"); vals.append(_valid_goal_date(fields["started_on"]))
    if "tag" in fields:
        sets.append("tag = ?"); vals.append(_venture_norm_tag(fields["tag"]))
        sets.append("account_id = ?"); vals.append(None)
    if "account_id" in fields:
        if fields["account_id"] is None:
            raise ValueError("account_id must not be null — switch linkage by setting tag instead")
        _require_own_account(conn, user_id, fields["account_id"])
        sets.append("account_id = ?"); vals.append(fields["account_id"])
        sets.append("tag = ?"); vals.append(None)
    if "status" in fields:
        if fields["status"] not in ("active", "stopped"):
            raise ValueError(f"status must be active/stopped, got {fields['status']!r}")
        sets.append("status = ?"); vals.append(fields["status"])
    if sets:
        vals.append(venture_id)
        vals.append(user_id)
        conn.execute(f"UPDATE venture SET {', '.join(sets)} WHERE id = ? AND user_id = ?", vals)
        conn.commit()
    row = conn.execute("SELECT * FROM venture WHERE id = ? AND user_id = ?", (venture_id, user_id)).fetchone()
    return None if row is None else _venture_dict(row)


def delete_venture(conn, user_id, venture_id) -> None:
    conn.execute("DELETE FROM venture WHERE id = ? AND user_id = ?", (venture_id, user_id))
    conn.commit()


def venture_flows(conn, user_id, venture) -> dict:
    """Monthly revenue/cost flows for a venture's linked tag or account.

    Correctness rules (DEC-020, devils-advocate findings 5/6):
      - transfers are EXCLUDED (card-payoff pairs would post phantom costs);
      - refunds REDUCE cost (mirrors month_actuals' sign flip);
      - everything linked counts regardless of date (a deliberately tagged old
        transaction is data, not noise) — pace math handles the time axis.
    Accepts a _venture_dict (camelCase keys). Returns cents. Scoped to `user_id` so a
    venture never tallies another user's transactions even if names/tags collide."""
    base = ("SELECT substr(t.posted_on,1,7) AS m, t.direction, t.kind, "
            "SUM(t.amount_cents) AS s, COUNT(*) AS c FROM txn t ")
    if venture.get("tag"):
        sql = base + ("JOIN txn_tag jt ON jt.txn_id = t.id JOIN tag tg ON tg.id = jt.tag_id "
                      "WHERE tg.name = ? COLLATE NOCASE AND t.is_transfer = 0 AND t.user_id = ? "
                      "GROUP BY m, t.direction, t.kind")
        vals: tuple = (venture["tag"], user_id)
    elif venture.get("accountId") is not None:
        sql = base + ("WHERE t.account_id = ? AND t.is_transfer = 0 AND t.user_id = ? "
                      "GROUP BY m, t.direction, t.kind")
        vals = (venture["accountId"], user_id)
    else:
        return {"byMonth": {}, "revenueCents": 0, "costCents": 0, "txnCount": 0}
    by_month: dict = {}
    revenue = cost = count = 0
    for r in conn.execute(sql, vals).fetchall():
        m = by_month.setdefault(r["m"], {"revenueCents": 0, "costCents": 0})
        count += r["c"]
        # Refunds sign-flip on EVERY direction (mirrors month_actuals — review finding 1):
        # an out-refund reduces cost, an in-refund reduces revenue.
        s = -r["s"] if r["kind"] == "refund" else r["s"]
        if r["direction"] == "in":
            m["revenueCents"] += s; revenue += s
        else:
            m["costCents"] += s; cost += s
    return {"byMonth": by_month, "revenueCents": revenue, "costCents": cost, "txnCount": count}


def goal_saved_cents(conn, user_id, goal) -> int:
    """Saved-so-far in cents. Precedence (deliberate, review finding 3): the linked
    account's LATEST balance snapshot when one exists; a linked account with NO
    snapshots yet falls back to the manual figure (linking must never make progress
    vanish while the first balance update is pending); manual-only goals use the
    manual figure. Accepts a _goal_dict (camelCase keys). Scoped by `user_id` (defense
    in depth — the linked account_id is already guaranteed to be the caller's own via
    _require_own_account at goal creation/update time)."""
    acct = goal.get("accountId")
    if acct is not None:
        row = conn.execute(
            "SELECT balance_cents FROM balance_snapshot WHERE account_id = ? AND user_id = ? "
            "ORDER BY as_of DESC, id DESC LIMIT 1",
            (acct, user_id)).fetchone()
        if row is not None:
            return row["balance_cents"]
    manual = goal.get("manualSaved")
    return 0 if manual is None else round(manual * 100)


# ---------- sinking funds (TODO-238, DEC-034, docs/sinking-funds-design.md) ----------

def _fund_dict(row) -> dict:
    return {
        "id": row["id"], "name": row["name"], "bucket": row["bucket"],
        "monthlyContribution": row["monthly_contribution_cents"] / 100.0,
        "target": None if row["target_cents"] is None else row["target_cents"] / 100.0,
        "targetDate": row["target_date"], "recurrence": row["recurrence"],
        "status": row["status"], "createdAt": row["created_at"],
    }


def _valid_fund_recurrence(recurrence) -> str:
    """'none' (default, today's one-time-target behavior) or 'yearly' (TODO-238
    amendment — the reserve math is unchanged; only the display-layer effective target
    date rolls forward, see tracking.fund_effective_target_date)."""
    r = str(recurrence if recurrence is not None else "none").strip().lower()
    if r not in ("none", "yearly"):
        raise ValueError(f"recurrence must be 'none' or 'yearly', got {recurrence!r}")
    return r


def _valid_fund_bucket(bucket) -> str | None:
    """A fund's envelope bucket must be a real spend bucket, never 'investment' — DEC-010
    already special-cases investment as permanent savings excluded from spend, a different
    concept than a sinking fund's reserve (§4.3)."""
    if bucket is None:
        return None
    b = str(bucket).strip()
    if not b:
        raise ValueError("bucket must not be empty")
    if b == "investment":
        raise ValueError("fund bucket must be a spend bucket, not 'investment'")
    return b


def create_fund(conn, user_id, name, *, bucket=None, monthly_contribution_cents=0,
                 target_cents=None, target_date=None, recurrence="none") -> dict:
    if not str(name or "").strip():
        raise ValueError("name must not be empty")
    bucket = _valid_fund_bucket(bucket)
    if not isinstance(monthly_contribution_cents, int) or monthly_contribution_cents < 0:
        raise ValueError(f"monthly_contribution_cents must be an int >= 0, got {monthly_contribution_cents!r}")
    if target_cents is not None and (not isinstance(target_cents, int) or target_cents <= 0):
        raise ValueError(f"target_cents must be an int > 0, got {target_cents!r}")
    if target_date is not None:
        target_date = _valid_goal_date(target_date)
    recurrence = _valid_fund_recurrence(recurrence)
    cur = conn.execute(
        """INSERT INTO fund (user_id, name, bucket, monthly_contribution_cents, target_cents, target_date, recurrence, status, created_at)
           VALUES (?,?,?,?,?,?,?,'active',?)""",
        (user_id, str(name).strip(), bucket, monthly_contribution_cents, target_cents, target_date, recurrence, _now()))
    conn.commit()
    return _fund_dict(conn.execute("SELECT * FROM fund WHERE id = ?", (cur.lastrowid,)).fetchone())


def list_funds(conn, user_id, include_archived=False) -> list[dict]:
    q = ("SELECT * FROM fund WHERE user_id = ?" +
         ("" if include_archived else " AND status = 'active'") + " ORDER BY name, id")
    return [_fund_dict(r) for r in conn.execute(q, (user_id,)).fetchall()]


def update_fund(conn, user_id, fund_id, **fields) -> dict | None:
    """Patch a fund. `clear_target`/`clear_target_date` are handled by the caller
    (server.py) translating to explicit `target_cents=None`/`target_date=None` fields —
    mirrors goal's `clear_account` convention (None can't itself signal "unset")."""
    allowed = {"name", "bucket", "monthly_contribution_cents", "target_cents", "target_date", "recurrence", "status"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown fund fields: {sorted(unknown)}")
    if "name" in fields and not str(fields["name"] or "").strip():
        raise ValueError("name must not be empty")
    if "bucket" in fields:
        fields["bucket"] = _valid_fund_bucket(fields["bucket"])
    if "monthly_contribution_cents" in fields and (
            not isinstance(fields["monthly_contribution_cents"], int) or fields["monthly_contribution_cents"] < 0):
        raise ValueError(
            f"monthly_contribution_cents must be an int >= 0, got {fields['monthly_contribution_cents']!r}")
    if fields.get("target_cents") is not None and (
            not isinstance(fields["target_cents"], int) or fields["target_cents"] <= 0):
        raise ValueError(f"target_cents must be an int > 0, got {fields['target_cents']!r}")
    if fields.get("target_date") is not None:
        fields["target_date"] = _valid_goal_date(fields["target_date"])
    if "recurrence" in fields:
        fields["recurrence"] = _valid_fund_recurrence(fields["recurrence"])
    if "status" in fields and fields["status"] not in ("active", "archived"):
        raise ValueError(f"status must be active/archived, got {fields['status']!r}")
    sets, vals = [], []
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        vals.append(str(v).strip() if k == "name" else v)
    if sets:
        vals.append(fund_id)
        vals.append(user_id)
        conn.execute(f"UPDATE fund SET {', '.join(sets)} WHERE id = ? AND user_id = ?", vals)
        conn.commit()
    row = conn.execute("SELECT * FROM fund WHERE id = ? AND user_id = ?", (fund_id, user_id)).fetchone()
    return None if row is None else _fund_dict(row)


def _require_own_fund(conn, user_id, fund_id) -> dict:
    """Cross-entity ownership guard (mirrors _require_own_account): fund_id must belong
    to the caller's own scope before use. Returns the raw fund dict."""
    row = conn.execute("SELECT * FROM fund WHERE id = ? AND user_id = ?", (fund_id, user_id)).fetchone()
    if row is None:
        raise ValueError(f"fund {fund_id} does not exist")
    return _fund_dict(row)


def fund_monthly_flows(conn, user_id, fund_id, upto_month=None) -> dict[str, dict]:
    """Per-month {contributeCents, drawCents} for one fund's linked transactions, scoped
    to user_id (defense in depth — fund_id is already the caller's own via
    _require_own_fund at every call site; the join also re-checks t.user_id so a foreign
    txn slipped into fund_txn by any future bug can never leak into the reserve math).

    `upto_month` ('YYYY-MM') bounds rows to postedOn <= month_end(upto_month), mirroring
    list_txns' date_to bound — a fund's reserve as of month M must never be influenced by
    data dated after M. None folds the fund's ENTIRE history (used by the hard-delete
    reserve guard, where "is there money left" must reflect everything ever logged).

    Returns a dict keyed by 'YYYY-MM' — only months with recorded contribute/draw
    activity are present; a month absent is zero activity for BOTH roles (the caller,
    tracking.fund_rollup, treats an absent month identically to one explicitly zeroed)."""
    sql = ("SELECT substr(t.posted_on,1,7) AS m, ft.role, SUM(t.amount_cents) AS s "
           "FROM fund_txn ft JOIN txn t ON t.id = ft.txn_id "
           "WHERE ft.fund_id = ? AND t.user_id = ?")
    vals: list = [fund_id, user_id]
    if upto_month is not None:
        sql += " AND t.posted_on <= ?"
        vals.append(tracking.month_end(upto_month))
    sql += " GROUP BY m, ft.role"
    out: dict = {}
    for r in conn.execute(sql, vals).fetchall():
        d = out.setdefault(r["m"], {"contributeCents": 0, "drawCents": 0})
        if r["role"] == "contribute":
            d["contributeCents"] = r["s"]
        else:
            d["drawCents"] = r["s"]
    return out


def list_fund_txns(conn, user_id, fund_id) -> list[dict]:
    """Draw/contribution history for one fund (drives the Actuals fund-chip's history
    list) — a lean projection scoped + ownership-checked, newest first."""
    _require_own_fund(conn, user_id, fund_id)
    rows = conn.execute(
        "SELECT ft.role, t.id AS txn_id, t.posted_on, t.amount_cents, t.description, t.category "
        "FROM fund_txn ft JOIN txn t ON t.id = ft.txn_id "
        "WHERE ft.fund_id = ? AND t.user_id = ? ORDER BY t.posted_on DESC, t.id DESC",
        (fund_id, user_id)).fetchall()
    return [{
        "txnId": r["txn_id"], "role": r["role"], "postedOn": r["posted_on"],
        "amount": r["amount_cents"] / 100.0, "description": r["description"], "category": r["category"],
    } for r in rows]


def link_fund_txn(conn, user_id, fund_id, txn_id, role) -> dict:
    """Link an existing transaction to a fund as a contribution or draw (§7's fund_txn
    link table). One fund per txn (UNIQUE via PRIMARY KEY(txn_id)) — a txn already linked
    to ANY fund (including this same fund) is rejected, so a dollar is never double-
    counted across funds (§4.3). `txn_id` must be the caller's OWN transaction (mirrors
    _require_own_account) — a foreign or nonexistent id is rejected identically to a
    foreign or nonexistent fund_id."""
    if role not in ("contribute", "draw"):
        raise ValueError(f"role must be 'contribute' or 'draw', got {role!r}")
    _require_own_fund(conn, user_id, fund_id)
    if not conn.execute("SELECT 1 FROM txn WHERE id = ? AND user_id = ?", (txn_id, user_id)).fetchone():
        raise ValueError(f"txn {txn_id} does not exist")
    existing = conn.execute("SELECT fund_id FROM fund_txn WHERE txn_id = ?", (txn_id,)).fetchone()
    if existing is not None:
        raise ValueError(f"txn {txn_id} is already linked to fund {existing['fund_id']}")
    conn.execute("INSERT INTO fund_txn (fund_id, txn_id, role) VALUES (?,?,?)", (fund_id, txn_id, role))
    conn.commit()
    return {"fundId": fund_id, "txnId": txn_id, "role": role}


def unlink_fund_txn(conn, user_id, fund_id, txn_id) -> None:
    """Remove a fund<->txn link. No-op if not linked or foreign to this fund (mirrors
    delete_account/delete_goal's silent-no-op convention) — the fund ownership check
    still runs first so a member can never even probe another user's fund's links."""
    _require_own_fund(conn, user_id, fund_id)
    conn.execute("DELETE FROM fund_txn WHERE fund_id = ? AND txn_id = ?", (fund_id, txn_id))
    conn.commit()


def delete_fund(conn, user_id, fund_id, *, hard=False, force=False) -> dict:
    """Archive-by-default deletion (DEC-034 §4.3, architect's call). `hard=False`
    (default): sets status='archived' — stops new flows, preserves history and past
    excusals; always allowed, idempotent, a no-op (not an error) if the fund doesn't
    exist. `hard=True`: actually DELETEs the fund row (fund_txn cascades via ON DELETE
    CASCADE) — blocked with ValueError when the fund's ALL-TIME reserve is nonzero unless
    `force=True` is also passed, because a hard delete retroactively reverts past funded
    draws to raw spend, re-blowing past months' funded view. Returns
    {"deleted": bool, "archived": bool, "hard": bool}."""
    row = conn.execute("SELECT * FROM fund WHERE id = ? AND user_id = ?", (fund_id, user_id)).fetchone()
    if row is None:
        return {"deleted": False, "archived": False, "hard": hard}
    if not hard:
        conn.execute("UPDATE fund SET status = 'archived' WHERE id = ? AND user_id = ?", (fund_id, user_id))
        conn.commit()
        return {"deleted": False, "archived": True, "hard": False}
    if not force:
        flows = fund_monthly_flows(conn, user_id, fund_id)
        reserve = tracking.fund_rollup(flows)["reserve"]
        if reserve:
            raise ValueError(
                f"fund {fund_id} has a nonzero reserve (${reserve:.2f}); pass force=true to "
                "hard-delete anyway (past funded draws will revert to raw spend)")
    conn.execute("DELETE FROM fund WHERE id = ? AND user_id = ?", (fund_id, user_id))
    conn.commit()
    return {"deleted": True, "archived": False, "hard": True}


# ---------- household shared-budget layer, Slice A (TODO-232, DEC-041,
# docs/shared-budget-design.md §4/§5/§9/§10) ----------
#
# HOUSEHOLD-scoped, not user_id-scoped (DEC-030 implicit singleton): the CRUD functions
# below (`create_/update_/delete_/get_household_budget`) still take no `user_id`/scopeId
# ownership filter — a shared line is one row potentially several household members
# read/write together, not owned by a single scope the way account/txn/goal/venture/fund
# are. WHO may reach a given line at all is a separate ACCESS MODEL, layered on top by
# `household_budget_access`/`list_household_budget` (issue #3 amendment, review
# 2026-08-06 — the user's directed choice, "only people you invite"): the owner may
# access every line; a PARTICIPANT (has a share row on THIS line) may read/edit that one
# line; everyone else gets nothing (§10's original "none use the scopeId data filter"
# undersold this — it's not unrestricted, it's participation-gated). server.py's
# endpoints call the access check BEFORE any mutation/read of a specific line_id; the
# CRUD functions themselves stay access-agnostic (mirrors `get_household_budget`'s
# existing plain "fetch by id" shape) so the access POLICY lives in exactly one place.
#
# A PURE expenses layer (DEC-041): this section must NEVER read or write filing status,
# and must NEVER auto-derive a split ratio from another member's income (§13 honesty
# boundary) — split ratios are always an explicit value the caller supplies.
#
# Slice A ships CRUD only. The actuals side (the joint account, A2) and the household
# rollup (C) are later slices — this section has no txn/account awareness at all.

def household_member_scopes(conn: sqlite3.Connection) -> dict[str, dict]:
    """Every current household member's DATA-SCOPE id -> {id, role, displayName}. The
    scope key is the SAME scopeId resolve_user() would hand that member for their own
    data ('__owner__' for the owner role regardless of their real HA id, the real id for
    every other member) — the join key `household_budget_share.user_id` always uses, and
    the set a shared line's `shares[].userId` must always be drawn from.

    Reused for (a) validating a shared line's per-member shares are real, CURRENT
    household members (`_validate_household_shares` below), and (b) the roster naming
    (`displayName`) folded into each share in the read shape (`_household_budget_dict`).
    `displayName` here already applies the owner-transfer-picker precedence (label wins
    over the header-captured display_name, falling back to the raw id) — mirrors
    index.html's `label || displayName || id` convention, kept server-side here since
    this is the one surface where a PARTNER's name is rendered inside another member's
    own view (§6.2's "you funded $1400 · Alex funded $1100" — same naming plumbing)."""
    out: dict[str, dict] = {}
    for u in list_users(conn):
        scope = _SENTINEL_OWNER_ID if u["role"] == "owner" else u["id"]
        out[scope] = {
            "id": u["id"], "role": u["role"],
            "displayName": u["label"] or u["displayName"] or u["id"],
        }
    return out


def _validate_household_shares(conn: sqlite3.Connection, type_: str, shares) -> list[dict]:
    """Normalize + validate a shared line's per-member shares against *type_* ('split' or
    'pooled') and the CURRENT household roster. Returns a list of
    {userId, splitRatioBps, contributionCents} dicts (exactly one of the two money
    fields populated, per the line's type) ready to persist. Raises ValueError on any
    violation — every caller maps that to HTTP 422.

    Rules (§4, §9, §10's contract notes):
      - shares must be a non-empty list; every userId must be a CURRENT household member
        (household_member_scopes) and appear at most once;
      - split: every share supplies an integer ratioBps in [0, 10000] (0 valid — a 100/0
        line, §6.5's "see a line I don't personally pay"); the ratios across ALL shares
        must sum to EXACTLY 10000 (100%);
      - pooled: every share supplies an integer contributionCents >= 0; no sum
        constraint (the pool's budget is simply Σ contributions, derived at read time)."""
    if not isinstance(shares, list) or not shares:
        raise ValueError("shares must be a non-empty list")
    scopes = household_member_scopes(conn)
    seen: set = set()
    normalized: list[dict] = []
    for s in shares:
        uid = str((s or {}).get("userId") or "").strip()
        if not uid:
            raise ValueError("every share needs a userId")
        if uid not in scopes:
            raise ValueError(f"userId {uid!r} is not a current household member")
        if uid in seen:
            raise ValueError(f"userId {uid!r} appears more than once in shares")
        seen.add(uid)
        ratio = (s or {}).get("ratioBps")
        contrib = (s or {}).get("contributionCents")
        if type_ == "split":
            if contrib is not None:
                raise ValueError(f"split share for {uid!r} takes ratioBps, not contributionCents")
            if not isinstance(ratio, int) or isinstance(ratio, bool) or ratio < 0 or ratio > 10000:
                raise ValueError(f"split share for {uid!r} needs an integer ratioBps in 0..10000")
            normalized.append({"userId": uid, "splitRatioBps": ratio, "contributionCents": None})
        else:
            if ratio is not None:
                raise ValueError(f"pooled share for {uid!r} takes contributionCents, not ratioBps")
            if not isinstance(contrib, int) or isinstance(contrib, bool) or contrib < 0:
                raise ValueError(f"pooled share for {uid!r} needs an integer contributionCents >= 0")
            normalized.append({"userId": uid, "splitRatioBps": None, "contributionCents": contrib})
    if type_ == "split":
        total_bps = sum(n["splitRatioBps"] for n in normalized)
        if total_bps != 10000:
            raise ValueError(f"split ratios must sum to 10000 bps (100%), got {total_bps}")
    return normalized


def _household_budget_dict(conn: sqlite3.Connection, row) -> dict:
    """Base read shape for one shared line — everything EXCEPT `yourShareCents`, which is
    reader-relative (depends on the calling scope), not a property of the line itself;
    server.py folds that in per caller via `household_your_share_cents` below."""
    scopes = household_member_scopes(conn)
    shares = []
    for r in conn.execute(
            "SELECT user_id, split_ratio_bps, contribution_cents FROM household_budget_share "
            "WHERE line_id = ? ORDER BY user_id", (row["id"],)).fetchall():
        info = scopes.get(r["user_id"])
        shares.append({
            "userId": r["user_id"],
            "displayName": info["displayName"] if info else r["user_id"],
            "ratioBps": r["split_ratio_bps"],
            "contributionCents": r["contribution_cents"],
        })
    return {
        "id": row["id"], "name": row["name"], "bucket": row["bucket"], "type": row["type"],
        "totalCents": row["total_cents"], "status": row["status"],
        "createdBy": row["created_by"], "createdAt": row["created_at"],
        "shares": shares,
    }


def _household_split_allocation(total_cents: int, shares: list[dict]) -> dict[str, int]:
    """Largest-remainder allocation of *total_cents* across *shares* (each a dict with
    'userId' and 'ratioBps'), so the returned per-member cents PROVABLY SUM to exactly
    total_cents. Independent per-share rounding does NOT have this property (review
    finding 2026-08-06: a truncation mutant in the old `round(total * ratio / 10000)`
    passed the full suite because every existing test used evenly-divisible ratios --
    the flagship 50/50 case actually drifts on every odd-cent total, e.g. $100.01 split
    50/50 -> $50.00 + $50.00 = $100.00, one cent short of the plan, and Slice C's
    household card compares the household's `plannedCents` total against the SUM of
    members' `yourShareCents` -- a mismatch there would be a visible, confusing bug).

    Method: floor every share's ideal fractional amount (`total_cents * ratioBps /
    10000`), then hand out the few leftover cents ONE AT A TIME to the shares with the
    largest fractional remainder -- the standard largest-remainder / Hamilton
    apportionment method. Ties broken by `userId` ascending for a deterministic,
    reproducible allocation regardless of caller-supplied ordering.

    Guaranteed `sum(returned.values()) == total_cents` whenever `sum(ratioBps) ==
    10000` (already enforced by `_validate_household_shares` at write time) -- the
    fractional parts left behind by flooring are exactly the integer number of leftover
    cents (see the proof in this function's test coverage), so `remainder_cents` is
    always in `[0, len(shares))` and every leftover cent is assigned to a distinct
    share."""
    ideal = [(s["userId"], total_cents * (s.get("ratioBps") or 0) / 10000) for s in shares]
    floors = {uid: int(v) for uid, v in ideal}   # int() truncates toward zero == floor for v >= 0
    remainder_cents = total_cents - sum(floors.values())
    order = sorted(ideal, key=lambda t: (-(t[1] - int(t[1])), t[0]))
    out = dict(floors)
    for uid, _ in order[:remainder_cents]:
        out[uid] += 1
    return out


def household_your_share_cents(line: dict, scope: str) -> int:
    """The calling *scope*'s own share of *line* (a `_household_budget_dict` shape), in
    cents (§4/§6.5's fold — this is the exact figure a later slice's Budget-tab overlay
    plugs straight into `myShare`). Split: the caller's slot in
    `_household_split_allocation` (largest-remainder — the per-member cents across ALL
    of a line's shares are guaranteed to sum to `totalCents` exactly, never drifting by
    a cent on an unevenly-divisible total or ratio split). Pooled: the caller's own
    contributionCents. 0 if the caller has no participation row on this line at all
    (e.g. a third household member on a couple's line, or a line predating them) — never
    an error; a non-participant simply sees $0 of it, not a crash."""
    mine = next((s for s in line["shares"] if s["userId"] == scope), None)
    if mine is None:
        return 0
    if line["type"] == "split":
        allocation = _household_split_allocation(line["totalCents"] or 0, line["shares"])
        return allocation.get(scope, 0)
    return mine["contributionCents"] or 0


def create_household_budget(conn: sqlite3.Connection, created_by: str, name, bucket, type_, shares,
                             total_cents=None) -> dict:
    """Create a shared budget line. *created_by* is the caller's scopeId — an audit trail
    field only (§9: "not an owner gate"); any household member may create a line, and
    every member may edit or archive it afterward regardless of who created it."""
    name = str(name or "").strip()
    if not name:
        raise ValueError("name must not be empty")
    bucket = str(bucket or "").strip()
    if not bucket:
        raise ValueError("bucket must not be empty")
    if type_ not in ("split", "pooled"):
        raise ValueError(f"type must be 'split' or 'pooled', got {type_!r}")
    if type_ == "split":
        if not isinstance(total_cents, int) or isinstance(total_cents, bool) or total_cents <= 0:
            raise ValueError("split lines need an integer totalCents > 0")
    else:
        if total_cents is not None:
            raise ValueError("pooled lines derive totalCents from contributions -- do not pass totalCents")
        total_cents = None
    normalized = _validate_household_shares(conn, type_, shares)
    cur = conn.execute(
        "INSERT INTO household_budget (name, bucket, type, total_cents, status, created_by, created_at) "
        "VALUES (?,?,?,?,'active',?,?)",
        (name, bucket, type_, total_cents, created_by, _now()))
    line_id = cur.lastrowid
    for s in normalized:
        conn.execute(
            "INSERT INTO household_budget_share (line_id, user_id, split_ratio_bps, contribution_cents) "
            "VALUES (?,?,?,?)",
            (line_id, s["userId"], s["splitRatioBps"], s["contributionCents"]))
    conn.commit()
    return _household_budget_dict(conn, conn.execute(
        "SELECT * FROM household_budget WHERE id = ?", (line_id,)).fetchone())


def household_budget_access(conn: sqlite3.Connection, line_id, caller_scope: str, is_owner: bool) -> bool:
    """Access model (review finding 2026-08-06, issue #3 amendment — the user's directed
    choice, "only people you invite"): the OWNER can access any line; a PARTICIPANT (has
    a share row on THIS line) can access that one line; everyone else — including an
    account auto-provisioned on its very first request (DEC-026/031) — gets nothing.

    Gate is "having a share row", NEVER "yourShareCents > 0" — a 0-ratio 100/0 line
    (§6.5, "see a line I don't personally pay") is a real participant with a real $0
    share; excluding them would silently break the one scenario the ratio exists for.

    "Tie the check to current membership ∩ participation": *caller_scope* is, by
    construction, always a CURRENT household member's scope — it can only ever be
    the value `resolve_user()` just handed out for THIS request, and that resolver
    never returns anything but a live, currently-provisioned identity's scope. So
    matching `household_budget_share.user_id == caller_scope` directly already IS the
    membership-intersected check: a share row keyed to some OTHER, no-longer-current id
    (the exact failure mode `transfer_ownership`/`redeem_link_code` used to leave
    behind, fixed above) can never equal a live caller's own scope, so it can never
    confer access to anyone by accident."""
    if is_owner:
        return True
    return conn.execute(
        "SELECT 1 FROM household_budget_share WHERE line_id = ? AND user_id = ?",
        (line_id, caller_scope),
    ).fetchone() is not None


def list_household_budget(conn: sqlite3.Connection, caller_scope: str, is_owner: bool,
                          include_archived=False) -> list[dict]:
    """The lines *caller_scope* may see (issue #3 amendment access model, see
    `household_budget_access`): the owner sees every line, HOUSEHOLD-wide; anyone else
    sees ONLY the lines they participate in (have a share row on) — never a 403 that
    would confirm OTHER lines exist, just a filtered (possibly empty) list, matching how
    every other surface in this app already degrades for a non-owner/non-participant
    (empty scope, or 404). `caller_scope`/`is_owner` are REQUIRED positionals (mirrors
    this codebase's "a call can never be unscoped" convention for every other
    user_id-scoped list_* function — R3, tests/test_multiuser_scoping.py) — there is no
    "give me everything" default a caller could reach by omission.

    `yourShareCents` is NOT included here; server.py folds it in per the calling scope."""
    q = "SELECT * FROM household_budget"
    conds = [] if include_archived else ["status = 'active'"]
    params: list = []
    if not is_owner:
        conds.append("id IN (SELECT line_id FROM household_budget_share WHERE user_id = ?)")
        params.append(caller_scope)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY name, id"
    return [_household_budget_dict(conn, r) for r in conn.execute(q, params).fetchall()]


def get_household_budget(conn: sqlite3.Connection, line_id) -> dict | None:
    row = conn.execute("SELECT * FROM household_budget WHERE id = ?", (line_id,)).fetchone()
    return None if row is None else _household_budget_dict(conn, row)


#: Fields on a shared line that only the OWNER may PATCH (BUG-0014). These are
#: household-level facts about the shared bill, plus `status` — which is LIFECYCLE, the
#: thing `delete_household_budget_endpoint`'s docstring already reserves to the owner
#: ("the participant grant is explicitly limited to read + edit, never delete in either
#: mode"). `PATCH status='archived'` reached exactly that archive through a different
#: verb, which is why listing `status` here is not belt-and-braces but the actual hole.
_HB_OWNER_ONLY_FIELDS = ("name", "bucket", "type", "total_cents", "status")


def _authorize_household_patch(conn: sqlite3.Connection, row, fields, caller_scope: str,
                               is_owner: bool) -> None:
    """Enforce WHICH FIELDS a non-owner participant may PATCH (BUG-0014). Raises
    PermissionError (server.py maps to 403) on violation; returns None when allowed.

    `household_budget_access` answers "may this caller touch this line AT ALL" and is a
    necessary gate, but it was also the ONLY one: every field was writable by anyone who
    passed it, so a participant could rewrite `shares` to give themselves 10000 bps and
    zero the owner out, archive the line via `status`, or restate the household's bill
    via `total_cents`. Access is not authority — this function supplies the missing half.

    The rule, derived from the model the code already documents (participants get
    "read + edit", owner keeps lifecycle):
      - owner: everything, unchanged;
      - participant: may adjust ONLY THEIR OWN `contributionCents`, on a POOLED line.

    Why split ratios are owner-only even though "editing your own share" sounds
    symmetrical: split ratios must sum to exactly 10000 bps, so a participant CANNOT
    change their own ratio without a compensating change to somebody else's row. There
    is no such thing as a self-only ratio edit — every one of them spends another
    member's money. Pooled contributions are genuinely independent (the line's total is
    derived as Σ contributions), so a self-only edit is well-defined there and only there.

    DENY BY DEFAULT: any supplied `shares` payload this function cannot positively prove
    is a self-only contribution edit — different roster, unparseable rows, caller absent
    from their own line — raises rather than falling through to the write. A malformed
    payload must not reach the mutation path on a non-owner's authority and get its
    verdict from validation instead."""
    if is_owner:
        return
    forbidden = sorted(f for f in _HB_OWNER_ONLY_FIELDS if f in fields)
    if forbidden:
        raise PermissionError(
            "Only the household owner can change " + ", ".join(forbidden)
            + " on a shared line. You can adjust what you contribute.")
    if "shares" not in fields:
        return
    if row["type"] != "pooled":
        raise PermissionError(
            "Only the household owner can change the split on a shared line -- split "
            "ratios must total 100%, so changing yours would change someone else's.")
    current = {
        r["user_id"]: r["contribution_cents"]
        for r in conn.execute(
            "SELECT user_id, contribution_cents FROM household_budget_share WHERE line_id = ?",
            (row["id"],))
    }
    if caller_scope not in current:
        raise PermissionError("You are not a participant on this shared budget line.")
    supplied: dict = {}
    for s in fields["shares"]:
        uid = str((s or {}).get("userId") or "").strip()
        if not uid or uid in supplied:
            raise PermissionError("Only the household owner can change who is on a shared line.")
        supplied[uid] = (s or {}).get("contributionCents")
    if set(supplied) != set(current):
        raise PermissionError(
            "Only the household owner can add or remove people from a shared line.")
    for uid, contrib in supplied.items():
        if uid == caller_scope:
            continue
        if contrib != current[uid]:
            raise PermissionError(
                "You can only change your own contribution to a shared budget line.")


def update_household_budget(conn: sqlite3.Connection, line_id, *, caller_scope: str,
                            is_owner: bool, **fields) -> dict | None:
    """Patch a shared line.

    `caller_scope`/`is_owner` are REQUIRED keyword-only args, mirroring
    `list_household_budget`'s "a call can never be unscoped" convention (R3): field-level
    authorization (`_authorize_household_patch`, BUG-0014) is not something a caller can
    reach past by omitting an argument, and adding them as required is what makes every
    pre-existing call site a load-bearing compile-time question rather than a silent
    default-to-permissive.

    `shares` is ONLY re-validated (and rewritten) when the caller actually supplies a
    new `shares` list, or supplies `type` (which requires `shares` in the SAME call --
    see below). A `total_cents`-only (or name/bucket/status-only) PATCH NEVER touches
    `household_budget_share` at all — total_cents and the roster of who's participating
    are independent facts (§4/§9), so there is no reason to revalidate membership for a
    change that has nothing to do with it. This matters concretely: `transfer_ownership`
    and `redeem_link_code` can leave a share keyed to an id no longer held by any
    current member (see those functions' docstrings) — a household member must still be
    able to bump the rent's totalCents while that repair is pending, not get hard-locked
    out of the line by a 422 for a participant they didn't even touch (review finding
    2026-08-06). Re-validating a STALE participant is still required the moment the
    caller actually edits `shares` or flips `type` — this only relaxes the totally
    unrelated fields.

    `type` MUST be accompanied by a `shares` list in the SAME call (raises otherwise) --
    a type flip changes what "valid" means for every share (ratioBps vs
    contributionCents), so silently re-interpreting the OLD shares under the NEW type
    would either misfire on a shape mismatch or, worse, silently reinterpret stale
    numbers as the wrong kind of money. Supplying fresh shares is mandatory, not
    inferred."""
    allowed = {"name", "bucket", "type", "total_cents", "shares", "status"}
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown household_budget fields: {sorted(unknown)}")
    row = conn.execute("SELECT * FROM household_budget WHERE id = ?", (line_id,)).fetchone()
    if row is None:
        return None
    # BUG-0014: field-level authorization, BEFORE any mutation and before `shares`
    # validation -- a non-owner must never reach the write path, and must not be able to
    # distinguish "forbidden" from "malformed" by which error comes back first.
    _authorize_household_patch(conn, row, fields, caller_scope, is_owner)
    effective_type = fields.get("type", row["type"])
    if effective_type not in ("split", "pooled"):
        raise ValueError(f"type must be 'split' or 'pooled', got {effective_type!r}")

    sets, vals = [], []
    if "name" in fields:
        nm = str(fields["name"] or "").strip()
        if not nm:
            raise ValueError("name must not be empty")
        sets.append("name = ?"); vals.append(nm)
    if "bucket" in fields:
        bk = str(fields["bucket"] or "").strip()
        if not bk:
            raise ValueError("bucket must not be empty")
        sets.append("bucket = ?"); vals.append(bk)
    if "status" in fields:
        if fields["status"] not in ("active", "archived"):
            raise ValueError(f"status must be active/archived, got {fields['status']!r}")
        sets.append("status = ?"); vals.append(fields["status"])

    type_changed = "type" in fields
    total_supplied = "total_cents" in fields
    shares_supplied = "shares" in fields
    if type_changed and not shares_supplied:
        raise ValueError("changing type requires supplying new shares in the same request")

    normalized: list[dict] | None = None
    if total_supplied or type_changed:
        if effective_type == "split":
            total_cents = fields["total_cents"] if total_supplied else row["total_cents"]
            if not isinstance(total_cents, int) or isinstance(total_cents, bool) or total_cents <= 0:
                raise ValueError("split lines need an integer totalCents > 0")
        else:
            if total_supplied and fields["total_cents"] is not None:
                raise ValueError("pooled lines derive totalCents from contributions -- do not pass totalCents")
            total_cents = None
        sets.append("type = ?"); vals.append(effective_type)
        sets.append("total_cents = ?"); vals.append(total_cents)

    if shares_supplied:
        # Orthogonal to the total_cents/type branch above: a shares-only PATCH (no type
        # or total_cents change) replaces the share rows without touching the parent's
        # type/total_cents columns at all -- they don't need touching.
        normalized = _validate_household_shares(conn, effective_type, fields["shares"])

    if sets:
        vals.append(line_id)
        conn.execute(f"UPDATE household_budget SET {', '.join(sets)} WHERE id = ?", vals)
    if normalized is not None:
        conn.execute("DELETE FROM household_budget_share WHERE line_id = ?", (line_id,))
        for s in normalized:
            conn.execute(
                "INSERT INTO household_budget_share (line_id, user_id, split_ratio_bps, contribution_cents) "
                "VALUES (?,?,?,?)",
                (line_id, s["userId"], s["splitRatioBps"], s["contributionCents"]))
    conn.commit()
    return get_household_budget(conn, line_id)


def delete_household_budget(conn: sqlite3.Connection, line_id, *, hard=False, confirm=False) -> dict:
    """Archive-by-default deletion (§10, mirrors `delete_fund`'s convention). `hard=False`
    (default): sets status='archived' — always allowed, idempotent, a no-op (not an
    error) if the line doesn't exist. `hard=True`: permanently DELETEs the line
    (`household_budget_share` cascades via ON DELETE CASCADE) and requires
    `confirm=True` — an explicit extra flag, not a reserve/history guard like
    `delete_fund`'s `force` (Slice A has no actuals awareness yet to compute one
    against); a bare `hard=true` without `confirm=true` is rejected rather than silently
    treated as an archive, so a caller's explicit intent is never downgraded quietly.
    Returns {"deleted": bool, "archived": bool, "hard": bool}."""
    row = conn.execute("SELECT * FROM household_budget WHERE id = ?", (line_id,)).fetchone()
    if row is None:
        return {"deleted": False, "archived": False, "hard": hard}
    if not hard:
        conn.execute("UPDATE household_budget SET status = 'archived' WHERE id = ?", (line_id,))
        conn.commit()
        return {"deleted": False, "archived": True, "hard": False}
    if not confirm:
        raise ValueError(f"hard delete of household budget line {line_id} requires confirm=true")
    conn.execute("DELETE FROM household_budget WHERE id = ?", (line_id,))
    conn.commit()
    return {"deleted": True, "archived": False, "hard": True}


# ----- recurring expectations (seeded from the budget; reconciled, never auto-created) -----

def _recurring_dict(r) -> dict:
    return {"id": r["id"], "bucket": r["bucket"], "category": r["category"],
            "direction": r["direction"], "dueDay": r["due_day"],
            "expected": round(r["expected_cents"] / 100.0, 2), "active": bool(r["active"])}


def upsert_recurring(conn, user_id, category, *, direction="out", bucket=None, due_day=None,
                     expected_cents=0, active=True) -> dict:
    """Create or update a recurring expectation keyed by (user_id, direction, bucket, category).
    Idempotent so re-seeding from the budget updates in place instead of duplicating."""
    if direction not in ("in", "out"):
        raise ValueError(f"direction must be 'in' or 'out', got {direction!r}")
    if bucket is not None and not str(bucket).strip():
        raise ValueError(f"bucket must not be empty")
    if not (category or "").strip():
        raise ValueError("category is required for a recurring item")
    if due_day is not None and not (1 <= int(due_day) <= 31):
        raise ValueError(f"due_day must be 1..31, got {due_day!r}")
    existing = conn.execute(
        "SELECT id FROM recurring WHERE user_id = ? AND direction = ? AND IFNULL(bucket,'') = IFNULL(?,'') "
        "AND category = ? COLLATE NOCASE", (user_id, direction, bucket, category)).fetchone()
    dd = None if due_day is None else int(due_day)
    if existing:
        conn.execute("UPDATE recurring SET due_day = ?, expected_cents = ?, active = ? WHERE id = ? AND user_id = ?",
                     (dd, int(expected_cents), 1 if active else 0, existing["id"], user_id))
        rid = existing["id"]
    else:
        cur = conn.execute(
            """INSERT INTO recurring (user_id, bucket, category, direction, due_day, expected_cents, active, created_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (user_id, bucket, category, direction, dd, int(expected_cents), 1 if active else 0, _now()))
        rid = cur.lastrowid
    conn.commit()
    return _recurring_dict(conn.execute("SELECT * FROM recurring WHERE id = ?", (rid,)).fetchone())


def list_recurring(conn, user_id) -> list[dict]:
    return [_recurring_dict(r) for r in conn.execute(
        "SELECT * FROM recurring WHERE user_id = ? ORDER BY direction, bucket, category", (user_id,)).fetchall()]


def delete_recurring(conn, user_id, recurring_id) -> None:
    conn.execute("DELETE FROM recurring WHERE id = ? AND user_id = ?", (recurring_id, user_id))
    conn.commit()


# ---------- scheduled money (schedules.py is the pure engine) ----------
#
# What is stored is the RULE. Occurrences are computed on demand, and only ever become
# transactions once their date has arrived. Two guards keep that honest, and both matter:
#
#   1. Nothing is posted before its date. `materialize_due_schedules` expands to `today`,
#      never past it.
#   2. Nothing is posted from before the schedule existed. A rule anchored years back (every
#      migrated bill is anchored at 2000-01-01 to carry its day-of-month) must not retroactively
#      post two decades of rent the first time auto-post is switched on. The window therefore
#      starts at the LATER of the anchor, the schedule's own creation date, and a bounded
#      catch-up horizon.
#
# Idempotency is enforced by the database, not by bookkeeping: `schedule_txn`'s composite
# primary key, plus `idx_txn_dedupe` on (source, external_id). Catch-up can run on every page
# load and cannot double-post.

_CATCHUP_DAYS = 60          # how far back a catch-up will reach; see guard 2 above


def _schedule_dict(r) -> dict:
    return {
        "id": r["id"], "name": r["name"], "direction": r["direction"],
        "amount": round(r["amount_cents"] / 100.0, 2),
        "amountIsEstimate": bool(r["amount_is_estimate"]),
        "accountId": r["account_id"], "toAccountId": r["to_account_id"],
        "bucket": r["bucket"], "category": r["category"], "description": r["description"],
        "freq": r["freq"], "interval": r["interval_n"], "weekdays": r["weekdays"],
        "day1": r["day_1"], "day2": r["day_2"], "monthOfYear": r["month_of_year"],
        "anchorOn": r["anchor_on"], "endMode": r["end_mode"], "endsOn": r["ends_on"],
        "endCount": r["end_count"], "weekendShift": r["weekend_shift"],
        "autoPost": bool(r["auto_post"]), "active": bool(r["active"]),
        "parentId": r["parent_id"], "createdAt": r["created_at"],
    }


def _schedule_row(conn, user_id, schedule_id):
    return conn.execute("SELECT * FROM schedule WHERE id = ? AND user_id = ?",
                        (schedule_id, user_id)).fetchone()


def _exceptions_for(conn, schedule_id) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM schedule_exception WHERE schedule_id = ?", (schedule_id,)).fetchall()]


def _validate_schedule_fields(conn, user_id, f: dict) -> dict:
    """Normalize + validate one schedule's fields. The rule half is validated by constructing
    a `schedules.Rule`, so the DB can never hold a rule the engine refuses to read — one
    definition of validity, not two that drift."""
    name = str(f.get("name") or "").strip()
    if not name:
        raise ValueError("name must not be empty")
    direction = f.get("direction")
    if direction not in ("in", "out", "transfer"):
        raise ValueError(f"direction must be in|out|transfer, got {direction!r}")

    amount_cents = int(f.get("amount_cents") or 0)
    if amount_cents < 0:
        raise ValueError("amount_cents must be >= 0")

    account_id = f.get("account_id")
    to_account_id = f.get("to_account_id")
    if account_id is not None:
        _require_own_account(conn, user_id, account_id)
    if direction == "transfer":
        if account_id is None or to_account_id is None:
            raise ValueError("a transfer needs both account_id and to_account_id")
        if account_id == to_account_id:
            raise ValueError("a transfer needs two different accounts")
        _require_own_account(conn, user_id, to_account_id)
    else:
        to_account_id = None

    bucket = f.get("bucket")
    if bucket is not None and not str(bucket).strip():
        raise ValueError("bucket must not be empty")

    # Raises ScheduleRuleError (a ValueError) on anything the engine cannot expand.
    rule = schedules.Rule(
        freq=f.get("freq"), anchor_on=f.get("anchor_on"), interval=f.get("interval_n", 1),
        weekdays=f.get("weekdays"), day_1=f.get("day_1"), day_2=f.get("day_2"),
        month_of_year=f.get("month_of_year"), ends_on=f.get("ends_on"),
        end_mode=f.get("end_mode", "never"), end_count=f.get("end_count"),
        weekend_shift=f.get("weekend_shift", "none"))

    weekdays = ",".join(schedules.WEEKDAY_CODES[w] for w in rule.weekdays) if rule.weekdays else None
    return {
        "name": name, "direction": direction, "amount_cents": amount_cents,
        "amount_is_estimate": 1 if f.get("amount_is_estimate") else 0,
        "account_id": account_id, "to_account_id": to_account_id,
        "bucket": bucket, "category": f.get("category"), "description": f.get("description"),
        "freq": rule.freq, "interval_n": rule.interval, "weekdays": weekdays,
        "day_1": rule.day_1, "day_2": rule.day_2, "month_of_year": rule.month_of_year,
        "anchor_on": rule.anchor_on.isoformat(),
        "end_mode": rule.end_mode,
        "ends_on": rule.ends_on.isoformat() if rule.ends_on else None,
        "end_count": rule.end_count,
        "weekend_shift": rule.weekend_shift,
        "auto_post": 1 if f.get("auto_post") else 0,
        "active": 0 if f.get("active") is False else 1,
        "parent_id": f.get("parent_id"),
    }


def create_schedule(conn, user_id, created_at=None, **fields) -> dict:
    """`created_at` is injectable because it is load-bearing, not bookkeeping: it is one of the
    three terms in the catch-up window (see `_window_start`), so a caller reconstructing history
    -- a migration, a restore, a test -- must be able to say when the schedule really began."""
    vals = _validate_schedule_fields(conn, user_id, fields)
    cols = ["user_id"] + list(vals) + ["created_at"]
    params = [user_id] + list(vals.values()) + [created_at or _now()]
    cur = conn.execute(
        f"INSERT INTO schedule ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", params)
    conn.commit()
    return _schedule_dict(conn.execute("SELECT * FROM schedule WHERE id = ?", (cur.lastrowid,)).fetchone())


def list_schedules(conn, user_id, include_inactive=True) -> list[dict]:
    sql = "SELECT * FROM schedule WHERE user_id = ?"
    if not include_inactive:
        sql += " AND active = 1"
    return [_schedule_dict(r) for r in conn.execute(sql + " ORDER BY name", (user_id,)).fetchall()]


def _fire_progress_dict(r) -> dict:
    import json as _json
    try:
        assumptions = _json.loads(r["assumptions"]) if r["assumptions"] else None
    except (ValueError, TypeError):
        assumptions = None          # a row written by an older shape must not break the list
    return {
        "id": r["id"],
        "on": r["on_date"],
        "netWorth": round(r["net_worth_cents"] / 100.0, 2),
        "fiTarget": round(r["fi_target_cents"] / 100.0, 2),
        "pctToFi": (round(r["net_worth_cents"] / r["fi_target_cents"], 6)
                    if r["fi_target_cents"] else None),
        "variantKey": r["variant_key"] or None,
        "assumptions": assumptions,
        "note": r["note"],
        "createdAt": r["created_at"],
    }


def log_fire_progress(conn, user_id, on: str, net_worth: float, fi_target: float,
                      variant_key: str | None = None, assumptions=None, note=None) -> dict:
    """Record where the user stands against their FI target, ON A GIVEN DAY.

    `fi_target` is stored, not derived. That is the point: it is a function of spending, withdrawal
    rate and variant choices that change over time and were never recorded, so a target read back
    later can only be the one that was true when the row was written.

    Re-logging the same day and target REPLACES the row rather than adding a second. A day has one
    net worth; logging twice because the first figure was wrong is the common case, and two
    contradictory points on one date is not a history, it is a bug the chart would render.
    """
    import json as _json
    try:
        on = datetime.strptime(str(on or "").strip(), "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        raise ValueError(f"on must be YYYY-MM-DD, got {on!r}")

    def _cents(v, field):
        try:
            c = round(float(v) * 100)
        except (TypeError, ValueError):
            raise ValueError(f"{field} must be a number, got {v!r}")
        if c < 0:
            raise ValueError(f"{field} must not be negative")
        return int(c)

    # Net worth MAY be zero or anything upward -- a reading of nothing is a real reading, and
    # somebody starting from nothing is exactly who this chart is for.
    nw_c = _cents(net_worth, "netWorth")
    fi_c = _cents(fi_target, "fiTarget")
    if fi_c <= 0:
        # A target of zero would make pctToFi a division by zero and the chart meaningless.
        raise ValueError("fiTarget must be greater than zero")
    key = (variant_key or "").strip()
    payload = _json.dumps(assumptions, sort_keys=True) if assumptions is not None else None
    conn.execute(
        "INSERT INTO fire_progress (user_id, on_date, net_worth_cents, fi_target_cents,"
        " variant_key, assumptions, note, created_at) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT (user_id, on_date, variant_key) DO UPDATE SET"
        "   net_worth_cents=excluded.net_worth_cents, fi_target_cents=excluded.fi_target_cents,"
        "   assumptions=excluded.assumptions, note=excluded.note, created_at=excluded.created_at",
        (user_id, on, nw_c, fi_c, key, payload, (note or None), _now()))
    conn.commit()
    row = conn.execute(
        "SELECT * FROM fire_progress WHERE user_id=? AND on_date=? AND variant_key=?",
        (user_id, on, key)).fetchone()
    return _fire_progress_dict(row)


def list_fire_progress(conn, user_id, variant_key: str | None = None, limit: int = 500) -> list[dict]:
    """Oldest first -- the order a chart plots, so no caller has to remember to reverse it."""
    sql = "SELECT * FROM fire_progress WHERE user_id=?"
    args: list = [user_id]
    if variant_key is not None:
        sql += " AND variant_key=?"
        args.append(variant_key or "")
    sql += " ORDER BY on_date ASC, id ASC LIMIT ?"
    args.append(int(limit))
    return [_fire_progress_dict(r) for r in conn.execute(sql, args).fetchall()]


def delete_fire_progress(conn, user_id, entry_id) -> bool:
    cur = conn.execute("DELETE FROM fire_progress WHERE user_id=? AND id=?", (user_id, entry_id))
    conn.commit()
    return cur.rowcount > 0


def schedules_in_window(conn, user_id, start: str, end: str, cap: int = 400) -> list[dict]:
    """Every ACTIVE schedule with its occurrences inside [start, end], for a month view.

    Exists because the Actuals tab's monthly reconciliation -- "did this bill come through?" --
    needs every schedule at once, and the per-schedule endpoint would make that N requests on
    every refresh. It is the same computation, batched: `schedule_occurrences` is called
    unchanged, so a month view can never disagree with the single-schedule list.

    `posted` means THIS ENGINE wrote a transaction for that date. It is NOT the same question as
    "does a matching transaction exist" -- a bill paid and logged by hand is unposted but has
    certainly come through. The caller reconciles that; this reports only what it knows.
    """
    out = []
    for row in conn.execute(
        "SELECT * FROM schedule WHERE user_id = ? AND active = 1 ORDER BY name", (user_id,)
    ).fetchall():
        d = _schedule_dict(row)
        posted = _posted_dates(conn, d["id"])
        hits = []
        for h in schedules.expand(schedules.Rule.from_row(row), _exceptions_for(conn, d["id"]),
                                  start, end, cap):
            on = h["on"].isoformat()
            hits.append({"on": on, "raw": h["raw"].isoformat(), "posted": on in posted,
                         "amount": round(h.get("amount_cents", row["amount_cents"]) / 100.0, 2),
                         "overridden": bool(h.get("overridden"))})
        d["occurrences"] = hits
        out.append(d)
    return out


def get_schedule(conn, user_id, schedule_id) -> dict | None:
    row = _schedule_row(conn, user_id, schedule_id)
    return _schedule_dict(row) if row else None


def update_schedule(conn, user_id, schedule_id, **fields) -> dict | None:
    """Merge semantics with an explicit-clear escape hatch, which is what a schedule needs:
    an omitted field keeps its stored value, and passing `None` explicitly CLEARS it (the
    `_UNSET` sentinel is what distinguishes the two). Without that distinction there would be
    no way to remove an end date once set — the classic PATCH trap.

    The merged result is re-validated as a whole, so a partial edit can never leave a rule the
    engine cannot expand."""
    row = _schedule_row(conn, user_id, schedule_id)
    if row is None:
        return None
    merged = {k: row[k] for k in row.keys() if k not in ("id", "user_id", "created_at")}
    merged.update({k: v for k, v in fields.items() if v is not _UNSET})
    vals = _validate_schedule_fields(conn, user_id, merged)
    conn.execute(
        f"UPDATE schedule SET {', '.join(f'{k} = ?' for k in vals)} WHERE id = ? AND user_id = ?",
        list(vals.values()) + [schedule_id, user_id])
    conn.commit()
    return _schedule_dict(_schedule_row(conn, user_id, schedule_id))


def delete_schedule(conn, user_id, schedule_id) -> bool:
    """Delete the rule. Transactions it already posted are KEPT — they are real money that
    really moved, and deleting a plan must never rewrite history. `schedule_txn` rows go with
    the schedule via ON DELETE CASCADE, which releases those dates, but the txns stay."""
    cur = conn.execute("DELETE FROM schedule WHERE id = ? AND user_id = ?", (schedule_id, user_id))
    conn.commit()
    return cur.rowcount > 0


def set_schedule_active(conn, user_id, schedule_id, active: bool) -> dict | None:
    row = _schedule_row(conn, user_id, schedule_id)
    if row is None:
        return None
    conn.execute("UPDATE schedule SET active = ? WHERE id = ? AND user_id = ?",
                 (1 if active else 0, schedule_id, user_id))
    conn.commit()
    return _schedule_dict(_schedule_row(conn, user_id, schedule_id))


# ----- occurrences -----

def _window_start(row, today: str, catchup_days=None) -> str:
    """The earliest date catch-up is willing to post for this schedule.

    The later of: the rule's anchor, the day the schedule was created, and the catch-up
    horizon. Without the created_at term, switching auto-post on for a bill anchored in 2000
    would post 25 years of rent in one click; without the horizon term, a long-dormant app
    would flood the ledger on the next open.
    """
    t = date.fromisoformat(today)
    horizon = _CATCHUP_DAYS if catchup_days is None else int(catchup_days)
    candidates = [date.fromisoformat(row["anchor_on"]), t - timedelta(days=horizon)]
    created = (row["created_at"] or "")[:10]
    try:
        # created_at is stamped in UTC; `today` is the caller's LOCAL date. The two disagree
        # for a few hours every evening, and an unclamped UTC date then sits in the caller's
        # future -- which pushed the window past today and silently dropped a just-created
        # schedule's own first occurrence. The earliest LOCAL day a schedule could have been
        # created is its UTC date minus one, so that is the honest floor; a day of slack is
        # nothing against the 60-day horizon, and it removes a whole class of timezone bugs
        # rather than the one that happened to surface. Also clamped to `today` so a
        # far-future created_at (clock skew, a bad restore) cannot freeze catch-up entirely.
        candidates.append(min(date.fromisoformat(created) - timedelta(days=1), t))
    except ValueError:
        pass
    return max(candidates).isoformat()


def _posted_dates(conn, schedule_id) -> set:
    return {r["occurrence_on"] for r in conn.execute(
        "SELECT occurrence_on FROM schedule_txn WHERE schedule_id = ?", (schedule_id,)).fetchall()}


def _due_for_row(conn, row, today: str, catchup_days=None) -> list[dict]:
    """Occurrences of one schedule that have come due and have not been posted."""
    hits = schedules.expand(schedules.Rule.from_row(row), _exceptions_for(conn, row["id"]),
                            _window_start(row, today, catchup_days), today)
    posted = _posted_dates(conn, row["id"])
    return [h for h in hits if h["on"].isoformat() not in posted]


def _post_occurrence(conn, user_id, row, occurrence_on: str, amount_cents: int, status: str) -> list[int]:
    """Write the transaction(s) for one occurrence and link the occurrence to them.

    Raw inserts and a single commit, mirroring `record_card_payment` — a transfer's two legs
    must land together or not at all, and `create_txn` commits per call.

    `external_id` is `sched:<id>:<date>`, which makes the existing `idx_txn_dedupe` partial
    unique index a second, database-level guarantee against double-posting, on top of
    `schedule_txn`'s primary key. No new column on `txn` was needed for either.
    """
    sid = row["id"]
    ext = f"sched:{sid}:{occurrence_on}"
    desc = row["description"] or row["name"]
    now = _now()
    ids: list[int] = []

    def _insert(account_id, direction, is_transfer, bucket, external_id):
        cur = conn.execute(
            """INSERT INTO txn (user_id, account_id, posted_on, direction, amount_cents, bucket,
                   category, description, is_transfer, transfer_group, source, external_id,
                   partner_owed_cents, status, kind, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, account_id, occurrence_on, direction, int(amount_cents), bucket,
             row["category"], desc, 1 if is_transfer else 0, tg, "schedule", external_id,
             0, status, "charge", now))
        return cur.lastrowid

    if row["direction"] == "transfer":
        tg = secrets.token_hex(16)
        # OUT of the source account first, so the returned primary leg is the one whose
        # account the schedule names.
        ids.append(_insert(row["account_id"], "out", True, None, ext))
        ids.append(_insert(row["to_account_id"], "in", True, None, ext + ":2"))
    else:
        tg = None
        ids.append(_insert(row["account_id"], row["direction"], False, row["bucket"], ext))

    conn.execute("INSERT INTO schedule_txn (schedule_id, occurrence_on, txn_id) VALUES (?,?,?)",
                 (sid, occurrence_on, ids[0]))
    conn.commit()
    return ids


def materialize_due_schedules(conn, user_id, today: str, catchup_days=None) -> dict:
    """Post every due occurrence of every AUTO-POST schedule, exactly once.

    Idempotent by construction, so the client may call it on every load. Returns a summary
    rather than raising when one schedule is unpostable: a single broken schedule (its account
    archived, say) must not stop the other nine from posting, and the surviving problem is
    reported so the UI can say so rather than failing silently.
    """
    posted, problems = [], []
    rows = conn.execute(
        "SELECT * FROM schedule WHERE user_id = ? AND active = 1 AND auto_post = 1",
        (user_id,)).fetchall()
    for row in rows:
        # An estimate never auto-posts however the flag is set: the amount is a guess, and
        # writing a guess into the ledger unasked is exactly what DEC-009 #3 forbids.
        if row["amount_is_estimate"]:
            continue
        if row["account_id"] is None:
            problems.append({"scheduleId": row["id"], "name": row["name"], "reason": "no account"})
            continue
        try:
            due = _due_for_row(conn, row, today, catchup_days)
        except ValueError as e:
            problems.append({"scheduleId": row["id"], "name": row["name"], "reason": str(e)})
            continue
        for hit in due:
            on = hit["on"].isoformat()
            amount = hit.get("amount_cents", row["amount_cents"])
            try:
                ids = _post_occurrence(conn, user_id, row, on, amount, "pending")
            except sqlite3.IntegrityError:
                conn.rollback()          # already posted by a concurrent call; the DB said so
                continue
            except sqlite3.Error as e:
                conn.rollback()
                problems.append({"scheduleId": row["id"], "name": row["name"], "reason": str(e)})
                break
            posted.append({"scheduleId": row["id"], "name": row["name"], "on": on, "txnIds": ids})
    return {"posted": posted, "problems": problems, "today": today}


def due_occurrences(conn, user_id, today: str, catchup_days=None) -> list[dict]:
    """What is waiting for a decision: due occurrences of schedules that do NOT auto-post
    (including every estimate). These are computed, never stored — nothing exists in the
    ledger until the user confirms it."""
    out = []
    rows = conn.execute(
        "SELECT * FROM schedule WHERE user_id = ? AND active = 1", (user_id,)).fetchall()
    for row in rows:
        if row["auto_post"] and not row["amount_is_estimate"]:
            continue
        try:
            due = _due_for_row(conn, row, today, catchup_days)
        except ValueError:
            continue
        for hit in due:
            out.append({
                "scheduleId": row["id"], "name": row["name"], "direction": row["direction"],
                "on": hit["on"].isoformat(),
                "amount": round(hit.get("amount_cents", row["amount_cents"]) / 100.0, 2),
                "amountIsEstimate": bool(row["amount_is_estimate"]),
                "accountId": row["account_id"], "toAccountId": row["to_account_id"],
                "bucket": row["bucket"], "category": row["category"],
            })
    out.sort(key=lambda o: (o["on"], o["name"]))
    return out


def confirm_occurrence(conn, user_id, schedule_id, occurrence_on: str, amount_cents=None) -> dict:
    """Post one occurrence now, at a possibly-corrected amount. This is the one-tap path out
    of the due tray, and the amount the user leaves in the field is the amount that lands —
    a confirmed transaction is `settled`, not `pending`, because the user is asserting it
    happened."""
    row = _schedule_row(conn, user_id, schedule_id)
    if row is None:
        raise ValueError("schedule not found")
    if row["account_id"] is None:
        raise ValueError("this schedule has no account to post to")
    if occurrence_on in _posted_dates(conn, schedule_id):
        raise ValueError(f"{occurrence_on} has already been posted")
    amount = row["amount_cents"] if amount_cents is None else int(amount_cents)
    if amount < 0:
        raise ValueError("amount_cents must be >= 0")
    ids = _post_occurrence(conn, user_id, row, occurrence_on, amount, "settled")
    return {"scheduleId": schedule_id, "on": occurrence_on, "txnIds": ids}


def skip_occurrence(conn, user_id, schedule_id, occurrence_on: str) -> dict:
    """Drop one occurrence without touching the rule or any other date."""
    return set_occurrence_exception(conn, user_id, schedule_id, occurrence_on, action="skip")


def set_occurrence_exception(conn, user_id, schedule_id, occurrence_on: str, *, action,
                             amount_cents=None, moved_to=None, description=None) -> dict:
    """Record a per-occurrence edit, keyed by the date the RULE produced.

    `occurrence_on` is the engine's `raw` date, not the shifted one — keying on where an
    occurrence lands would orphan every exception the moment weekend_shift changed.
    """
    if _schedule_row(conn, user_id, schedule_id) is None:
        raise ValueError("schedule not found")
    if action not in ("skip", "override"):
        raise ValueError(f"action must be skip|override, got {action!r}")
    conn.execute(
        """INSERT INTO schedule_exception (schedule_id, occurrence_on, action, amount_cents,
               moved_to, description, created_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(schedule_id, occurrence_on) DO UPDATE SET
               action = excluded.action, amount_cents = excluded.amount_cents,
               moved_to = excluded.moved_to, description = excluded.description""",
        (schedule_id, occurrence_on, action,
         None if amount_cents is None else int(amount_cents), moved_to, description, _now()))
    conn.commit()
    return {"scheduleId": schedule_id, "occurrenceOn": occurrence_on, "action": action}


def clear_occurrence_exception(conn, user_id, schedule_id, occurrence_on: str) -> bool:
    if _schedule_row(conn, user_id, schedule_id) is None:
        raise ValueError("schedule not found")
    cur = conn.execute("DELETE FROM schedule_exception WHERE schedule_id = ? AND occurrence_on = ?",
                       (schedule_id, occurrence_on))
    conn.commit()
    return cur.rowcount > 0


def split_schedule(conn, user_id, schedule_id, from_date: str, **changes) -> dict:
    """"Change this one and everything after it": end the current series the day before
    `from_date` and start a successor from it.

    A split, not an edit, because occurrences already posted must keep the terms they were
    posted under — the old rent really was $2,150 in March. `parent_id` keeps the lineage
    visible so the two read as one history rather than two unrelated schedules.
    """
    row = _schedule_row(conn, user_id, schedule_id)
    if row is None:
        raise ValueError("schedule not found")
    start = date.fromisoformat(from_date)
    anchor = date.fromisoformat(row["anchor_on"])
    if start <= anchor:
        raise ValueError("split date must be after the schedule's own start; edit the schedule instead")

    successor_fields = {k: row[k] for k in row.keys() if k not in ("id", "user_id", "created_at")}
    successor_fields.update({k: v for k, v in changes.items() if v is not _UNSET})
    successor_fields["anchor_on"] = from_date
    successor_fields["parent_id"] = schedule_id
    # The successor starts its own life: an "after N times" limit counted from the ORIGINAL
    # anchor would be meaningless here, and silently carrying it would end the new series early.
    if successor_fields.get("end_mode") == "after":
        successor_fields["end_mode"] = "never"
        successor_fields["end_count"] = None

    successor = create_schedule(conn, user_id, **successor_fields)
    conn.execute("UPDATE schedule SET end_mode = 'on', ends_on = ?, end_count = NULL "
                 "WHERE id = ? AND user_id = ?",
                 ((start - timedelta(days=1)).isoformat(), schedule_id, user_id))
    conn.commit()
    return {"ended": _schedule_dict(_schedule_row(conn, user_id, schedule_id)), "successor": successor}


def schedule_occurrences(conn, user_id, schedule_id, start: str, end: str, cap: int = 400) -> list[dict]:
    """The computed forward list for one schedule, exceptions applied. Never stored."""
    row = _schedule_row(conn, user_id, schedule_id)
    if row is None:
        raise ValueError("schedule not found")
    posted = _posted_dates(conn, schedule_id)
    out = []
    for h in schedules.expand(schedules.Rule.from_row(row), _exceptions_for(conn, schedule_id), start, end, cap):
        on = h["on"].isoformat()
        out.append({"on": on, "raw": h["raw"].isoformat(), "posted": on in posted,
                    "amount": round(h.get("amount_cents", row["amount_cents"]) / 100.0, 2),
                    "overridden": bool(h.get("overridden"))})
    return out


# ---------- scenarios (TODO-219, DEC-017) ----------

class ScenarioConflictError(Exception):
    """Raised when an operation collides with the one-active-scenario invariant
    (activate while another is active, edit/delete an active scenario, revert a
    draft); maps to HTTP 409. The caller must revert first (DEC-017 #5)."""


def _scenario_dict(r, include_payload=True) -> dict:
    d = {"id": r["id"], "name": r["name"], "status": r["status"],
         "createdAt": r["created_at"], "updatedAt": r["updated_at"],
         "activatedAt": r["activated_at"]}
    if include_payload:
        d["payload"] = json.loads(r["payload_json"])
    return d


def create_scenario(conn, user_id, name, spec: dict) -> dict:
    """New draft scenario. `spec` is the client-authored what-if definition
    (comp, activationMonth, payFreq, budgetPlan, catchup) — stored opaque
    (DEC-017 #3); the server never derives budget figures from it."""
    if not (name or "").strip():
        raise ValueError("scenario name is required")
    if not isinstance(spec, dict):
        raise ValueError(f"spec must be an object, got {type(spec).__name__}")
    payload = {"payloadVersion": 1, "spec": spec, "revert": None}
    now = _now()
    cur = conn.execute(
        "INSERT INTO scenario (user_id, name, status, payload_json, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (user_id, name.strip(), "draft", json.dumps(payload), now, now))
    conn.commit()
    return get_scenario(conn, user_id, cur.lastrowid)


def get_scenario(conn, user_id, scenario_id) -> dict | None:
    r = conn.execute(
        "SELECT * FROM scenario WHERE id = ? AND user_id = ?", (scenario_id, user_id)).fetchone()
    return _scenario_dict(r) if r else None


def list_scenarios(conn, user_id) -> list[dict]:
    """Summaries only — no payload parse (the list view doesn't need the blob).
    The active scenario (at most one, per user) always sorts first."""
    return [_scenario_dict(r, include_payload=False) for r in conn.execute(
        "SELECT * FROM scenario WHERE user_id = ? ORDER BY (status = 'active') DESC, updated_at DESC",
        (user_id,)).fetchall()]


def update_scenario(conn, user_id, scenario_id, *, name=None, spec=None) -> dict | None:
    """Rename and/or replace the draft's spec. An ACTIVE scenario is immutable
    (409 — revert first) so the installed plans always match its spec (DEC-017)."""
    r = conn.execute(
        "SELECT * FROM scenario WHERE id = ? AND user_id = ?", (scenario_id, user_id)).fetchone()
    if not r:
        return None
    if r["status"] == "active":
        raise ScenarioConflictError("scenario is active; revert it before editing")
    if name is not None and not str(name).strip():
        raise ValueError("scenario name must not be empty")
    if spec is not None and not isinstance(spec, dict):
        raise ValueError(f"spec must be an object, got {type(spec).__name__}")
    payload = json.loads(r["payload_json"])
    if spec is not None:
        payload["spec"] = spec
    conn.execute(
        "UPDATE scenario SET name = ?, payload_json = ?, updated_at = ? WHERE id = ? AND user_id = ?",
        (str(name).strip() if name is not None else r["name"], json.dumps(payload), _now(),
         scenario_id, user_id))
    conn.commit()
    return get_scenario(conn, user_id, scenario_id)


def delete_scenario(conn, user_id, scenario_id) -> bool:
    """Delete a draft. An ACTIVE scenario can't be deleted (409 — revert first):
    its revert bookkeeping is the only path back to the pre-activation plans."""
    r = conn.execute(
        "SELECT status FROM scenario WHERE id = ? AND user_id = ?", (scenario_id, user_id)).fetchone()
    if not r:
        return False
    if r["status"] == "active":
        raise ScenarioConflictError("scenario is active; revert it before deleting")
    conn.execute("DELETE FROM scenario WHERE id = ? AND user_id = ?", (scenario_id, user_id))
    conn.commit()
    return True


def _valid_month(m) -> bool:
    return (isinstance(m, str) and len(m) == 7 and m[4] == "-"
            and m[:4].isdigit() and m[5:].isdigit() and 1 <= int(m[5:]) <= 12)


def activate_scenario(conn, user_id, scenario_id, activation_month: str, plan_months: list[dict],
                      client_state=None) -> dict | None:
    """Install the scenario as the live plan from `activation_month` (M) onward —
    ONE transaction (DEC-017 #5).

    `plan_months` entries are PlanLockModel-shaped dicts plus a `month` key, every
    month ≥ M (the client supplies the same derived figures it posts to
    /plan/{month}/lock). For each month: snapshot the prior plan_snapshot into the
    scenario's revert bookkeeping (existed / tombstone), then build_plan + upsert
    through the same machinery as the lock endpoint. Months < M are never read or
    written (DEC-007). Raises ScenarioConflictError when any scenario is already
    active FOR THIS USER (the partial unique index is now per-user — `UNIQUE(user_id,
    status) WHERE status='active'` — so concurrent activation across DIFFERENT users
    never collides), ValueError on bad input. Returns None for an unknown id (or one
    belonging to another user — scoped lookup, never leaks existence)."""
    r = conn.execute(
        "SELECT * FROM scenario WHERE id = ? AND user_id = ?", (scenario_id, user_id)).fetchone()
    if not r:
        return None
    if r["status"] == "active":
        raise ScenarioConflictError("scenario is already active")
    other = conn.execute(
        "SELECT id, name FROM scenario WHERE status = 'active' AND user_id = ? AND id != ?",
        (user_id, scenario_id)).fetchone()
    if other:
        raise ScenarioConflictError(
            f"scenario {other['id']} ({other['name']!r}) is active; revert it first")
    if not _valid_month(activation_month):
        raise ValueError(f"activationMonth must be 'YYYY-MM', got {activation_month!r}")
    if not plan_months:
        raise ValueError("planMonths must contain at least the activation month")
    seen = set()
    for pm in plan_months:
        month = pm.get("month")
        if not _valid_month(month):
            raise ValueError(f"planMonths[].month must be 'YYYY-MM', got {month!r}")
        if month < activation_month:
            raise ValueError(f"planMonths month {month} is before activation month {activation_month}")
        if month in seen:
            raise ValueError(f"duplicate planMonths month {month}")
        seen.add(month)
    if activation_month not in seen:
        raise ValueError(
            f"planMonths must include the activation month {activation_month} itself — "
            "activating 'from M' with no plan for M would leave the old M plan live")

    snapshots, overwrote, created = [], 0, 0
    try:
        for pm in sorted(plan_months, key=lambda p: p["month"]):
            month = pm["month"]
            prior = get_plan(conn, user_id, month)
            if prior:
                snapshots.append({"month": month, "existed": True, "status": prior["status"],
                                  "engineVersion": prior["engineVersion"], "payload": prior["payload"],
                                  "createdAt": prior["createdAt"], "lockedAt": prior["lockedAt"]})
                overwrote += 1
            else:
                snapshots.append({"month": month, "existed": False})
                created += 1
            payload = tracking.build_plan(
                month,
                bucket_planned=pm.get("bucketPlanned") or {},
                income_planned=pm.get("incomePlanned") or 0.0,
                savings_rate_planned=pm.get("savingsRatePlanned") or 0.0,
                forecast_cone=pm.get("forecastCone") or [],
                anchor_date=pm.get("anchorDate") or "",
                anchor_value=pm.get("anchorValue") or 0.0,
                engine_version=pm.get("engineVersion") or "1.0",
            )
            # Record what we're installing so revert can detect (and preserve) any edits
            # the user makes to these months while the scenario is active (data-safety).
            snapshots[-1]["installed"] = payload
            snapshots[-1]["installedStatus"] = pm.get("status") or "locked"
            _save_plan_row(conn, user_id, month, payload, status=pm.get("status") or "locked",
                           engine_version=pm.get("engineVersion") or "1.0")
        now = _now()
        body = json.loads(r["payload_json"])
        body["revert"] = {"activatedAt": now, "activationMonth": activation_month,
                          "planSnapshots": snapshots, "clientState": client_state}
        conn.execute(
            "UPDATE scenario SET status = 'active', payload_json = ?, updated_at = ?, activated_at = ? "
            "WHERE id = ? AND user_id = ?",
            (json.dumps(body), now, now, scenario_id, user_id))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()  # the partial unique index caught a concurrent activate
        raise ScenarioConflictError("another scenario was activated concurrently; revert it first")
    except Exception:
        conn.rollback()
        raise
    return {"scenario": get_scenario(conn, user_id, scenario_id),
            "summary": {"monthsWritten": len(snapshots), "monthsOverwritten": overwrote,
                        "monthsCreated": created}}


def revert_scenario(conn, user_id, scenario_id) -> dict | None:
    """Exactly undo activate — ONE transaction. Restore every captured plan_snapshot
    (re-save the prior payload with its prior status/lock timestamp) and delete the
    plan rows activation created where none existed. Data-safety guard: a month the
    user edited AFTER activation (e.g. a real month-close lock while the scenario was
    live) no longer matches what activation installed — revert KEEPS the user's
    version and reports it as "kept-user-edit" instead of silently clobbering it.
    Flips the scenario back to draft and returns the opaque clientState so the client
    can restore its own budget config + Tax inputs (DEC-017 #6). Returns None for an
    unknown id (or one belonging to another user)."""
    r = conn.execute(
        "SELECT * FROM scenario WHERE id = ? AND user_id = ?", (scenario_id, user_id)).fetchone()
    if not r:
        return None
    if r["status"] != "active":
        raise ScenarioConflictError("scenario is not active; nothing to revert")
    body = json.loads(r["payload_json"])
    revert = body.get("revert") or {}
    restored: dict = {}
    try:
        for snap in revert.get("planSnapshots") or []:
            month = snap["month"]
            cur = get_plan(conn, user_id, month)
            installed = snap.get("installed")
            if (cur is not None and installed is not None
                    and (cur["payload"] != installed
                         or cur["status"] != (snap.get("installedStatus") or "locked"))):
                restored[month] = "kept-user-edit"   # changed since activation — theirs wins
                continue
            if snap.get("existed"):
                _save_plan_row(conn, user_id, month, snap["payload"], status=snap.get("status") or "locked",
                               engine_version=snap.get("engineVersion") or "1.0",
                               locked_at=snap.get("lockedAt"))
                restored[month] = "restored"
            else:
                conn.execute("DELETE FROM plan_snapshot WHERE month = ? AND user_id = ?", (month, user_id))
                restored[month] = "deleted"
        body["revert"] = None
        conn.execute(
            "UPDATE scenario SET status = 'draft', payload_json = ?, updated_at = ?, activated_at = NULL "
            "WHERE id = ? AND user_id = ?",
            (json.dumps(body), _now(), scenario_id, user_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    # materializedIds is read from `revert` (the pre-wipe local var) BEFORE the UPDATE
    # above set body["revert"] = None -- handed back so the client can act on the exact
    # ids in the SAME round trip that confirms the plan-baseline revert committed,
    # instead of a separate GET that would race a concurrent second write.
    return {"scenario": get_scenario(conn, user_id, scenario_id), "restored": restored,
            "clientState": revert.get("clientState"), "materializedIds": revert.get("materializedIds")}


def record_scenario_materialization(conn, user_id, scenario_id, goal_ids: list[int],
                                    fund_ids: list[int]) -> dict | None:
    """TODO-243 Phase 2 code-review BLOCKER fix (2026-07-28): records EXACTLY which real
    goal/fund rows a What-If activation materialized, so revert acts ONLY on these ids —
    never re-derives them by name. Name-matching was confirmed catastrophic in review: a
    pre-existing real "Vacation" goal sharing a draft's name got archived/deleted on
    revert even though materialization correctly SKIPPED creating it (idempotency), and a
    second scenario's revert could hard-delete a fund a completely different, earlier
    scenario had materialized. Recording the real ids at materialize-time makes the
    colliding pre-existing row (or another scenario's row) structurally unreachable by
    revert — it was never IN the recorded set to begin with.

    Allowed ONLY while `status == 'active'` — the one window materialization itself runs
    in (strictly after activate, §3.5 step 4) — and deliberately bypasses
    update_scenario's active-guard: that guard protects `spec` (the plan-baseline
    contract callers must not silently drift out from under an installed plan); this
    touches only `revert` bookkeeping, scoped to the CURRENT activation, which is exactly
    what must stay writable while active (activate_scenario itself writes `revert` at the
    same status). Idempotent: REPLACES the recorded set (not append/union) — a Retry
    after a partial-materialization failure (§3.6) posts the full up-to-date id list each
    time, so retrying twice is a no-op, never a duplicate-record bug. Returns None for an
    unknown id (or one belonging to another user — scoped lookup, never leaks existence)."""
    r = conn.execute(
        "SELECT * FROM scenario WHERE id = ? AND user_id = ?", (scenario_id, user_id)).fetchone()
    if not r:
        return None
    if r["status"] != "active":
        raise ScenarioConflictError("scenario is not active; nothing to record materialization for")
    if not isinstance(goal_ids, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in goal_ids):
        raise ValueError("goalIds must be a list of integers")
    if not isinstance(fund_ids, list) or not all(isinstance(x, int) and not isinstance(x, bool) for x in fund_ids):
        raise ValueError("fundIds must be a list of integers")
    body = json.loads(r["payload_json"])
    revert = body.get("revert") or {}
    revert["materializedIds"] = {"goals": goal_ids, "funds": fund_ids}
    body["revert"] = revert
    conn.execute(
        "UPDATE scenario SET payload_json = ?, updated_at = ? WHERE id = ? AND user_id = ?",
        (json.dumps(body), _now(), scenario_id, user_id))
    conn.commit()
    return get_scenario(conn, user_id, scenario_id)


# ---------- backup / restore ----------

def _main_db_file(conn: sqlite3.Connection) -> str:
    """Return the filesystem path of the main database attachment, or '' for :memory:."""
    for row in conn.execute("PRAGMA database_list").fetchall():
        if row[1] == "main":
            return row[2]
    return ""


# ---------- per-user server profile (S1.2, DEC-027/DEC-035, docs/s1_2-migration-design.md) ----------

def get_profile(conn: sqlite3.Connection, user_id: str) -> dict:
    """§3.1: read this user's profile row. Returns the empty shape when no row exists
    for *user_id* -- identical code path for owner and member; the scopeId (PK) is the
    only thing that differs. A foreign scope with no row of its own gets this exact same
    shape, never a distinguishable 403/leak (design §3.1's no-existence-leak guarantee)."""
    r = conn.execute(
        "SELECT blob, state_version, updated_at FROM user_profile WHERE user_id = ?",
        (user_id,),
    ).fetchone()
    if r is None:
        return {"hasState": False, "stateVersion": 0, "blob": None, "updatedAt": None}
    return {
        "hasState": True,
        "stateVersion": r["state_version"],
        "blob": json.loads(r["blob"]),
        "updatedAt": r["updated_at"],
    }


def put_profile(conn: sqlite3.Connection, user_id: str, blob: dict, base_state_version=None,
                 is_migration: bool = False) -> dict:
    """§3.2: last-write-wins upsert of this user's profile blob. The PUT ALWAYS wins —
    v1 has no rejection-on-conflict; `base_state_version` is advisory only (accepted,
    never persisted, never used to refuse a stale write — a future v2 409 is an open
    question, DEC-027). The displaced blob (if any) moves to `prev_blob`/
    `prev_state_version` — exactly ONE level of undo, not a history log.

    Implemented as a single atomic `INSERT ... ON CONFLICT ... DO UPDATE ... RETURNING`
    so a fresh row (state_version -> 1) and a displacing update (state_version -> N+1,
    prev_* <- the row this write displaced) are the SAME statement under the SAME write
    lock — no read-then-write race window, so two near-simultaneous PUTs for the same
    scope serialize cleanly (SQLite's writer lock + `busy_timeout`) and the loser's
    write is never lost to a stale in-Python read of the prior row (§7's "no torn blob"
    / simultaneous-PUT guarantee).

    §5.2 pre-migration server snapshot: on the FIRST `is_migration=True` PUT for a scope
    that has no row yet, take a one-time `.pre-profile-migration.bak` online-backup copy
    of the WHOLE db file BEFORE the upsert — mirrors `init_db`'s `.pre-multiuser.bak` and
    `import_all`'s `.pre-import-<ts>.bak` (DEC-016 OSError-before-mutation posture). An
    `OSError` from `conn.backup()` propagates here, before any mutation. Guarded by
    `os.path.exists(bak)` (same pattern as `init_db`) so a retried migration PUT never
    overwrites the TRUE pre-migration snapshot.
    """
    if is_migration:
        exists = conn.execute(
            "SELECT 1 FROM user_profile WHERE user_id = ?", (user_id,)
        ).fetchone()
        if exists is None:
            db_file = _main_db_file(conn)
            if db_file:                                    # '' for :memory: -- nothing to copy
                bak = f"{db_file}.pre-profile-migration.bak"
                if not os.path.exists(bak):                # preserve the TRUE first snapshot on re-runs
                    with closing(sqlite3.connect(bak)) as dest:
                        conn.backup(dest)                   # OSError propagates -> abort before any mutation

    # TODO-241 (pre-R1 hardening): capture the row's state_version BEFORE the upsert so the
    # response can tell the client whether this write displaced a DIFFERENT lineage than the
    # one it was based on -- distinct from an ordinary same-device re-flush (D1: base_state_
    # version == the prior state_version). `prior_state_version > base_state_version` is the
    # precise predicate: it's true exactly when the blob this PUT just overwrote had already
    # moved past what the caller last synced from -- another device's flush, or a restore's
    # reload-door FLUSH landing on an already-migrated-elsewhere server (D2/X1, §4/§7 of
    # docs/s1_2-migration-design.md). It is deliberately NOT true for S3 (local clean but
    # newer than a rolled-back/restored-older server, base_state_version > prior_state_version)
    # -- that flush re-asserts a NEWER local over an OLDER server, the opposite of what the
    # TODO-241 banner ("replaced a newer server version") describes, so it must not fire there.
    prior = conn.execute(
        "SELECT state_version, updated_at FROM user_profile WHERE user_id = ?", (user_id,)
    ).fetchone()
    prior_state_version = prior["state_version"] if prior is not None else None
    prior_updated_at = prior["updated_at"] if prior is not None else None

    now = _now()
    blob_json = json.dumps(blob)
    row = conn.execute(
        """
        INSERT INTO user_profile (user_id, blob, state_version, updated_at, prev_blob, prev_state_version, created_at)
        VALUES (?, ?, 1, ?, NULL, NULL, ?)
        ON CONFLICT(user_id) DO UPDATE SET
          prev_blob          = user_profile.blob,
          prev_state_version = user_profile.state_version,
          blob                = excluded.blob,
          state_version       = user_profile.state_version + 1,
          updated_at          = excluded.updated_at
        RETURNING state_version, updated_at
        """,
        (user_id, blob_json, now, now),
    ).fetchone()
    conn.commit()

    base = base_state_version if base_state_version is not None else 0
    displaced = prior_state_version is not None and prior_state_version > base
    return {
        "stateVersion": row["state_version"],
        "updatedAt": row["updated_at"],
        # Additive-only (TODO-241): existing clients ignore unknown fields; new ones use
        # `displaced` to surface the non-transient "your plan replaced a newer server version"
        # banner. `prevStateVersion`/`prevUpdatedAt` describe exactly what got displaced (the
        # same content now sitting in `prev_blob`/`prev_state_version`, one-level recoverable).
        "displaced": displaced,
        "prevStateVersion": prior_state_version,
        "prevUpdatedAt": prior_updated_at,
    }


def _validate_backup(payload: dict, current: int) -> None:
    """Pure validation — raises RestoreError on any structural or version problem; writes nothing."""
    if payload.get("app") != _BACKUP_APP_TAG and payload.get("app") not in _BACKUP_LEGACY_APP_TAGS:
        raise RestoreError(
            f"app tag mismatch: expected {_BACKUP_APP_TAG!r}, got {payload.get('app')!r}"
        )
    sv = payload.get("schemaVersion")
    if isinstance(sv, bool) or not isinstance(sv, int):
        raise RestoreError(f"schemaVersion must be an integer, got {type(sv).__name__}")
    if sv > current:
        raise RestoreError(
            f"backup is from a newer app version (schemaVersion {sv} > current {current}); "
            "upgrade the app first"
        )
    # S1.1: the envelope's `scope` marks whether a backup carries one user's data or the
    # whole household. Only "household-full" (or the field's historical absence, pre-S1.1)
    # is accepted here — the DEC-028 per-user "user" scope slice is a deferred follow-up
    # (docs/multiuser-household-plan.md S1.1 §4c) with no restore path yet.
    scope = payload.get("scope")
    if scope is not None and scope != "household-full":
        raise RestoreError(
            f"unsupported backup scope {scope!r}; this app version can only restore a "
            "'household-full' backup"
        )
    tables = payload.get("tables")
    if not isinstance(tables, dict):
        raise RestoreError(f"tables must be a dict, got {type(tables).__name__}")
    for tbl, _cols in _BACKUP_TABLES:
        if tbl not in tables:
            if tbl in _BACKUP_OPTIONAL_TABLES:
                continue  # a pre-scenario backup legitimately has no scenario table
            raise RestoreError(f"backup is missing required table {tbl!r}")
        rows = tables[tbl]
        if not isinstance(rows, list):
            raise RestoreError(f"tables[{tbl!r}] must be a list, got {type(rows).__name__}")
        for i, row in enumerate(rows):
            if not isinstance(row, dict):
                raise RestoreError(f"tables[{tbl!r}][{i}] must be a dict")


def export_all(conn: sqlite3.Connection, exported_at: str | None = None) -> dict:
    """Pure read, no commit. Export all data as raw DB values (integer cents, not dollars).

    Table and column identifiers come exclusively from _BACKUP_TABLES — never from the DB
    schema at runtime, so the allow-list is always the single source of truth.

    ALWAYS whole-household (S1.1): this is the owner-only full-DB backup, unchanged in
    scope, but now explicitly labeled `"scope": "household-full"` since every user-owned
    table carries `user_id` — the export contains every household member's rows, not just
    the owner's. `userCount` is a human sanity-check (distinct user_ids across the
    user-owned tables), not used by import_all.
    """
    if exported_at is None:
        exported_at = _now()
    schema_version = conn.execute("PRAGMA user_version").fetchone()[0]
    tables: dict = {}
    for tbl, cols in _BACKUP_TABLES:
        if tbl == "txn_tag":
            order_by = "txn_id, tag_id"
        elif tbl == "fund_txn":
            order_by = "fund_id, txn_id"        # PK is txn_id alone, but this reads better -- no surrogate id column
        elif tbl == "user_profile":
            order_by = "user_id"                # PK is user_id, not id -- no surrogate id column
        elif tbl == "household_budget_share":
            order_by = "line_id, user_id"       # PK is (line_id, user_id) -- no surrogate id column
        elif tbl == "schedule_txn":
            order_by = "schedule_id, occurrence_on"  # PK is the pair -- no surrogate id column
        else:
            order_by = "id"
        rows = conn.execute(f"SELECT * FROM {tbl} ORDER BY {order_by}").fetchall()
        if rows:
            available = set(rows[0].keys())
            emit_cols = [c for c in cols if c in available]
            tables[tbl] = [{c: row[c] for c in emit_cols} for row in rows]
        else:
            tables[tbl] = []
    user_scoped_tables = [tbl for tbl, cols in _BACKUP_TABLES if "user_id" in cols]
    user_count_sql = " UNION ".join(f"SELECT user_id FROM {tbl}" for tbl in user_scoped_tables)
    user_count = conn.execute(f"SELECT COUNT(*) FROM ({user_count_sql})").fetchone()[0]
    return {
        "app": _BACKUP_APP_TAG,
        "schemaVersion": schema_version,
        "scope": "household-full",
        "userCount": user_count,
        "exportedAt": exported_at,
        "tables": tables,
    }


_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _csv_safe(text: str) -> str:
    """Prefix *text* with a leading apostrophe when it starts with a formula-trigger
    character (=, +, -, @), so opening the export in Excel/Sheets/Numbers never executes
    a formula built from user-controlled data (description, category, account, tag names).
    Spreadsheet apps render the leading apostrophe as a plain-text marker, not a visible
    character, so the cell still reads correctly to a human."""
    if text and text[0] in _CSV_FORMULA_PREFIXES:
        return "'" + text
    return text


def export_txns_csv(conn: sqlite3.Connection, user_id, date_from: str | None = None, date_to: str | None = None) -> str:
    """Build a date-ranged CSV of transactions for analysis / tax-prep (read-only, no writes).

    Deliberately NOT a backup: no app tag, no schemaVersion, never accepted by import_all —
    this is a plain spreadsheet export a human opens in Excel/Sheets/Numbers.

    Owner-only endpoint (S1.1 §3b) — scoped to the CALLER's own transactions (their
    `scopeId`, always `__owner__` since only the owner may call this), never the whole
    household's. This is the owner's personal tax-prep export, not a household dump.

    *date_from* / *date_to* are inclusive ISO ``YYYY-MM-DD`` bounds on ``txn.posted_on``;
    either or both may be ``None`` for an unbounded side. Raises ``ValueError`` (surfaced by
    the caller as an HTTP 422) when a bound is not a valid ISO date, or when
    ``date_from > date_to``.

    One row per transaction, ordered by ``posted_on`` then ``id``. Columns: posted_on,
    account, direction, amount (dollars, 2 decimals — the DB stores integer cents),
    bucket, category, description, tags (``|``-joined tag names), status, kind,
    is_transfer, splits (empty, or ``bucket:category:amount`` legs ``|``-joined when the
    transaction has txn_split rows). account/tag names come from joins, never raw ids.
    Every user-controlled text cell (description, category, account, tag names, and the
    splits cell) is passed through `_csv_safe` to defuse CSV formula injection.
    """
    for label, value in (("from", date_from), ("to", date_to)):
        if value is not None:
            try:
                datetime.strptime(value, "%Y-%m-%d")
            except ValueError:
                raise ValueError(f"{label!r} must be a valid ISO date (YYYY-MM-DD), got {value!r}")
    if date_from is not None and date_to is not None and date_from > date_to:
        raise ValueError(f"from ({date_from!r}) must not be after to ({date_to!r})")

    sql = "SELECT t.*, a.name AS account_name FROM txn t JOIN account a ON a.id = t.account_id"
    where, vals = ["t.user_id = ?"], [user_id]
    if date_from is not None:
        where.append("t.posted_on >= ?"); vals.append(date_from)
    if date_to is not None:
        where.append("t.posted_on <= ?"); vals.append(date_to)
    sql += " WHERE " + " AND ".join(where) + " ORDER BY t.posted_on, t.id"
    rows = conn.execute(sql, vals).fetchall()

    ids = [r["id"] for r in rows]
    tagmap: dict = {}
    splitmap: dict = {}
    if ids:                                                  # batch-attach tags + splits (no N+1)
        ph = ",".join("?" * len(ids))
        for r in conn.execute(
            "SELECT jt.txn_id, tg.name FROM txn_tag jt JOIN tag tg ON tg.id = jt.tag_id "
            f"WHERE jt.txn_id IN ({ph}) ORDER BY tg.name", ids).fetchall():
            tagmap.setdefault(r["txn_id"], []).append(r["name"])
        for r in conn.execute(
            f"SELECT txn_id, bucket, category, amount_cents FROM txn_split WHERE txn_id IN ({ph}) ORDER BY id",
            ids).fetchall():
            splitmap.setdefault(r["txn_id"], []).append(r)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow([
        "posted_on", "account", "direction", "amount", "bucket", "category",
        "description", "tags", "status", "kind", "is_transfer", "splits",
    ])
    for r in rows:
        tags = "|".join(tagmap.get(r["id"], []))
        splits = "|".join(
            f"{s['bucket'] or ''}:{s['category'] or ''}:{s['amount_cents'] / 100:.2f}"
            for s in splitmap.get(r["id"], [])
        )
        writer.writerow([
            r["posted_on"],
            _csv_safe(r["account_name"]),
            r["direction"],
            f"{r['amount_cents'] / 100:.2f}",
            _csv_safe(r["bucket"] or ""),
            _csv_safe(r["category"] or ""),
            _csv_safe(r["description"] or ""),
            _csv_safe(tags),
            r["status"],
            r["kind"],
            "true" if r["is_transfer"] else "false",
            _csv_safe(splits),
        ])
    return buf.getvalue()


def _prune_pre_import_backups(db_file: str) -> None:
    """Delete all but the newest `MAX_PRE_IMPORT_BACKUPS` `.pre-import-*.bak` safety copies
    next to *db_file* (SEC-003). Only ever called AFTER an import has committed successfully
    — never on a failed/rolled-back import, so a safety copy is never removed before it might
    still be needed to recover from a bad restore.

    Sorts by the `<ts>` (`%Y%m%dT%H%M%SZ`, lexically = chronologically) embedded in the
    filename rather than mtime, so pruning is deterministic even if file timestamps are
    coarse or clock-skewed. Best-effort: any OSError (listdir or unlink) is swallowed —
    a failed prune must never fail the import it follows.
    """
    try:
        directory = os.path.dirname(db_file) or "."
        base = os.path.basename(db_file)
        pattern = re.compile(re.escape(base) + r"\.pre-import-(\d{8}T\d{6}Z)\.bak$")
        candidates = []
        for name in os.listdir(directory):
            m = pattern.match(name)
            if m:
                candidates.append((m.group(1), name))
        candidates.sort(key=lambda pair: pair[0])   # oldest first
        excess = candidates[:-MAX_PRE_IMPORT_BACKUPS] if len(candidates) > MAX_PRE_IMPORT_BACKUPS else []
        for _, name in excess:
            try:
                os.remove(os.path.join(directory, name))
            except OSError:
                pass   # best-effort — a failed unlink must not fail the already-successful import
    except OSError:
        pass


def import_all(conn: sqlite3.Connection, payload: dict) -> dict:
    """Atomically replace all tracked data with a backup payload.

    Phases:
      (a) Validate payload — raises RestoreError (→ HTTP 422) before any mutation or safety copy.
      (b) Safety-copy the live DB file via the sqlite3 online-backup API (WAL-consistent).
          Skipped for :memory: connections.  OSError propagates before any mutation.
      (c) Single atomic transaction with FK enforcement suspended:
            isolation_level=None (load-bearing: PRAGMA foreign_keys is ignored inside an
            implicit transaction), PRAGMA foreign_keys=OFF, BEGIN, capture each existing
            `user_profile` row's state_version (pre-R1 hardening — see below), DELETE all
            tables in reversed(_BACKUP_TABLES) order (child→parent), INSERT all tables in
            forward order (parent→child) using allow-list columns only, PRAGMA
            foreign_key_check, PRAGMA integrity_check, PRAGMA user_version=<N>, COMMIT.
          Any exception triggers ROLLBACK; FK ON and prior isolation_level restored in finally.
      (d) If the imported schemaVersion < the pre-import version, run init_db to apply
          any pending migrations and advance user_version to the current app schema.
      (e) On success only, prune old `.pre-import-*.bak` safety copies down to
          `MAX_PRE_IMPORT_BACKUPS` (SEC-003).

    PRE-R1 HARDENING — `user_profile` restore is a DISPLACING PUT, never a verbatim write:
    every other table is restored byte-for-byte (that IS the restore contract — REQ-1/REQ-2).
    `user_profile` is the one exception, and deliberately so: `state_version` is a
    SERVER-AUTHORITATIVE sync counter the client boot logic (docs/s1_2-migration-design.md
    §4) trusts to be monotonically non-decreasing. Writing a backup's `state_version` verbatim
    can REWIND it (a backup taken at v2 restored over a live v3 row), and a rewound version is
    exactly what let a stale restored blob hydrate back down over newer local data on next
    boot (the incident this hardening closes — see docs/multiuser-household-plan.md's pre-R1
    guard ledger row and LESSONS-LEARNED.md "A restored backup file is a second device with
    stale data"). Fix: for each restored `user_profile` row, treat the restore exactly like a
    `put_profile` PUT whose "current" row is whatever existed for that scope immediately
    before this restore (captured before the DELETE below) — `new_state_version =
    max(existing_state_version, payload_state_version) + 1` (never verbatim; a client meta
    that had already learned a high version — either the live pre-restore row's version, or
    the version the payload itself carries — must still see the server strictly advance,
    never regress or tie), `prev_blob`/`prev_state_version` = the row this restore displaced
    (one level of undo, same as an ordinary PUT — recorded from the LIVE pre-restore row, not
    from the payload's own prev_blob, which the backup contract excludes, see `_BACKUP_TABLES`
    above), `updated_at` = now. A restore into a scope with no existing row still versions as
    `max(0, payload_state_version) + 1` rather than the payload's raw version, so a client
    whose meta synced with the pre-backup lineage can never see the server as
    older-or-equal. The restored blob DOES still land as the live blob (disaster-recovery
    intent preserved, DEC-016/§1.3) — only the version discipline changes; this is why
    `test_household_full_restore_replace_writes_profile_blobs` and
    `test_backup_round_trip_profile_survives_verbatim` in tests/test_profile_store.py were
    updated alongside this fix (they asserted the old verbatim-version behavior, which was the
    bug). Duplicate `user_id` rows in a payload (malformed backup): the second row hits the
    `user_id` PRIMARY KEY -> `RestoreError` -> the ENTIRE restore rolls back (fail-closed,
    live row intact) — deliberately kept as reject-and-rollback rather than last-write-wins,
    same posture as a duplicate surrogate `id` in any other table.
    """
    # (a) validate first — no mutation and no safety copy on a bad payload
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    _validate_backup(payload, current)

    # (b) safety copy before ANY write
    db_file = _main_db_file(conn)
    safety_path: str | None = None
    if db_file:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safety_path = f"{db_file}.pre-import-{ts}.bak"
        with closing(sqlite3.connect(safety_path)) as dest:
            conn.backup(dest)  # WAL-consistent online backup; OSError propagates before mutation

    # (c) atomic replace
    schema_version: int = payload["schemaVersion"]
    src_tables: dict = payload["tables"]
    restored: dict[str, int] = {}
    prior_isolation = conn.isolation_level
    conn.isolation_level = None   # autocommit: PRAGMA foreign_keys takes effect immediately
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("BEGIN")
        try:
            # Pre-R1 hardening: capture each existing user_profile row's version/blob BEFORE
            # the DELETE below wipes it — this is the "current row" a restore must displace
            # exactly like an ordinary put_profile PUT would (see the docstring above).
            existing_profiles: dict = {
                r["user_id"]: {"blob": r["blob"], "state_version": r["state_version"]}
                for r in conn.execute("SELECT user_id, blob, state_version FROM user_profile").fetchall()
            }
            restore_now = _now()

            # DELETE child→parent (FK is OFF, but ordering is still correct practice)
            for tbl, _cols in reversed(_BACKUP_TABLES):
                conn.execute(f"DELETE FROM {tbl}")
            # INSERT parent→child using only allow-list columns
            for tbl, cols in _BACKUP_TABLES:
                count = 0
                for row in src_tables.get(tbl, []):
                    if tbl == "user_profile":
                        # Displacing-PUT semantics, not a verbatim write — see the docstring.
                        if "user_id" not in row or "blob" not in row:
                            raise RestoreError(f"row in {tbl!r} has no recognised columns")
                        user_id = row["user_id"]
                        payload_version = row.get("state_version")
                        if isinstance(payload_version, bool) or not isinstance(payload_version, int):
                            payload_version = 0
                        existing = existing_profiles.get(user_id)
                        if existing is not None:
                            existing_version = existing["state_version"]
                            prev_blob = existing["blob"]
                            prev_state_version = existing_version
                        else:
                            existing_version = 0
                            prev_blob = None
                            prev_state_version = None
                        new_version = max(existing_version, payload_version) + 1
                        created_at = row.get("created_at", restore_now)
                        try:
                            conn.execute(
                                "INSERT INTO user_profile "
                                "(user_id, blob, state_version, updated_at, prev_blob, prev_state_version, created_at) "
                                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (user_id, row["blob"], new_version, restore_now,
                                 prev_blob, prev_state_version, created_at),
                            )
                        except (sqlite3.Error, OverflowError) as exc:
                            # OverflowError: a hostile/corrupt payload state_version near
                            # 2^63 makes new_version exceed SQLite's INTEGER max BEFORE the
                            # INSERT — sqlite3.Error alone would let it escape as a 500
                            # instead of the endpoint's clean 422 RestoreError contract.
                            raise RestoreError(f"insert into {tbl!r} failed: {exc}") from exc
                        count += 1
                        continue
                    use = [c for c in cols if c in row]
                    if not use:
                        raise RestoreError(f"row in {tbl!r} has no recognised columns")
                    col_clause = ", ".join(use)
                    placeholders = ", ".join("?" * len(use))
                    try:
                        conn.execute(
                            f"INSERT INTO {tbl} ({col_clause}) VALUES ({placeholders})",
                            [row[c] for c in use],
                        )
                    except sqlite3.Error as exc:
                        raise RestoreError(f"insert into {tbl!r} failed: {exc}") from exc
                    count += 1
                restored[tbl] = count
            # Post-insert integrity checks
            fk_violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk_violations:
                raise RestoreError(
                    f"foreign key violations after import ({len(fk_violations)} row(s))"
                )
            ic_rows = conn.execute("PRAGMA integrity_check").fetchall()
            if not (len(ic_rows) == 1 and ic_rows[0][0] == "ok"):
                first = ic_rows[0][0] if ic_rows else "no result"
                raise RestoreError(f"integrity_check failed: {first}")
            conn.execute(f"PRAGMA user_version = {schema_version}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.isolation_level = prior_isolation

    # (d) run pending migrations if the imported schema is older than the current app schema
    final_version = schema_version
    if schema_version < current:
        init_db(conn)
        final_version = conn.execute("PRAGMA user_version").fetchone()[0]

    # (e) success — prune old safety copies (never reached if anything above raised)
    if db_file:
        _prune_pre_import_backups(db_file)

    return {"restored": restored, "schemaVersion": final_version, "safetyCopy": safety_path}


__all__ = [
    "resolve_db_path", "connect", "init_db",
    "create_account", "get_account", "list_accounts", "update_account", "delete_account",
    "account_liability_map",
    "create_txn", "list_txns", "update_txn", "delete_txn", "list_tags",
    "upsert_snapshot", "list_snapshots", "delete_snapshot",
    "save_plan", "get_plan", "month_actuals", "suggestions",
    "create_template", "list_templates", "delete_template",
    "upsert_recurring", "list_recurring", "delete_recurring",
    "get_profile", "put_profile",
    "RestoreError", "export_all", "import_all",
]
