#!/usr/bin/env python3
"""
ledger_migrate.py — idempotent, re-runnable migration for Ledger API v2.

Creates the v2 tables (`ledger_holds`, `ledger_idempotency`, `ledger_entries`,
`ledger_meta`), adds the core-side freeze columns to `balances`, and ensures the
treasury wallet rows exist. It backfills nothing destructive: no row is deleted,
no existing column is rewritten, no balance is touched.

Run it as many times as you like:

    python3 ledger_migrate.py                       # against ./restocker.db
    python3 ledger_migrate.py --db /path/restocker.db
    python3 ledger_migrate.py --dry-run             # report, change nothing

WHY `PRAGMA defer_foreign_keys=ON` (this is not decoration)
-----------------------------------------------------------
A previous migration here was validated against a *copy* of the database opened
with `foreign_keys=0`, then failed in production where `Restocker_db._get_conn()`
sets `PRAGMA foreign_keys=ON` on every connection. This script therefore:

  1. opens with `foreign_keys=ON`, so it runs under production's rules, and
  2. sets `PRAGMA defer_foreign_keys=ON` *inside* the transaction, so FK
     enforcement is postponed to COMMIT rather than checked statement-by-
     statement. Order of statements inside the migration then stops mattering:
     `ledger_holds` may reference `balances(user_id)` before the treasury rows
     that satisfy it are inserted, and the whole thing is still verified before
     the transaction is allowed to land.

`defer_foreign_keys` resets itself at every COMMIT, so it is re-issued for each
transaction rather than set once at the top.

WHY THE `balances` TRIGGER (finding S1 — this is the escrow guarantee)
---------------------------------------------------------------------
`ledger_v2` enforces `available = balance - held` on every path it owns. It owns
none of the legacy ones. `Restocker_db.adjust_balance` — the mutator every shop,
hive and payout in Restocker goes through — writes `coins = MAX(0, coins - ?)`
with no reference to `ledger_holds` and no failure mode, so a bid's escrow could
be spent by a shop purchase and the auction's capture would then fail forever.

Auditing every caller does not close that; DATABASE triggers do. There are
three, covering the three write shapes that can lower a wallet — UPDATE,
INSERT-that-lands (which is what `INSERT OR REPLACE` is) and DELETE. An INSERT
that is IGNORED is not covered and does not need to be: it writes nothing. See
`HOLD_GUARD_DDL` below for the rule, exactly what is and is not covered, why it
does not fire on ledger_v2's own capture, and what a legacy caller now sees.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Iterable

MIGRATION_ID: str = "ledger_v2_001"

#: S1. Recorded separately from MIGRATION_ID so a database that already has
#: `ledger_v2_001` still gets the trigger on the next run.
TRIGGER_MIGRATION_ID: str = "ledger_v2_002_balances_respect_holds"

#: N1/N2/N3. Same reason: a database that already has 001 and 002 still needs
#: the `settling` / `applied_unknown` columns and the INSERT/DELETE guards.
ROUND3_MIGRATION_ID: str = "ledger_v2_003_settling_applied_unknown_write_shapes"

#: Names of the escrow guard triggers. All are dropped and recreated on every
#: run, so editing the DDL here is enough to update a live database.
HOLD_GUARD_TRIGGER: str = "ledger_balances_respect_holds"
HOLD_GUARD_INSERT_TRIGGER: str = "ledger_balances_respect_holds_ins"
HOLD_GUARD_DELETE_TRIGGER: str = "ledger_balances_respect_holds_del"

TREASURY_ACCOUNTS: tuple[str, ...] = ("treasury:osentar", "treasury:estates")


# --------------------------------------------------------------------------
# DDL
# --------------------------------------------------------------------------
# NOTE ON MONEY TYPES
# `balances.coins` is REAL in the legacy schema and cannot be retyped without
# rewriting the table (and every REAL column that joins to it). Everything v2
# writes is an INTEGER number of coins, and every v2 read casts with int().
# `ledger_holds.amount` and the ledger entry columns are declared INTEGER so the
# new surface is float-free even while the old column is not. Retyping
# `balances.coins` to INTEGER is a separate, riskier migration — deliberately not
# bundled here.

SCHEMA_V2: str = """
CREATE TABLE IF NOT EXISTS ledger_holds (
    hold_id         TEXT PRIMARY KEY,
    service         TEXT NOT NULL,             -- owning service: osentar | estates
    user_id         TEXT NOT NULL,             -- Discord user id, or treasury:*
    amount          INTEGER NOT NULL,          -- coins reserved, > 0, immutable
    captured_amount INTEGER NOT NULL DEFAULT 0,
    released_amount INTEGER NOT NULL DEFAULT 0,
    state           TEXT NOT NULL DEFAULT 'open',
                    -- open | captured | released | expired
    reason          TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT,                      -- key of the call that created it
    terminal_key    TEXT,                      -- key of the call that terminated it
    settling        INTEGER NOT NULL DEFAULT 0,
                    -- N2. Coins this hold has claimed and is about to debit,
                    -- non-zero ONLY inside `capture_hold`'s transaction. It is
                    -- how the escrow guard tells a capture settling its own
                    -- reservation from a shop purchase spending someone else's.
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at      TEXT NOT NULL,             -- ISO-8601 UTC, required, never NULL
    CHECK (amount > 0),
    CHECK (captured_amount >= 0),
    CHECK (released_amount >= 0),
    CHECK (captured_amount + released_amount <= amount),
    CHECK (state IN ('open','captured','released','expired'))
);

