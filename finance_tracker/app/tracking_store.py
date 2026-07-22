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
import io
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

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
    ("account",          ("id", "user_id", "name", "type", "is_liability", "currency", "archived", "created_at", "invest_group")),
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
    # S1.2 (DEC-027/DEC-035, docs/s1_2-migration-design.md §1.3): the per-user server profile.
    # `prev_blob`/`prev_state_version` are DELIBERATELY excluded from this column tuple —
    # they are an ephemeral conflict-recovery buffer, not restore-worthy state; the
    # allow-list intersection drops them for free, so a restore rebuilds a clean
    # current-only profile (no stitched-together undo chain from before the restore).
    ("user_profile",     ("user_id", "blob", "state_version", "updated_at", "created_at")),
)

# Tables added AFTER the original 9 — absent in older backups, so restore treats them as empty
# instead of rejecting the file. The original 9 stay strictly required (DEC-016 / DEC-017 #1).
_BACKUP_OPTIONAL_TABLES = frozenset({"scenario", "goal", "venture", "user_profile"})


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
  invest_group TEXT                        -- Invest-tab grouping (TODO-222): free text with UI presets; NULL = not an investment account grouping
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
CREATE TABLE IF NOT EXISTS users (
  user_id    TEXT PRIMARY KEY,
  role       TEXT NOT NULL CHECK (role IN ('owner','member')),
  created_at TEXT NOT NULL
);
-- At-most-one-owner invariant, enforced at the DB layer (mirrors idx_scenario_active):
-- a concurrent double-provision-as-owner fails the INSERT (IntegrityError) rather than
-- silently producing two owners.
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_one_owner
  ON users(role) WHERE role = 'owner';
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


_MIGRATIONS: list = [_mig_add_partner_owed, _mig_drop_bucket_checks, _mig_add_txn_status_kind, _mig_add_invest_group, _mig_add_goal_table, _mig_add_venture_table, _mig_add_users_table, _mig_add_user_scoping, _mig_add_user_profile]


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


def resolve_or_provision_user(conn: sqlite3.Connection, user_id: str) -> dict:
    """Look up *user_id* in the `users` table; provision it if this is the first time
    it's been seen. The very first id ever provisioned becomes 'owner'; every
    subsequently-seen new id becomes 'member' -- at most one owner (also enforced by
    the idx_users_one_owner unique index as a concurrency backstop). Idempotent: an
    id that's already provisioned just returns its stored role unchanged, regardless
    of how many times it's seen again.

    Called ONLY from server.py's resolve_user() for ids obtained via a trusted ingress
    header or the DEC-022 dev override -- never for the no-identity-signal case (see
    resolve_owner_fallback below).

    First-provision race (SEV-005): two never-seen ids can both observe "no existing
    owner" (our own SELECT above) and both attempt to INSERT as 'owner'. The
    idx_users_one_owner unique index lets only one such INSERT succeed; the loser's
    INSERT raises sqlite3.IntegrityError -- and since it's *this* user_id's own row that
    failed to land (the winner's id got the owner row, not ours), we retry the INSERT
    for this same user_id as 'member' instead of surfacing a 500. The DB invariant
    already prevents two owners; this just turns the race into a graceful member
    assignment for whoever lost it.

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
    row = conn.execute("SELECT user_id, role FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if row is not None:
        return {"id": row["user_id"], "role": row["role"]}
    existing_owner = conn.execute("SELECT 1 FROM users WHERE role = 'owner'").fetchone()
    role = "member" if existing_owner is not None else "owner"
    try:
        conn.execute(
            "INSERT INTO users (user_id, role, created_at) VALUES (?, ?, ?)",
            (user_id, role, _now()),
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
                "INSERT INTO users (user_id, role, created_at) VALUES (?, 'member', ?)",
                (user_id, _now()),
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
    (server.py enforces that)."""
    rows = conn.execute(
        "SELECT user_id, role, created_at FROM users ORDER BY (role != 'owner'), created_at"
    ).fetchall()
    return [{"id": r["user_id"], "role": r["role"], "createdAt": r["created_at"]} for r in rows]


# ---------- accounts ----------

def create_account(conn, user_id, name, type="other", is_liability=False, currency="USD", invest_group=None) -> dict:
    if type not in _ACCOUNT_TYPES:
        raise ValueError(f"invalid account type: {type!r}")
    invest_group = (invest_group or "").strip() or None
    cur = conn.execute(
        "INSERT INTO account (user_id, name, type, is_liability, currency, created_at, invest_group) VALUES (?,?,?,?,?,?,?)",
        (user_id, name, type, int(bool(is_liability)), currency, _now(), invest_group),
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
    allowed = {"name", "type", "is_liability", "archived", "currency", "invest_group"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "type" and v not in _ACCOUNT_TYPES:
            raise ValueError(f"invalid account type: {v!r}")
        if k == "invest_group":
            v = (v or "").strip() or None   # empty string clears the group
        if k in ("is_liability", "archived"):
            v = int(bool(v))
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
    return {
        "id": r["id"], "name": r["name"], "type": r["type"],
        "isLiability": bool(r["is_liability"]), "currency": r["currency"],
        "archived": bool(r["archived"]), "createdAt": r["created_at"],
        "investGroup": r["invest_group"],
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
    snap_rows = [dict(r) for r in conn.execute(
        "SELECT account_id, as_of, balance_cents FROM balance_snapshot WHERE user_id = ?",
        (user_id,)).fetchall()]
    return tracking.aggregate_actuals(txn_rows, snap_rows, account_liability_map(conn, user_id), month)


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
    return {"scenario": get_scenario(conn, user_id, scenario_id), "restored": restored,
            "clientState": revert.get("clientState")}


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
        elif tbl == "user_profile":
            order_by = "user_id"                # PK is user_id, not id -- no surrogate id column
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