-- The hot path: SUM(amount) of one user's OPEN holds, on every balance read and
-- every debit guard.
CREATE INDEX IF NOT EXISTS idx_ledger_holds_open
    ON ledger_holds(user_id, state);
CREATE INDEX IF NOT EXISTS idx_ledger_holds_service
    ON ledger_holds(service, user_id, state);
-- The sweep: oldest open hold past its expiry first.
CREATE INDEX IF NOT EXISTS idx_ledger_holds_expiry
    ON ledger_holds(state, expires_at);


CREATE TABLE IF NOT EXISTS ledger_idempotency (
    key           TEXT PRIMARY KEY,
    service       TEXT NOT NULL,
    endpoint      TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,   -- sha256 of the canonical request body
    state         TEXT NOT NULL DEFAULT 'in_progress',   -- in_progress | done
    applied_unknown INTEGER NOT NULL DEFAULT 0,
                    -- N1. 1 while this key's money move is in flight OUTSIDE
                    -- the ledger transaction (`/stock/*` via run_on_bot_loop).
                    -- Set by the same statement that takes the claim, so there
                    -- is no window without it. A row with 1 may never have its
                    -- claim taken over or deleted: `in_progress` does not mean
                    -- "not applied" for it.
    status_code   INTEGER,
    response_json TEXT,            -- stored verbatim so a replay is byte-identical
    created_at    REAL NOT NULL,
    completed_at  REAL,
    CHECK (state IN ('in_progress','done'))
);

-- Drives the 30-day retention sweep.
CREATE INDEX IF NOT EXISTS idx_ledger_idem_created
    ON ledger_idempotency(created_at);


CREATE TABLE IF NOT EXISTS ledger_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    service         TEXT NOT NULL,
    action          TEXT NOT NULL,   -- adjust | transfer_out | transfer_in |
                                     -- hold | capture | release | expire | stock_*
    user_id         TEXT NOT NULL,
    delta           INTEGER NOT NULL DEFAULT 0,   -- signed coin movement, 0 for holds
    balance_after   INTEGER,
    hold_id         TEXT,
    counterparty    TEXT,
    reason          TEXT NOT NULL DEFAULT '',
    idempotency_key TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_ledger_entries_user
    ON ledger_entries(user_id, id);
CREATE INDEX IF NOT EXISTS idx_ledger_entries_key
    ON ledger_entries(idempotency_key);


-- Scheduler / operator scratch. NOTE (S11): the expiry sweep does NOT keep a
-- cursor here. Its progress marker is the hold's own state — the candidate
-- query is `state='open' AND expires_at <= now`, so a released row leaves the
-- set and a half-finished sweep resumes with exactly the rows it never reached.
-- A `hold_sweep_cursor` key was written per row and read nowhere for two
-- rounds; `migrate()` below deletes it.
CREATE TABLE IF NOT EXISTS ledger_meta (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);


CREATE TABLE IF NOT EXISTS ledger_migrations (
    id         TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


# --------------------------------------------------------------------------
# S1 / N2 / N3 — the escrow guard. This is the one thing the ledger contract
# exists to guarantee, and until v2 it was advisory.
# --------------------------------------------------------------------------
# THE RULE
#   A write to `balances` may not deepen the gap between a user's coins and the
#   total their OPEN holds have reserved. `ledger_v2`, the legacy
#   `Restocker_db.adjust_balance`, `set_balance`, a hand-typed statement in a
#   sqlite3 shell — all are subject to it, because it lives in the schema and
#   not in any one caller.
#
# WHAT IS COVERED, EXACTLY  (N3 — the previous version of this comment claimed
# "any writer" while the schema enforced one statement shape)
#   UPDATE  ... SET coins = ...      → `ledger_balances_respect_holds`
#   INSERT OR REPLACE / a real INSERT → `ledger_balances_respect_holds_ins`
#   DELETE  FROM balances             → `ledger_balances_respect_holds_del`
#   INSERT ... ON CONFLICT DO UPDATE  → fires the UPDATE trigger (SQLite upsert)
#
#   NOT covered, deliberately: an INSERT that is IGNORED — `INSERT OR IGNORE` /
#   `ON CONFLICT DO NOTHING`, the ensure-wallet idiom used all over Restocker.
#   It writes nothing, so there is nothing to guard, and that is exactly why the
#   INSERT guard is AFTER INSERT rather than BEFORE: a BEFORE INSERT trigger
#   fires for an insert that is about to be discarded, and cannot tell one from
#   an `INSERT OR REPLACE` that is about to destroy a live balance. It would
#   have turned every `ensure_wallet(uid)` on a user with an open hold into an
#   IntegrityError. AFTER INSERT fires only when a row really landed, which is
#   true of REPLACE and false of an ignored insert.
#
#   Also not covered: an UPDATE that moves a row's `user_id` away from its
#   holds. Nothing in Restocker rewrites a wallet's id; if that changes, this
#   comment is where the next reader should find out that it is not guarded.
#
# WHY IT DOES NOT FIRE ON ledger_v2's OWN CAPTURE  (this is load-bearing)
#   `capture_hold` legitimately debits coins that a hold reserved. Two facts
#   make that safe, and both are things the capture either did or did not do —
#   neither is an exemption a bug can help itself to:
#
#       ledger_v2.capture_hold() does, inside ONE `BEGIN IMMEDIATE`:
#         1. UPDATE ledger_holds SET state='captured', settling=<amt>
#                                WHERE hold_id=? AND state='open'
#         2. UPDATE balances     SET coins = coins - <amt>          ← the guard
#         3. UPDATE ledger_holds SET settling=0 WHERE hold_id=?
#
#   Step 1 is claim-first and runs BEFORE step 2, so the hold is out of the open
#   total by the time the guard sees the debit. And step 1 declares `settling`,
#   so the guard also knows the debit in step 2 is the SETTLEMENT of the
#   reservation step 1 just retired.
#
#   The second half is N2, and it is not decoration. Ordering alone leaves the
#   guard unable to distinguish these two statements:
#       coins 8000 → 5000, with 6000 of other open holds — a capture of a 3000
#       hold on a wallet that was already over-committed; and the same numbers
#       written by a shop purchase.
#   The old rule (`NEW.coins >= open total`) refused both. On a wallet holding
#   8000 against holds of 3000 + 6000, capturing the 3000 was blocked by the
#   6000 and capturing the 6000 was blocked by the 3000: NEITHER legitimate hold
#   could ever be captured, the lot or market froze, and the only exit was DB
#   surgery. The rule below is instead "the shortfall may not GROW", with a
#   settling hold counted on the before side — so a capture, which retires a
#   reservation and debits exactly that reservation, leaves the shortfall
#   unchanged and is allowed, while a shop purchase, which retires nothing,
#   grows it and is refused. On a healthy wallet the two rules are identical.
#
#   `settling` is non-zero only between steps 1 and 3 of one transaction: a
#   rollback discards it and step 3 is unconditional, so it cannot survive a
#   commit. `ledger_v2.escrow_settling_leaks()` checks that once a minute rather
#   than trusting this paragraph.
#
# WHY `open total > 0` IS PART OF THE CONDITION
#   With no open holds the floor would be 0, and the trigger would then also ban
#   NEGATIVE balances. That collides with S12: `treasury:*` accounts are allowed
#   to go negative on purpose, so an estates bug misallocates coins and the
#   treasury screams, instead of the payout row parking as `insufficient` and
#   looking exactly like a punter with an empty wallet. A user row can never be
#   driven below zero anyway (`_debit` refuses, `adjust_balance` clamps at 0), so
#   the extra term costs nothing there.
#
# WHAT A LEGACY CALLER NOW SEES
#   `sqlite3.IntegrityError: insufficient: would spend coins reserved by an open
#   hold`. RAISE(ABORT) undoes the offending statement and propagates the error
#   to the application — it is never a silent no-op and never a partial debit.
#   `Restocker_db.adjust_balance` does not catch it, so it propagates to its
#   caller, whose `with db()` block rolls back. The purchase/payout fails and the
#   user's escrow is intact. See LEDGER_API_v2.md §5.1.

#: The user's open-hold total. `{col}` is the row alias holding `user_id`.
_OPEN_HOLDS: str = """(SELECT COALESCE(SUM(h.amount - h.captured_amount - h.released_amount), 0)
        FROM ledger_holds h
       WHERE h.user_id = {col}.user_id AND h.state = 'open')"""

#: Coins this user's holds have claimed and not yet debited. Non-zero only
#: inside `capture_hold`'s transaction (N2).
_SETTLING: str = """(SELECT COALESCE(SUM(h.settling), 0)
        FROM ledger_holds h
       WHERE h.user_id = {col}.user_id)"""

_OPEN_NEW = _OPEN_HOLDS.format(col="NEW")
_OPEN_OLD = _OPEN_HOLDS.format(col="OLD")
_SETTLING_NEW = _SETTLING.format(col="NEW")

HOLD_GUARD_DDL: str = f"""
CREATE TRIGGER {HOLD_GUARD_TRIGGER}
BEFORE UPDATE OF coins ON balances
FOR EACH ROW
WHEN CAST(NEW.coins AS INTEGER) < CAST(OLD.coins AS INTEGER)
 AND {_OPEN_NEW} > 0
 AND {_OPEN_NEW} - CAST(NEW.coins AS INTEGER)
   > MAX(0, {_OPEN_NEW} + {_SETTLING_NEW} - CAST(OLD.coins AS INTEGER))
BEGIN
    SELECT RAISE(ABORT,
        'insufficient: would spend coins reserved by an open hold (ledger_holds)');
END;
"""

#: N3. `INSERT OR REPLACE INTO balances ...` is a DELETE+INSERT: it never fires
#: an UPDATE trigger, and with `recursive_triggers` OFF (the default, and what
#: production runs) it does not fire a DELETE trigger either. Verified before
#: this existed: a REPLACE set a wallet with a live 6,000 hold to 0 coins and
#: the guard did not see it. AFTER INSERT is the one hook a REPLACE cannot
#: dodge, and — unlike BEFORE INSERT — it does not fire for the ignored inserts
#: that `ensure_wallet` does on every credit.
#:
#: R3-7 — WHY `- settling` IS HERE. An AFTER INSERT trigger has no OLD row (a
#: REPLACE's delete has already happened), so this guard cannot express the
#: UPDATE guard's "the shortfall may not GROW" rule; it can only be an absolute
#: floor. The floor it uses has to be the one the UPDATE rule degenerates to for
#: the same wallet in the same state, or the two guards disagree about one
#: invariant. Mid-capture that state is: the hold is still open, `settling`
#: holds the amount about to be debited, and the UPDATE guard deliberately
#: ALLOWS `coins` to fall to `open - settling` (that is the whole reason
#: `_SETTLING` exists — without it a capture's own debit aborts). Without the
#: term here, a REPLACE writing exactly that value would be refused by this
#: trigger and permitted by the other one.
#:
#: `settling` is non-zero only inside `capture_hold`'s transaction, so on every
#: other wallet this is character-for-character the old rule (`coins < open`),
#: and `ledger_v2.escrow_settling_leaks()` is what stops a stuck non-zero
#: `settling` quietly widening it. Unreachable today — nothing REPLACEs a
#: balance mid-capture — and made to agree anyway, because the next person to
#: make it reachable will not know that it did not.
HOLD_GUARD_INSERT_DDL: str = f"""
CREATE TRIGGER {HOLD_GUARD_INSERT_TRIGGER}
AFTER INSERT ON balances
FOR EACH ROW
WHEN {_OPEN_NEW} > 0
 AND CAST(NEW.coins AS INTEGER) < {_OPEN_NEW} - {_SETTLING_NEW}
BEGIN
    SELECT RAISE(ABORT,
        'insufficient: would spend coins reserved by an open hold (ledger_holds)');
END;
"""

#: N3. A wallet row with open holds may not be deleted: the holds would still
#: read as open, `_read_balance` would report 0 coins with a positive `held`,
#: and the capture would fail forever on a lot that had already been won.
HOLD_GUARD_DELETE_DDL: str = f"""
CREATE TRIGGER {HOLD_GUARD_DELETE_TRIGGER}
BEFORE DELETE ON balances
FOR EACH ROW
WHEN {_OPEN_OLD} > 0
BEGIN
    SELECT RAISE(ABORT,
        'cannot delete a wallet with open holds (ledger_holds)');
END;
"""

#: (trigger name, DDL) for every guard, in creation order. `migrate()` drops and
#: recreates the whole set on every run, so adding one here is enough.
HOLD_GUARDS: tuple[tuple[str, str], ...] = (
    (HOLD_GUARD_TRIGGER, HOLD_GUARD_DDL),
    (HOLD_GUARD_INSERT_TRIGGER, HOLD_GUARD_INSERT_DDL),
    (HOLD_GUARD_DELETE_TRIGGER, HOLD_GUARD_DELETE_DDL),
)

def hold_guard_drop(name: str) -> str:
    return f"DROP TRIGGER IF EXISTS {name}"


# Column additions. Each is tried independently; "duplicate column name" means a
# previous run already applied it and is not an error.
#
# ORDER MATTERS against the guard DDL: `HOLD_GUARD_DDL` reads
# `ledger_holds.settling`, so the ALTER has to land before the trigger is
# created. `migrate()` runs the schema, then these, then the triggers — a
# database created by an earlier v2 already has the table, so only the ALTER
# gives it the column.
COLUMN_MIGRATIONS: tuple[str, ...] = (
    "ALTER TABLE balances ADD COLUMN frozen        INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE balances ADD COLUMN frozen_reason TEXT",
    "ALTER TABLE balances ADD COLUMN frozen_by     TEXT",
    "ALTER TABLE balances ADD COLUMN frozen_at     TEXT",
    # N2 — see HOLD_GUARD_DDL. Default 0 is the only correct backfill: no
    # capture can be in flight while this migration holds the write lock.
    "ALTER TABLE ledger_holds ADD COLUMN settling INTEGER NOT NULL DEFAULT 0",
    # N1 — see ledger_v2.IN_BAND_ENDPOINTS. Default 0 is the correct backfill
    # for the same reason it is the correct value for an in-band claim: every
    # pre-existing row was written by a build in which no claim was protected,
    # and marking historical rows unknown would strand replays that already work.
    "ALTER TABLE ledger_idempotency ADD COLUMN applied_unknown INTEGER NOT NULL "
    "DEFAULT 0",
)


def _split_statements(script: str) -> list[str]:
    """Split a DDL script into statements WITHOUT breaking on ';' inside comments.

    Naive `script.split(";")` looks fine until one comment contains a semicolon,
    at which point it silently cuts a CREATE TABLE in half and the migration
    dies with "incomplete input" — on the live database, halfway through.
    `sqlite3.complete_statement` is the parser SQLite itself uses for exactly
    this, so statements are accumulated line by line until it says a statement
    has closed.
    """
    statements: list[str] = []
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            stmt = buf.strip()
            if stmt.rstrip(";").strip():
                statements.append(stmt)
            buf = ""
    if buf.strip():
        raise ValueError(f"unterminated SQL statement in migration: {buf.strip()[:80]!r}")
    return statements


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _trigger_sql(conn: sqlite3.Connection, name: str) -> str:
    """The stored text of a trigger, or '' if it does not exist."""
    row = conn.execute(
        "SELECT COALESCE(sql, '') FROM sqlite_master WHERE type='trigger' AND name=?",
        (name,),
    ).fetchone()
    return str(row[0]) if row else ""


def _trigger_is_current(conn: sqlite3.Connection, name: str, ddl: str) -> bool:
    """True when the live trigger text matches `ddl` exactly.

    Compared on whitespace-normalised text so re-indenting the constant is not
    reported as a pending change, but a rule change is.
    """
    live = " ".join(_trigger_sql(conn, name).split())
    want = " ".join(ddl.strip().rstrip(";").split())
    return bool(live) and live == want


def _guard_status(conn: sqlite3.Connection) -> dict[str, str]:
    """`{trigger name: current|outdated|missing}` for every escrow guard."""
    return {
        name: ("current" if _trigger_is_current(conn, name, ddl)
               else ("outdated" if _trigger_sql(conn, name) else "missing"))
        for name, ddl in HOLD_GUARDS
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _apply_column_migrations(conn: sqlite3.Connection, stmts: Iterable[str]) -> list[str]:
    """ALTER TABLE ADD COLUMN, one at a time, tolerating already-applied ones.

    Mirrors `Restocker_db._migrate`'s try/except style, but distinguishes the
    benign "duplicate column name" from every other OperationalError instead of
    swallowing all of them — a typo'd ALTER should fail the migration loudly.
    """
    applied: list[str] = []
    for stmt in stmts:
        try:
            conn.execute(stmt)
            applied.append(stmt)
        except sqlite3.OperationalError as exc:
            if "duplicate column name" in str(exc).lower():
                continue
            raise
    return applied


def migrate(db_path: Path, *, dry_run: bool = False, verbose: bool = True) -> dict:
    """Apply the v2 migration. Safe to run repeatedly. Returns a report dict."""

    def say(msg: str) -> None:
        if verbose:
            print(msg)

    if not db_path.exists():
        raise SystemExit(
            f"✗ {db_path} does not exist. Point --db at the live restocker.db; "
            f"this migration never creates the database from scratch."
        )

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        # Match production connection settings EXACTLY. The failure this script
        # is written against was a migration validated with foreign_keys=0.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")

        fk_on = int(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        say(f"• foreign_keys enforcement: {'ON' if fk_on else 'OFF'}")
        if not fk_on:
            raise SystemExit(
                "✗ Refusing to run: PRAGMA foreign_keys could not be enabled, so "
                "this run would not reproduce production's constraint checking. "
                "That is exactly how the last migration passed here and failed there."
            )

        if not _table_exists(conn, "balances"):
            raise SystemExit(
                f"✗ {db_path} has no `balances` table — this is not restocker.db."
            )

        already = _table_exists(conn, "ledger_migrations") and conn.execute(
            "SELECT 1 FROM ledger_migrations WHERE id=?", (MIGRATION_ID,)
        ).fetchone()

        report: dict = {
            "db": str(db_path),
            "migration_id": MIGRATION_ID,
            "previously_applied": bool(already),
            "dry_run": dry_run,
            "columns_added": [],
            "treasuries_created": [],
            "tables": [],
            "hold_guard_trigger": "unknown",
            "hold_guards": {},
        }

        if dry_run:
            missing_tables = [
                t for t in ("ledger_holds", "ledger_idempotency", "ledger_entries",
                            "ledger_meta", "ledger_migrations")
                if not _table_exists(conn, t)
            ]
            missing_cols = sorted(
                ({"frozen", "frozen_reason", "frozen_by", "frozen_at"}
                 - _columns(conn, "balances"))
                | ({"ledger_holds.settling"}
                   if _table_exists(conn, "ledger_holds")
                   and "settling" not in _columns(conn, "ledger_holds") else set())
                | ({"ledger_idempotency.applied_unknown"}
                   if _table_exists(conn, "ledger_idempotency")
                   and "applied_unknown" not in _columns(conn, "ledger_idempotency")
                   else set())
            )
            missing_treasuries = [
                t for t in TREASURY_ACCOUNTS
                if not conn.execute(
                    "SELECT 1 FROM balances WHERE user_id=?", (t,)
                ).fetchone()
            ]
            report["tables"] = missing_tables
            report["columns_added"] = missing_cols
            report["treasuries_created"] = missing_treasuries
            guards = _guard_status(conn) if _table_exists(conn, "ledger_holds") else {
                name: "missing" for name, _ in HOLD_GUARDS
            }
            report["hold_guards"] = guards
            report["hold_guard_trigger"] = guards[HOLD_GUARD_TRIGGER]
            say(f"• would create tables      : {missing_tables or '(none)'}")
            say(f"• would add columns        : {missing_cols or '(none)'}")
            say(f"• would create treasuries  : {missing_treasuries or '(none)'}")
            for name, status in guards.items():
                say(f"• escrow guard {name:34s}: {status}"
                    + ("" if status == "current" else " → would be (re)created"))
            say("• dry run — nothing written.")
            return report

        # ---- one transaction, FK checks deferred to COMMIT -----------------
        # defer_foreign_keys is per-transaction and resets on COMMIT, so it is
        # issued here, after BEGIN, not once at connection setup.
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("PRAGMA defer_foreign_keys=ON")

        # NB: executescript() would COMMIT the open transaction implicitly and
        # drop us out of the deferred-FK window. The DDL is therefore executed
        # statement by statement.
        for stmt in _split_statements(SCHEMA_V2):
            conn.execute(stmt)

        report["columns_added"] = _apply_column_migrations(conn, COLUMN_MIGRATIONS)

        # S1/N2/N3 — the escrow guards. DROP + CREATE rather than
        # CREATE IF NOT EXISTS: re-running must converge on the DDL in THIS file,
        # otherwise a database that got an earlier version of the rule keeps it
        # forever and the migration reports success. Dropping them inside the
        # transaction is safe — no row is rewritten, and the window in which the
        # guards are absent is the window in which this migration holds the write
        # lock, so nothing else can write a balance during it.
        before = _guard_status(conn)
        for name, ddl in HOLD_GUARDS:
            conn.execute(hold_guard_drop(name))
            conn.execute(ddl)
            report["hold_guards"][name] = (
                "unchanged" if before.get(name) == "current" else "created")
        report["hold_guard_trigger"] = report["hold_guards"][HOLD_GUARD_TRIGGER]

        # Treasury wallets are ordinary `balances` rows with non-numeric ids, so
        # the house's position is inspectable with the same tools as a player's.
        # INSERT OR IGNORE: never resets an existing treasury balance.
        for acct in TREASURY_ACCOUNTS:
            cur = conn.execute(
                "INSERT OR IGNORE INTO balances (user_id, coins, principal, lp) "
                "VALUES (?, 0, 0, 0)",
                (acct,),
            )
            if cur.rowcount == 1:
                report["treasuries_created"].append(acct)

        # S11. `hold_sweep_cursor` was seeded here and written once per released
        # hold by `sweep_expired_holds`, and read by nothing in either file. Two
        # reviews let it stand because it looked load-bearing. It is gone from
        # the sweep; this deletes the dead key rather than leaving a row that
        # invites the next reader to "resume from" it.
        cur = conn.execute("DELETE FROM ledger_meta WHERE key='hold_sweep_cursor'")
        report["dead_markers_removed"] = int(cur.rowcount or 0)

        for mid in (MIGRATION_ID, TRIGGER_MIGRATION_ID, ROUND3_MIGRATION_ID):
            conn.execute(
                "INSERT OR IGNORE INTO ledger_migrations (id) VALUES (?)", (mid,))

        # Verify BEFORE committing, while deferred FK violations are still
        # rollback-able. foreign_key_check returns one row per violation.
        violations = conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            conn.execute("ROLLBACK")
            raise SystemExit(
                f"✗ Rolled back: {len(violations)} foreign key violation(s) would "
                f"have been committed. First: {tuple(violations[0])}"
            )

        conn.execute("COMMIT")

        report["tables"] = [
            t for t in ("ledger_holds", "ledger_idempotency", "ledger_entries",
                        "ledger_meta", "ledger_migrations")
            if _table_exists(conn, t)
        ]

        say(f"• tables present     : {', '.join(report['tables'])}")
        say(f"• columns added      : {report['columns_added'] or '(already present)'}")
        say(f"• treasuries created : {report['treasuries_created'] or '(already present)'}")
        for name, status in report["hold_guards"].items():
            say(f"• escrow guard       : {name} ({status})")
        say("  → UPDATE, INSERT-that-lands (incl. INSERT OR REPLACE) and DELETE on "
            "balances now raise IntegrityError instead of eating open escrow; an "
            "ignored INSERT writes nothing and is not guarded")
        if report.get("dead_markers_removed"):
            say("• removed dead marker: ledger_meta['hold_sweep_cursor'] (S11)")
        say(f"✓ {MIGRATION_ID} applied to {db_path}"
            + (" (was already applied; re-run was a no-op)" if already else ""))
        return report
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ledger API v2 migration (idempotent).")
    ap.add_argument("--db", default="restocker.db", help="path to restocker.db")
    ap.add_argument("--dry-run", action="store_true",
                    help="report what would change; write nothing")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)
    migrate(Path(args.db), dry_run=args.dry_run, verbose=not args.quiet)
    return 0


if __name__ == "__main__":
    sys.exit(main())
