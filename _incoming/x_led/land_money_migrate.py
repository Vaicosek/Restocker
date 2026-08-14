#!/usr/bin/env python3
"""
land_money_migrate.py — convert the Land Exchange's money columns from REAL to
INTEGER coins.  Dry run by default.  Nothing is written without --apply.

────────────────────────────────────────────────────────────────────────────────
WHAT THIS TOUCHES
────────────────────────────────────────────────────────────────────────────────
  land_listings.reserve       REAL -> INTEGER
  land_listings.buy_now       REAL -> INTEGER
  land_listings.current_bid   REAL -> INTEGER   <- the only LIVE escrow figure
  land_listings.listing_fee   REAL -> INTEGER
  land_listings.sold_price    REAL -> INTEGER   <- SETTLED, needs --include-settled
  land_bids.amount            REAL -> INTEGER

WHAT THIS DELIBERATELY DOES NOT TOUCH, and why
  land_listings.min_increment_pct   percentage, 5.0 means 5%.  Not money.
  land_listings.commission_pct      percentage, 2.5% is legitimate.  Not money.
  land_listings.chunks              a land quantity, not a coin amount.
  balances.coins                    a far riskier migration with a live LEDGER
                                    API v2 trigger on it; ledger_migrate.py:85
                                    already carved it out as separate work.  If
                                    this script touched it the two migrations
                                    would race for the same rows.
  land_fees.*                       a different subsystem (teleport fees,
                                    cogs/lands.py, which is not staged).  Its
                                    values are INFERRED from balance deltas
                                    (Restocker_db.py:449) — rounding them is a
                                    materially different question and belongs in
                                    its own migration with its own preview.

────────────────────────────────────────────────────────────────────────────────
THE ROUNDING POLICY, AND WHY IT IS NOT A FREE CHOICE
────────────────────────────────────────────────────────────────────────────────
Every coin movement in cogs/land_exchange.py already goes through Python's
`int(round(x))` — :425 (bid debit), :427 (outbid refund), :360 (pre-empted
refund), :462 (instant-buy debit), :468 (compensating refund), :571 and :1127.
Python's round() is BANKER'S rounding (half to even).  So half-even is not a
proposal; it is the policy this table has been settled under for its entire
life.

That matters most for `current_bid` on an active auction.  The bidder was
debited `int(round(amt))` at :425.  The column stored the raw float at :429.
The refund pays `int(round(current_bid))` at :427.  Set current_bid to
`int(round(current_bid))` and the refund is bit-for-bit what it was going to be:
ZERO coins move.  Choose any other policy and the refund stops matching the
debit, by exactly `policy(x) - int(round(x)) ∈ {-1, 0, +1}` per row.

And SQLite's own ROUND() is HALF-AWAY-FROM-ZERO, not half-even.  Verified on
sqlite 3.45.1:  SELECT ROUND(1000.5) -> 1001.0,  Python round(1000.5) -> 1000.
So `UPDATE land_listings SET current_bid = ROUND(current_bid)` — the obvious
one-liner — silently applies a DIFFERENT policy than the code has used for
years.  This script therefore computes every new value in Python and never lets
SQLite do the arithmetic.

--rounding is offered so the preview can show all three side by side.  The dry
run always prints the comparison whichever you pick.  half-even is the default
and is the only value under which live escrow moves no coins.

────────────────────────────────────────────────────────────────────────────────
AN HONEST LIMIT: `INTEGER` IS A DECLARATION, NOT AN ENFORCEMENT
────────────────────────────────────────────────────────────────────────────────
SQLite's INTEGER affinity converts a REAL to INTEGER only when the conversion is
LOSSLESS.  Verified on 3.45.1:

    CREATE TABLE t (a INTEGER);
    INSERT INTO t VALUES (1000.0);   -- stored as integer 1000
    INSERT INTO t VALUES (1000.6);   -- stored as REAL 1000.6, no error

`Restocker_db.add_land_bid:3851` does `float(amount)` and `_min_next_bid:151`
returns `float(listing["reserve"])`.  Both still work after this migration and
both still write fractions.  So:

    This migration cleans the table.  It does not keep the table clean.

Fixing the source is `_min_next_bid:151`, `create_listing_core:531` and
`/realestate list:1138` (all of which mint a fractional reserve), plus
`add_land_bid:3851`.  Migrating columns without those is a treadmill.

A CHECK constraint (`CHECK (typeof(current_bid) IN ('integer','null'))`) would
turn the silent corruption into a loud IntegrityError, and this script
deliberately does NOT add one.  Look at where it would fire: `add_land_bid` is
called at land_exchange.py:428, which is THREE LINES after the bidder was
debited at :425 and the previous bidder was refunded at :427, none of it in one
transaction.  A CHECK there converts a record-keeping bug into "bidder charged,
previous bidder refunded, bid never recorded".  A guard rail that fires inside a
money path, after the money moved, is not a guard rail.  Add it after the write
paths are integer-clean, not before.

────────────────────────────────────────────────────────────────────────────────
MIGRATION TRACKING — WHAT THE CODEBASE ACTUALLY HAS
────────────────────────────────────────────────────────────────────────────────
I looked for one and there isn't one.  `Restocker_db._migrate:793` is a flat
list of ALTER TABLE strings each wrapped in `except sqlite3.OperationalError:
pass` — idempotent by swallowing errors, with no record of what ran or when.
There is no schema_migrations table, no PRAGMA user_version use anywhere in
Restocker_db.py / Restocker_main.py / Restocker_web.py / cogs.

So rather than invent a table, this records into the two things that already
exist for this database:

  1. `bot_config` (Restocker_db.py:606, accessors get_config:1748 /
     set_config:1968) — the live codebase's durable key/value store, readable
     from the running bot.  Key: `migration:land_money_int:v1`.
  2. `ledger_migrations` (id, applied_at) — ONLY IF IT ALREADY EXISTS, i.e. if
     ledger_migrate.py has already run against this same database.  This script
     never creates that table; it just joins the register if the register is
     there.

────────────────────────────────────────────────────────────────────────────────
FOREIGN KEYS
────────────────────────────────────────────────────────────────────────────────
Production connects with `PRAGMA foreign_keys=ON` (Restocker_db.py:26).  A past
migration here passed on a copy opened with enforcement OFF and failed in
production where it was on.  So this script opens with foreign_keys=ON, REFUSES
to run if it cannot be turned on, and REPORTS the observed state rather than
assuming it.

Then it turns enforcement OFF for the rebuild itself, and that deserves a
straight explanation rather than a footnote, because "the migration ran with
foreign keys off" is the exact shape of the failure rule 7 exists to prevent.

I assumed, writing the first version, that `PRAGMA defer_foreign_keys=ON` was
what made a drop-and-rename rebuild safe with enforcement left ON.  That is
wrong, and the test caught it.  Against a fixture where
`land_bids.listing_id REFERENCES land_listings(id) ON DELETE CASCADE`, the first
version printed:

    land_listings: 15 rows copied
    land_bids:      0 rows copied          <- all nine bids destroyed
    in-scope failures: 0
    APPLIED and VERIFIED

`DROP TABLE` under foreign_keys=ON runs an implicit `DELETE FROM` first, and that
DELETE fires ON DELETE actions.  `defer_foreign_keys` defers when constraints are
CHECKED; it does not disable the cascading ACTION.  And `PRAGMA foreign_keys` is
a silent no-op inside a transaction, so enforcement has to be settled before
BEGIN.  Both verified on 3.45.1.

So: foreign_keys=OFF for the rebuild (which is step 1 of SQLite's own documented
12-step ALTER TABLE procedure), with these compensating controls:

  * `PRAGMA defer_foreign_keys=ON` inside the transaction as well;
  * every table's row count, and every rebuilt table's NON-money content, is
    fingerprinted before the transaction and asserted inside it — so the failure
    above now rolls back instead of reporting success.  A verification pass that
    could not fail on an empty table was verifying nothing;
  * `PRAGMA foreign_key_check` inside the transaction, and again after COMMIT
    with enforcement restored to ON;
  * enforcement is restored to ON on both the success and the rollback path.

On the real schema none of this bites: the staged land tables declare no foreign
keys at all, so the preview will tell you "no other table references it — a
rebuild cannot cascade."  This is defence against a constraint added later.  It
is in here because the only reason I know about it is that I ran the thing.

The table rebuild reconstructs the new table from the LIVE `CREATE TABLE` text
in sqlite_master, rewriting only the money columns' type tokens.  It does not
use a hardcoded schema — production may carry columns added by a later
_migrate() ALTER that the staged source does not show, and a hardcoded CREATE
would silently drop them.  The rewrite is verified column-by-column against the
original table_info before any data is copied, and aborts if anything but the
money columns' declared type differs.
"""

from __future__ import annotations

import argparse
import json
import hashlib
import math
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

MIGRATION_ID = "land_money_int:v1"
CONFIG_KEY = f"migration:{MIGRATION_ID}"

TMP_SUFFIX = "__land_money_mig_new"

# table -> money columns, in schema order
MONEY_COLUMNS: dict[str, tuple[str, ...]] = {
    "land_listings": ("reserve", "buy_now", "current_bid", "listing_fee", "sold_price"),
    "land_bids": ("amount",),
}

# Columns that look like money and are not.  Listed so the preview can say so out
# loud — this is the mistake a "convert every REAL column" migration makes.
NOT_MONEY = {
    "land_listings": {
        "min_increment_pct": "percentage (5.0 == 5%)",
        "commission_pct": "percentage (2.5% is legitimate)",
        "chunks": "land quantity, not coins",
        "anti_snipe_minutes": "already INTEGER",
    },
}

# sold_price is settled by definition — it only exists on a closed listing.
ALWAYS_SETTLED = {("land_listings", "sold_price")}

ACTIVE_STATUS = "active"

MAX_EXACT_INT_IN_DOUBLE = 2 ** 53  # beyond this a float cannot represent every integer


# ══════════════════════════════════════════════════════════════════════════════
# rounding policies
# ══════════════════════════════════════════════════════════════════════════════

def r_half_even(x: float) -> int:
    """Python's round() — banker's.  The policy every coin movement already uses."""
    return int(round(x))


def r_half_up(x: float) -> int:
    """Half away from zero — what SQLite's own ROUND() does.  Differs from the
    code's behaviour on exact .5 values only."""
    return int(math.floor(x + 0.5)) if x >= 0 else int(math.ceil(x - 0.5))


def r_floor(x: float) -> int:
    """Truncate toward -inf.  Destroys the fraction; on live escrow that means
    the bidder is refunded less than they were debited."""
    return int(math.floor(x))


POLICIES = {
    "half-even": r_half_even,
    "half-up": r_half_up,
    "floor": r_floor,
}
DEFAULT_POLICY = "half-even"


# ══════════════════════════════════════════════════════════════════════════════
# small helpers
# ══════════════════════════════════════════════════════════════════════════════

def say(msg: str = "") -> None:
    print(msg, flush=True)


def rule(title: str = "") -> None:
    if title:
        say()
        say(f"── {title} " + "─" * max(0, 74 - len(title)))
    else:
        say("─" * 78)


def fmt_money(v) -> str:
    """Full precision, always.  A previous version of this used %.10g and printed
    9999.9999999999 as "10,000" — which is precisely the value whose whole point is
    that int() reads it as 9999 (FINDINGS §8).  A preview that rounds the number it
    is asking you to approve rounding is worthless."""
    if v is None:
        return "NULL"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN"
        if math.isinf(v):
            return "+Inf" if v > 0 else "-Inf"
        return repr(v)
    return repr(v)


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def table_sql(conn: sqlite3.Connection, name: str) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"no CREATE TABLE text in sqlite_master for {name!r}")
    return row[0]


def table_info(conn: sqlite3.Connection, name: str) -> list[dict]:
    return [
        {"cid": r[0], "name": r[1], "type": r[2], "notnull": r[3], "dflt": r[4], "pk": r[5]}
        for r in conn.execute(f"PRAGMA table_info({name})").fetchall()
    ]


def is_integral(v) -> bool:
    """True if this value is already a whole coin count.  NULL counts as clean."""
    if v is None:
        return True
    if isinstance(v, int):
        return True
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return False
        return v == int(v)
    return False


def stored_typeof(conn: sqlite3.Connection, table: str, col: str, rowid: int) -> str:
    row = conn.execute(f"SELECT typeof({col}) FROM {table} WHERE rowid=?", (rowid,)).fetchone()
    return row[0] if row else "?"



def inbound_fk_refs(conn: sqlite3.Connection, target: str) -> list[tuple[str, str, str]]:
    """Every (child_table, child_column, on_delete) that references `target`.

    This is the check that turns a silent data loss into a refusal.  See
    rebuild_table's docstring for what these do to a DROP TABLE.
    """
    out = []
    for (t,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        try:
            for r in conn.execute(f"PRAGMA foreign_key_list({t})"):
                if str(r[2]).lower() == target.lower():
                    out.append((t, r[3], str(r[6] or "NO ACTION").upper()))
        except sqlite3.OperationalError:
            continue
    return out


def snapshot(conn: sqlite3.Connection) -> dict:
    """Row count for every table, plus a content fingerprint of every rebuilt
    table's NON-money columns.

    The money columns are excluded because they are supposed to change.
    Everything else must survive byte-identical, and the row count must survive
    for EVERY table in the database — not just the ones being rebuilt.  That
    second part is the whole point: the first version of this script destroyed
    all 9 rows of a child table and still printed 'APPLIED and VERIFIED',
    because verification only checked that the surviving values were integral.
    A verification pass that cannot fail on an empty table verifies nothing.
    """
    snap = {"counts": {}, "content": {}}
    for (t,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"):
        try:
            snap["counts"][t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        except sqlite3.OperationalError:
            continue
    for t, money in MONEY_COLUMNS.items():
        if not table_exists(conn, t):
            continue
        cols = [c["name"] for c in table_info(conn, t) if c["name"] not in money]
        if not cols:
            continue
        rows = conn.execute(
            f"SELECT {', '.join(cols)} FROM {t} ORDER BY rowid").fetchall()
        h = hashlib.sha256()
        for r in rows:
            h.update(repr(tuple(r)).encode())
            h.update(b"\x00")
        snap["content"][t] = (h.hexdigest(), cols)
    return snap


def compare_snapshots(before: dict, after: dict) -> list[str]:
    """Differences that must abort the migration."""
    problems = []
    for t, n in before["counts"].items():
        m = after["counts"].get(t)
        if m is None:
            problems.append(f"table {t} DISAPPEARED (had {n:,} rows)")
        elif m != n:
            problems.append(f"table {t} row count {n:,} -> {m:,} ({m - n:+,})")
    for t in after["counts"]:
        if t not in before["counts"] and not t.endswith(TMP_SUFFIX):
            problems.append(f"table {t} APPEARED unexpectedly")
    for t, (h, cols) in before["content"].items():
        ha = after["content"].get(t)
        if ha is None:
            problems.append(f"{t}: no content fingerprint after rebuild")
        elif ha[1] != cols:
            problems.append(f"{t}: non-money column set changed {cols} -> {ha[1]}")
        elif ha[0] != h:
            problems.append(f"{t}: NON-MONEY content changed — this migration must only "
                            f"touch {', '.join(MONEY_COLUMNS[t])}")
    return problems


# ══════════════════════════════════════════════════════════════════════════════
# anomaly classification
# ══════════════════════════════════════════════════════════════════════════════

def anomaly_of(v) -> str | None:
    """Return a reason string if this value cannot be safely rounded, else None.

    On NaN, one correction to FINDINGS §8.  §8 traced a NaN bid through BOTH money
    guards in _place_bid_core (`amt < min_bid` and `bal < amt` are each False for
    NaN) and through json.loads, which accepts a bare NaN token.  All true.  But a
    NaN can never actually be READ BACK from a money column, because SQLite does
    not store NaN — it silently writes NULL instead.  Verified on 3.45.1:

        INSERT INTO t(a) VALUES (?)   with Python float('nan')
        SELECT a, typeof(a) FROM t    ->  (None, 'null')

    So the NaN branch below is defensive, not load-bearing, for anything that came
    through SQLite.  What §8 should say instead is worse in a different way: a NaN
    that reached `update_land_listing(current_bid=nan)` would land as current_bid
    NULL with current_bidder still SET.  That is the orphan shape — _min_next_bid
    then treats the auction as unbid and returns the reserve, and the outbid refund
    at :427 evaluates `int(round(None))` and raises TypeError one line after the
    new bidder was already debited at :425.
    """
    if v is None:
        return None
    if isinstance(v, (int,)):
        if abs(v) >= MAX_EXACT_INT_IN_DOUBLE:
            return "magnitude >= 2^53 (float round-trip is lossy)"
        return None
    if isinstance(v, float):
        if math.isnan(v):
            return "NaN — passes both money guards in _place_bid_core, see FINDINGS §8"
        if math.isinf(v):
            return "Infinity"
        if abs(v) >= MAX_EXACT_INT_IN_DOUBLE:
            return "magnitude >= 2^53 (float cannot represent every integer here)"
        return None
    return f"non-numeric ({type(v).__name__})"


# ══════════════════════════════════════════════════════════════════════════════
# scanning — build the full change set, no writes
# ══════════════════════════════════════════════════════════════════════════════

class Change:
    __slots__ = ("table", "rowid", "pk", "column", "before", "after", "delta",
                 "klass", "party", "party_label", "realized", "anomaly")

    def __init__(self, table, rowid, pk, column, before, after, delta,
                 klass, party, party_label, realized, anomaly=None):
        self.table = table
        self.rowid = rowid
        self.pk = pk
        self.column = column
        self.before = before
        self.after = after
        self.delta = delta
        self.klass = klass              # "LIVE" | "SETTLED"
        self.party = party              # discord id or synthetic key
        self.party_label = party_label  # human description
        self.realized = realized        # True == coins actually change hands
        self.anomaly = anomaly


def classify(table: str, column: str, row: dict, live_bid_ids: set[int]) -> tuple[str, str, str, bool]:
    """-> (class, party_key, party_label, realized)

    realized == True means a coin movement's SIZE changes as a result of editing
    this cell.  There is exactly one such cell in the whole exchange:
    land_listings.current_bid on an active auction, because the outbid /
    cancel / expiry refund at land_exchange.py:427 recomputes the payout from
    this column rather than from what was debited.
    """
    if (table, column) in ALWAYS_SETTLED:
        return ("SETTLED", "record", "record-only (seller already paid at close)", False)

    if table == "land_listings":
        active = (row.get("status") == ACTIVE_STATUS)
        if not active:
            return ("SETTLED", "record", f"record-only (listing status={row.get('status')!r})", False)
        if column == "current_bid":
            bidder = row.get("current_bidder")
            if bidder:
                return ("LIVE", str(bidder),
                        f"bidder {bidder} — held escrow, refund recomputes from this cell", True)
            return ("LIVE", "orphan", "current_bid set with NO current_bidder (anomaly)", False)
        if column == "listing_fee":
            return ("LIVE", str(row.get("seller_id")),
                    f"seller {row.get('seller_id')} — fee already debited, never refunded", False)
        if column == "reserve":
            return ("LIVE", "future-bidder", "unrealised — sets the minimum FIRST bid", False)
        if column == "buy_now":
            return ("LIVE", "future-buyer", "unrealised — sets what an instant-buy costs", False)
        return ("LIVE", "record", "record-only", False)

    if table == "land_bids":
        if row["id"] in live_bid_ids:
            return ("LIVE", str(row.get("bidder_id")),
                    f"bidder {row.get('bidder_id')} — top bid on an active listing (mirrors escrow)", False)
        return ("SETTLED", "record", "record-only (superseded / closed bid log)", False)

    return ("SETTLED", "record", "record-only", False)


def find_live_bid_ids(conn: sqlite3.Connection) -> set[int]:
    """The land_bids rows that mirror an active escrow: the highest-id bid on an
    active listing whose bidder is that listing's current_bidder."""
    if not (table_exists(conn, "land_bids") and table_exists(conn, "land_listings")):
        return set()
    rows = conn.execute("""
        SELECT b.id
          FROM land_bids b
          JOIN land_listings l ON l.id = b.listing_id
         WHERE l.status = ?
           AND l.current_bidder IS NOT NULL
           AND b.bidder_id = l.current_bidder
           AND b.id = (SELECT MAX(b2.id) FROM land_bids b2 WHERE b2.listing_id = l.id)
    """, (ACTIVE_STATUS,)).fetchall()
    return {int(r[0]) for r in rows}


def scan(conn: sqlite3.Connection, policy_name: str, include_settled: bool):
    """Compute the full change set under one policy.  Read-only."""
    fn = POLICIES[policy_name]
    live_bid_ids = find_live_bid_ids(conn)
    changes: list[Change] = []
    anomalies: list[Change] = []
    scanned = 0
    in_scope_cells = 0

    for table, cols in MONEY_COLUMNS.items():
        if not table_exists(conn, table):
            continue
        present = {c["name"] for c in table_info(conn, table)}
        cols = tuple(c for c in cols if c in present)
        if not cols:
            continue
        for r in conn.execute(f"SELECT rowid AS _rid, * FROM {table}").fetchall():
            row = dict(r)
            scanned += 1
            pk = row.get("id", row["_rid"])
            for col in cols:
                v = row.get(col)
                klass, party, label, realized = classify(table, col, row, live_bid_ids)
                if klass == "SETTLED" and not include_settled:
                    continue
                in_scope_cells += 1
                bad = anomaly_of(v)
                if bad:
                    anomalies.append(Change(table, row["_rid"], pk, col, v, None, None,
                                            klass, party, label, realized, bad))
                    continue
                if v is None or is_integral(v):
                    continue
                new = fn(float(v))
                changes.append(Change(table, row["_rid"], pk, col, v, new,
                                      new - float(v), klass, party, label, realized))
    return changes, anomalies, scanned, in_scope_cells


# ══════════════════════════════════════════════════════════════════════════════
# preview
# ══════════════════════════════════════════════════════════════════════════════

def policy_comparison(conn: sqlite3.Connection, include_settled: bool) -> None:
    rule("ROUNDING POLICY COMPARISON")
    say("The same rows under each policy.  'coins moved' counts only cells where a")
    say("coin movement's size actually changes — active current_bid — because that is")
    say("the only column a refund is recomputed from (land_exchange.py:427).")
    say()
    say(f"  {'policy':<12} {'cells changed':>13} {'record delta':>15} {'coins moved':>13}")
    say(f"  {'-'*12} {'-'*13} {'-'*15} {'-'*13}")
    for name in ("half-even", "half-up", "floor"):
        ch, _an, _s, _c = scan(conn, name, include_settled)
        record_delta = sum(c.delta for c in ch)
        moved = sum(c.after - r_half_even(float(c.before)) for c in ch if c.realized)
        marker = "  <- code's existing policy" if name == "half-even" else ""
        say(f"  {name:<12} {len(ch):>13,} {record_delta:>+15,.4f} {moved:>+13,}{marker}")
    say()

    # D4 from LAND_FLOAT_EXPOSURE — the only set where half-up and half-even differ.
    half_cells = []
    live_bid_ids = find_live_bid_ids(conn)
    for table, cols in MONEY_COLUMNS.items():
        if not table_exists(conn, table):
            continue
        present = {c["name"] for c in table_info(conn, table)}
        for r in conn.execute(f"SELECT rowid AS _rid, * FROM {table}").fetchall():
            row = dict(r)
            for col in cols:
                if col not in present:
                    continue
                v = row.get(col)
                if not isinstance(v, float) or math.isnan(v) or math.isinf(v):
                    continue
                if abs(v - math.floor(v) - 0.5) < 1e-12:
                    klass, _p, _l, _rz = classify(table, col, row, live_bid_ids)
                    half_cells.append((table, row.get("id", row["_rid"]), col, v, klass))
    say(f"  exact .5 values (query D4 — the ONLY cells where half-up != half-even): {len(half_cells)}")
    for t, pk, col, v, klass in half_cells[:40]:
        say(f"      {t}#{pk}.{col} = {v!r}   half-even -> {r_half_even(v)}   "
            f"half-up -> {r_half_up(v)}   [{klass}]")
    if len(half_cells) > 40:
        say(f"      ... and {len(half_cells) - 40} more")


def shape_warnings(conn: sqlite3.Connection) -> None:
    """Data shapes that are not this migration's job to fix, but that it would be
    dishonest to rewrite past in silence.  Reported, never touched."""
    if not table_exists(conn, "land_listings"):
        return
    checks = [
        ("active listing with current_bid but NO current_bidder",
         "SELECT id, title, current_bid FROM land_listings "
         "WHERE status='active' AND current_bid IS NOT NULL AND current_bidder IS NULL",
         "coins were debited from somebody and there is no id to refund them to"),
        ("active listing with current_bidder but NULL current_bid",
         "SELECT id, title, current_bidder FROM land_listings "
         "WHERE status='active' AND current_bidder IS NOT NULL AND current_bid IS NULL",
         "the outbid refund evaluates int(round(None)) and raises AFTER the new "
         "bidder is debited (land_exchange.py:425 then :427). This is also the shape "
         "a NaN bid leaves behind, because SQLite stores NaN as NULL"),
        ("listing marked sold with NULL sold_price",
         "SELECT id, title, status FROM land_listings "
         "WHERE status='sold' AND sold_price IS NULL",
         "settled with no record of the figure it settled at"),
        ("non-active listing still holding a current_bidder",
         "SELECT id, title, status, current_bidder FROM land_listings "
         "WHERE status<>'active' AND status<>'sold' AND current_bidder IS NOT NULL",
         "may be an un-refunded escrow, or may just be a stale column — "
         "cross-check coin_ledger before assuming either"),
        ("negative money value",
         "SELECT id, title, reserve, current_bid, buy_now FROM land_listings "
         "WHERE reserve < 0 OR current_bid < 0 OR buy_now < 0 OR listing_fee < 0",
         "no code path should produce one"),
    ]
    # The land_claim valuation key is written from sold_price as str(float(price))
    # (land_exchange.py:384). Rewriting sold_price does NOT rewrite that key, so
    # --include-settled leaves the two disagreeing. Worth knowing before you do it:
    # per FINDINGS the key has no reader in the staged tree, so this may be inert —
    # or it may be feeding the 65% land haircut from cogs/valuation.py.
    valuation_drift = []
    if table_exists(conn, "bot_config"):
        try:
            for k, v in conn.execute(
                    "SELECT key, value FROM bot_config WHERE key LIKE 'valuate:land_claim:%'"):
                mkt = k.split(":", 2)[2]
                row = conn.execute(
                    "SELECT id, sold_price FROM land_listings WHERE market_id=? AND status='sold' "
                    "ORDER BY closed_at DESC LIMIT 1", (mkt,)).fetchone()
                if row and row[1] is not None:
                    try:
                        if abs(float(v) - float(row[1])) > 1e-9 or float(v) != int(float(v)):
                            valuation_drift.append((k, v, row[0], row[1]))
                    except (TypeError, ValueError):
                        valuation_drift.append((k, v, row[0], row[1]))
        except sqlite3.OperationalError:
            pass

    hits = []
    for label, sql, why in checks:
        try:
            rows = conn.execute(sql).fetchall()
        except sqlite3.OperationalError:
            continue
        if rows:
            hits.append((label, rows, why))
    if not hits and not valuation_drift:
        return
    rule("DATA-SHAPE WARNINGS (reported, NOT changed by this migration)")
    if valuation_drift:
        say(f"  {len(valuation_drift)} x valuate:land_claim key holds a non-integer or "
            f"stale figure")
        say("      why it matters: land_exchange.py:384 writes this key as "
            "str(float(sold_price)).")
        say("      Rewriting sold_price does NOT rewrite the key, so --include-settled")
        say("      leaves them disagreeing. FINDINGS could not locate any READER for this")
        say("      key in the staged tree (gather_and_value does not exist there), so this")
        say("      is either inert or it is feeding the 65% land haircut. Check before you")
        say("      decide it does not matter.")
        for k, v, lid, sp in valuation_drift[:10]:
            say(f"      {k} = {v!r}   (land_listings#{lid}.sold_price = {sp!r})")
        say()
    for label, rows, why in hits:
        say(f"  {len(rows)} x {label}")
        say(f"      why it matters: {why}")
        for r in rows[:10]:
            say(f"      {tuple(r)}")
        if len(rows) > 10:
            say(f"      ... and {len(rows) - 10} more")
        say()


def preview(conn: sqlite3.Connection, args, changes, anomalies, scanned, in_scope_cells) -> None:
    rule("ROW COUNTS")
    for table in MONEY_COLUMNS:
        if not table_exists(conn, table):
            say(f"  {table:<16} MISSING")
            continue
        total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        say(f"  {table:<16} {total:>8,} rows")
        if table == "land_listings":
            for st, n in conn.execute(
                    "SELECT status, COUNT(*) FROM land_listings GROUP BY status ORDER BY 2 DESC"):
                tag = "  <- LIVE" if st == ACTIVE_STATUS else "     settled"
                say(f"      status={str(st):<12} {n:>8,}{tag}")
            esc = conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(current_bid),0) FROM land_listings "
                "WHERE status=? AND current_bidder IS NOT NULL", (ACTIVE_STATUS,)).fetchone()
            say(f"      live escrows (active + has bidder): {esc[0]:,}  "
                f"holding {fmt_money(esc[1])} coins")
            if isinstance(esc[1], float) and esc[1] != int(esc[1]):
                say("        (that trailing fraction is SUM() over a REAL column — the total "
                    "escrow")
                say("         figure is itself a float artefact today. After this migration "
                    "it is exact.)")
        if table == "land_bids":
            live_n = len(find_live_bid_ids(conn))
            say(f"      of which mirror a live escrow: {live_n:,}")

    rule("COLUMNS IN SCOPE")
    for table, cols in MONEY_COLUMNS.items():
        if not table_exists(conn, table):
            continue
        info = {c["name"]: c["type"] for c in table_info(conn, table)}
        for col in cols:
            ident = f"{table}.{col}"
            if col not in info:
                say(f"  {ident:<30} MISSING from this database")
                continue
            settled_note = "  (SETTLED by definition)" if (table, col) in ALWAYS_SETTLED else ""
            nonint = conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL "
                f"AND typeof({col}) != 'integer'").fetchone()[0]
            say(f"  {ident:<30} declared {info[col]:<8} "
                f"{nonint:,} rows stored non-integer{settled_note}")
    say()
    say("  NOT converted, on purpose:")
    for table, cols in NOT_MONEY.items():
        for col, why in cols.items():
            say(f"    {table}.{col:<20} {why}")

    rule("SCOPE OF THIS RUN")
    say(f"  rounding policy      : {args.rounding}"
        + ("   (matches the code's existing int(round(x)))" if args.rounding == DEFAULT_POLICY
           else "   *** NOT the code's policy — see the comparison above ***"))
    say(f"  --include-settled    : {'YES — historical rows WILL be rewritten' if args.include_settled else 'no (live rows only)'}")
    if not args.include_settled:
        say("      Settled rows keep their fractional values.  The column is still")
        say("      re-declared INTEGER, and SQLite stores a non-integral value in an")
        say("      INTEGER column as REAL without complaint (verified on 3.45.1), so")
        say("      this is coherent — those rows are simply left as found and are")
        say("      reported by the verification pass as deferred, not as failures.")
    say(f"  cells examined       : {in_scope_cells:,} across {scanned:,} rows")

    rule("EVERY ROW THAT CHANGES")
    if not changes:
        say("  (none — every in-scope money cell is already a whole number)")
    else:
        say(f"  {'table#id.column':<34} {'before':>20} {'after':>16} {'delta':>12}  class  party")
        say(f"  {'-'*34} {'-'*20} {'-'*16} {'-'*12}  -----  -----")
        for c in changes:
            ident = f"{c.table}#{c.pk}.{c.column}"
            say(f"  {ident:<34} {fmt_money(c.before):>20} {fmt_money(c.after):>16} "
                f"{c.delta:>+12.6g}  {c.klass:<6} {c.party_label}")

    shape_warnings(conn)

    rule("COINS CREATED / DESTROYED")
    record_delta = sum(c.delta for c in changes)
    realized = [c for c in changes if c.realized]
    realized_delta = sum(c.after - r_half_even(float(c.before)) for c in realized)
    say(f"  recorded-figure delta (sum of after-before) : {record_delta:+,.6f} coins")
    say("      This is a change to what the TABLE SAYS.  Most of it moves no coins:")
    say("      reserve/buy_now are prices nobody has paid yet, sold_price and the")
    say("      historical bid log are records of payments already made in integers.")
    say()
    say(f"  REALISED delta (coins that actually move)   : {realized_delta:+,} coins")
    say(f"      across {len(realized)} live escrow cell(s).  This is the number that matters.")
    if realized_delta == 0:
        say("      ZERO — every refund will pay exactly what its bidder was debited.")
    elif realized_delta < 0:
        say(f"      NEGATIVE — {abs(realized_delta):,} coins would be DESTROYED: bidders")
        say("      refunded less than they were debited.  Orphaned, not transferred.")
    else:
        say(f"      POSITIVE — {realized_delta:,} coins would be MINTED: bidders refunded")
        say("      more than they were debited.")

    rule("WHO ABSORBS THE FRACTIONS")
    if not changes:
        say("  (nobody — nothing changes)")
    else:
        by_party: dict[tuple, dict] = {}
        for c in changes:
            k = (c.party, c.party_label)
            e = by_party.setdefault(k, {"n": 0, "record": 0.0, "realized": 0})
            e["n"] += 1
            e["record"] += c.delta
            if c.realized:
                e["realized"] += c.after - r_half_even(float(c.before))
        say(f"  {'party':<46} {'cells':>6} {'record Δ':>12} {'coins Δ':>9}")
        say(f"  {'-'*46} {'-'*6} {'-'*12} {'-'*9}")
        for (_p, label), e in sorted(by_party.items(), key=lambda kv: -abs(kv[1]["record"])):
            say(f"  {label[:46]:<46} {e['n']:>6} {e['record']:>+12.4f} {e['realized']:>+9,}")

    if anomalies:
        rule("!!  ANOMALOUS VALUES — THESE BLOCK --apply")
        say("  These cannot be rounded at all.  int(round(NaN)) raises; a value past")
        say("  2^53 cannot be round-tripped through a double without losing coins.")
        say()
        for c in anomalies:
            say(f"  {c.table}#{c.pk}.{c.column} = {c.before!r}   [{c.klass}]  {c.anomaly}")
            say(f"      inspect: SELECT * FROM {c.table} WHERE rowid={c.rowid};")
        say()
        say("  --apply refuses while any of these exist.  Fix them by hand, or pass")
        say("  --skip-anomalous-rows to copy those cells through VERBATIM (they will")
        say("  be listed as known-unclean by the verification pass).")

    rule("FOREIGN KEYS POINTING AT THE TABLES BEING REBUILT")
    any_fk = False
    for table in MONEY_COLUMNS:
        if not table_exists(conn, table):
            continue
        refs = inbound_fk_refs(conn, table)
        if not refs:
            say(f"  {table}: no other table references it — a rebuild cannot cascade.")
            continue
        any_fk = True
        say(f"  {table}: referenced by {len(refs)} foreign key(s):")
        for child, col, on_delete in refs:
            danger = "  <-- CASCADES ON DROP" if on_delete in ("CASCADE", "SET NULL",
                                                               "SET DEFAULT") else ""
            say(f"      {child}.{col}  ON DELETE {on_delete}{danger}")
    if any_fk:
        say()
        say("  A table rebuild DROPs the old table. With foreign_keys=ON, DROP TABLE runs")
        say("  an implicit DELETE FROM first, which FIRES these actions — defer_foreign_keys")
        say("  defers the CHECK, not the ACTION. This script therefore sets foreign_keys=OFF")
        say("  for the rebuild (SQLite's own documented procedure) and proves nothing was")
        say("  lost by fingerprinting every table's row count before and after, inside the")
        say("  transaction, plus foreign_key_check with enforcement restored afterwards.")

    rule("WHAT --apply WOULD DO")
    step = 1
    say(f"  {step}. Fingerprint every table's row count + non-money content.")
    step += 1
    say(f"  {step}. PRAGMA foreign_keys=OFF (before BEGIN — inside a transaction it is a")
    say("     silent no-op), then BEGIN IMMEDIATE, then PRAGMA defer_foreign_keys=ON.")
    for table in MONEY_COLUMNS:
        if table_exists(conn, table):
            step += 1
            say(f"  {step}. Rebuild {table}: CREATE {table}{TMP_SUFFIX} from the LIVE")
            say("     sqlite_master CREATE text with only the money columns' type token")
            say("     rewritten; verify column-for-column; copy rows with values computed")
            say(f"     in PYTHON (never SQLite's ROUND, which is half-away); DROP {table};")
            say("     RENAME; recreate indexes and triggers; restore sqlite_sequence.")
    step += 1
    say(f"  {step}. Re-fingerprint and compare INSIDE the transaction: any row-count or")
    say("     non-money content change rolls the whole thing back.")
    step += 1
    say(f"  {step}. PRAGMA foreign_key_check, PRAGMA integrity_check — rollback on any hit.")
    step += 1
    say(f"  {step}. Record `{CONFIG_KEY}` in bot_config, and a `{MIGRATION_ID}` row in")
    say("     ledger_migrations IF that table already exists (never creates it).")
    step += 1
    say(f"  {step}. COMMIT, restore PRAGMA foreign_keys=ON, re-run foreign_key_check with")
    say("     enforcement on, then re-read every money column and assert integrality.")


# ══════════════════════════════════════════════════════════════════════════════
# the rebuild
# ══════════════════════════════════════════════════════════════════════════════

def rewrite_create_sql(orig_sql: str, table: str, tmp_name: str, money_cols: tuple[str, ...]) -> str:
    """Take the LIVE CREATE TABLE text and change ONLY two things: the table name,
    and the declared type of each money column.  Everything else — unknown columns
    added by a later _migrate(), constraints, defaults, collations — rides along
    untouched.  A hardcoded CREATE would drop whatever it did not know about."""
    sql = orig_sql

    # Table name.  Handle quoted and unquoted forms; anchor on CREATE TABLE.
    pat = re.compile(
        r'(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?)(["\'`\[]?)' + re.escape(table) + r'(["\'`\]]?)',
        re.IGNORECASE)
    sql, n = pat.subn(lambda m: m.group(1) + tmp_name, sql, count=1)
    if n != 1:
        raise RuntimeError(f"could not locate the table name in the CREATE text for {table!r}")

    # Column types.  Match the column at the start of a definition (after '(' or ','
    # at a line start) followed by REAL.
    for col in money_cols:
        cpat = re.compile(
            r'(^|[(,]\s*\n?\s*)(["\'`\[]?)' + re.escape(col) + r'(["\'`\]]?)(\s+)REAL\b',
            re.IGNORECASE | re.MULTILINE)
        sql, cn = cpat.subn(lambda m: f"{m.group(1)}{m.group(2)}{col}{m.group(3)}{m.group(4)}INTEGER", sql)
        if cn == 0:
            # Already INTEGER, or the column is absent — verification below decides.
            pass
        elif cn > 1:
            raise RuntimeError(f"ambiguous rewrite: {col!r} matched {cn} times in {table!r}")
    return sql


def verify_rebuilt_shape(conn, table: str, tmp_name: str, money_cols: tuple[str, ...]) -> None:
    """Abort unless the new table is the old table with exactly the money columns'
    declared type changed.  Compares name, order, NOT NULL, default and PK flag."""
    old = table_info(conn, table)
    new = table_info(conn, tmp_name)
    if len(old) != len(new):
        raise RuntimeError(
            f"{table}: rebuilt table has {len(new)} columns, original has {len(old)} — aborting")
    for o, nw in zip(old, new):
        if o["name"] != nw["name"]:
            raise RuntimeError(f"{table}: column order changed ({o['name']!r} -> {nw['name']!r})")
        if (o["notnull"], o["dflt"], o["pk"]) != (nw["notnull"], nw["dflt"], nw["pk"]):
            raise RuntimeError(
                f"{table}.{o['name']}: constraint drift "
                f"{(o['notnull'], o['dflt'], o['pk'])} -> {(nw['notnull'], nw['dflt'], nw['pk'])}")
        if o["name"] in money_cols:
            if nw["type"].upper() != "INTEGER":
                raise RuntimeError(
                    f"{table}.{o['name']}: expected INTEGER after rewrite, got {nw['type']!r}")
        else:
            if o["type"] != nw["type"]:
                raise RuntimeError(
                    f"{table}.{o['name']}: type changed unexpectedly "
                    f"{o['type']!r} -> {nw['type']!r} — this column is not money")


def rebuild_table(conn: sqlite3.Connection, table: str, money_cols: tuple[str, ...],
                  changes_by_cell: dict, skip_cells: set, report: list) -> None:
    """Rebuild one table inside the caller's open transaction.

    THE CASCADE TRAP — found by testing this script, not by reading about it.

    The first version of this function ran with `PRAGMA foreign_keys=ON` and
    `defer_foreign_keys=ON`, on the assumption that deferring the check was what
    made a DROP-and-RENAME rebuild safe with enforcement left on.  It is not.
    Against a fixture where `land_bids.listing_id REFERENCES land_listings(id) ON
    DELETE CASCADE`, this happened:

        land_listings: 15 rows copied
        land_bids:      0 rows copied      <- all nine bids destroyed
        ...
        APPLIED and VERIFIED

    `DROP TABLE` with foreign_keys=ON performs an implicit `DELETE FROM` first,
    and that DELETE fires ON DELETE actions.  `defer_foreign_keys` defers when
    constraints are CHECKED; it does not disable the cascading ACTION.  Verified
    minimally on 3.45.1: parent+child with ON DELETE CASCADE, BEGIN,
    defer_foreign_keys=ON, DROP parent -> child count 1 -> 0.

    So the caller sets `PRAGMA foreign_keys=OFF` for the rebuild — which is what
    SQLite's own documented 12-step ALTER TABLE procedure says to do — and that
    pragma must be set BEFORE `BEGIN`, because inside a transaction it is a silent
    no-op (also verified).  The compensating controls, because "we turned
    enforcement off" is the exact shape of the failure that motivated rule 7:

      * every table's row count and every rebuilt table's non-money content are
        fingerprinted before the transaction and asserted inside it, so this
        specific failure now rolls back instead of reporting success;
      * `PRAGMA foreign_key_check` runs inside the transaction AND again after
        COMMIT with enforcement restored to ON;
      * the staged land schema declares no such FK, so on the real database this
        is defence against a constraint someone adds later, not a live hazard —
        but the preview reports any inbound reference it finds, with its ON
        DELETE action, so you can see whether it applies to you.
    """
    tmp = table + TMP_SUFFIX
    conn.execute(f"DROP TABLE IF EXISTS {tmp}")

    orig_sql = table_sql(conn, table)
    new_sql = rewrite_create_sql(orig_sql, table, tmp, money_cols)
    conn.execute(new_sql)
    verify_rebuilt_shape(conn, table, tmp, money_cols)

    cols = [c["name"] for c in table_info(conn, table)]
    collist = ", ".join(cols)
    placeholders = ", ".join("?" * len(cols))

    src = conn.execute(f"SELECT rowid AS _rid, {collist} FROM {table}").fetchall()
    written = 0
    for r in src:
        row = dict(r)
        rid = row["_rid"]
        vals = []
        for c in cols:
            v = row[c]
            key = (table, rid, c)
            if key in skip_cells:
                vals.append(v)                       # anomalous — verbatim
            elif key in changes_by_cell:
                vals.append(changes_by_cell[key])    # Python-computed integer
            elif c in money_cols and isinstance(v, float) and is_integral(v):
                vals.append(int(v))                  # 1000.0 -> 1000, no value change
            else:
                vals.append(v)
        conn.execute(f"INSERT INTO {tmp} ({collist}) VALUES ({placeholders})", vals)
        written += 1
        # Rule 2 — a marker per row, not one after the loop.  This runs inside a
        # single transaction so the marker is progress, not a claim of durability;
        # the durable claim is made once, after COMMIT.
        report.append(f"      copied {table} rowid={rid} ({written}/{len(src)})")

    # sqlite_sequence.  If rows were ever deleted, max(id) < seq, and letting the
    # sequence reset would REUSE a listing id.  Ledger reasons are keyed on that id
    # (`realestate:sale:<id>`, land_exchange.py:375) so a reused id makes two
    # different sales indistinguishable in coin_ledger.
    seq_row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone() \
        if table_exists(conn, "sqlite_sequence") else None
    old_seq = int(seq_row[0]) if seq_row else None

    idx = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
        (table,)).fetchall()
    trg = conn.execute(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name=? AND sql IS NOT NULL",
        (table,)).fetchall()

    conn.execute(f"DROP TABLE {table}")
    conn.execute(f"ALTER TABLE {tmp} RENAME TO {table}")

    for name, sql in idx:
        conn.execute(sql)
        report.append(f"      recreated index {name}")
    for name, sql in trg:
        conn.execute(sql)
        report.append(f"      recreated trigger {name}")

    if old_seq is not None:
        cur_row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
        cur_seq = int(cur_row[0]) if cur_row else None
        if cur_seq is None:
            conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?,?)", (table, old_seq))
            report.append(f"      restored sqlite_sequence({table}) = {old_seq}")
        elif cur_seq < old_seq:
            conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name=?", (old_seq, table))
            report.append(f"      restored sqlite_sequence({table}) {cur_seq} -> {old_seq} "
                          f"(prevents listing-id reuse)")

    report.append(f"    {table}: {written:,} rows copied, "
                  f"{sum(1 for k in changes_by_cell if k[0] == table):,} cells rounded")


# ══════════════════════════════════════════════════════════════════════════════
# recording
# ══════════════════════════════════════════════════════════════════════════════

def already_applied(conn: sqlite3.Connection) -> dict | None:
    if not table_exists(conn, "bot_config"):
        return None
    row = conn.execute("SELECT value FROM bot_config WHERE key=?", (CONFIG_KEY,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return {"raw": row[0]}


def record_migration(conn: sqlite3.Connection, payload: dict, report: list) -> None:
    if table_exists(conn, "bot_config"):
        conn.execute(
            "INSERT INTO bot_config (key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (CONFIG_KEY, json.dumps(payload, sort_keys=True)))
        report.append(f"    recorded bot_config[{CONFIG_KEY}]")
    else:
        report.append("    !! bot_config missing — migration NOT recorded there")

    # Join the existing register only if it is already there.  Never create it:
    # that register belongs to ledger_migrate.py, and a second script conjuring
    # it would be exactly the "invent a second mechanism" mistake.
    if table_exists(conn, "ledger_migrations"):
        conn.execute("INSERT OR IGNORE INTO ledger_migrations (id) VALUES (?)", (MIGRATION_ID,))
        report.append(f"    recorded ledger_migrations[{MIGRATION_ID}]")
    else:
        report.append("    ledger_migrations absent (ledger_migrate.py has not run here) "
                      "— not created, by design")


# ══════════════════════════════════════════════════════════════════════════════
# verification
# ══════════════════════════════════════════════════════════════════════════════

def verify(conn: sqlite3.Connection, include_settled: bool,
           skipped: set | None = None) -> int:
    """Re-read from disk and assert.  Returns the number of REAL failures.

    Cells deliberately passed over by --skip-anomalous-rows are reported as
    known-unclean, not as failures — the operator already said they knew. Rule 2
    applies here too: this walks and reports every row rather than trusting that
    the copy loop finishing means the copy loop was right."""
    skipped = skipped or set()
    rule("VERIFICATION PASS (re-read after commit)")
    failures = 0
    deferred = 0
    known_unclean = 0
    live_bid_ids = find_live_bid_ids(conn)

    for table, cols in MONEY_COLUMNS.items():
        if not table_exists(conn, table):
            continue
        info = {c["name"]: c["type"] for c in table_info(conn, table)}
        for col in cols:
            if col not in info:
                continue
            declared_ok = info[col].upper() == "INTEGER"
            ident = f"{table}.{col}"
            say(f"  {ident:<30} declared {info[col]:<8} "
                f"{'OK' if declared_ok else 'FAIL — still not INTEGER'}")
            if not declared_ok:
                failures += 1

        for r in conn.execute(f"SELECT rowid AS _rid, * FROM {table}").fetchall():
            row = dict(r)
            pk = row.get("id", row["_rid"])
            for col in cols:
                if col not in info:
                    continue
                v = row.get(col)
                if v is None:
                    continue
                klass, _p, _l, _rz = classify(table, col, row, live_bid_ids)
                st = stored_typeof(conn, table, col, row["_rid"])
                clean = (st == "integer")
                if clean:
                    continue
                if (table, row["_rid"], col) in skipped:
                    known_unclean += 1
                    say(f"    known-unclean (--skip-anomalous-rows): {table}#{pk}.{col} "
                        f"= {fmt_money(v)} stored as {st}")
                elif klass == "SETTLED" and not include_settled:
                    deferred += 1
                    if deferred <= 20:
                        say(f"    deferred (settled, not in scope): {table}#{pk}.{col} "
                            f"= {fmt_money(v)} stored as {st}")
                else:
                    failures += 1
                    say(f"    FAIL: {table}#{pk}.{col} = {fmt_money(v)} stored as {st} [{klass}]")
    if deferred > 20:
        say(f"    ... and {deferred - 20} more deferred settled cells")
    say()
    say(f"  in-scope failures: {failures}")
    say(f"  deferred settled cells (expected without --include-settled): {deferred}")
    say(f"  known-unclean cells you chose to skip: {known_unclean}")
    return failures


# ══════════════════════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════════════════════

def open_db(path: Path, busy_ms: int) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)  # manual transactions
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=%d" % busy_ms)
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def report_pragmas(conn: sqlite3.Connection) -> int:
    rule("PRAGMA STATE (observed, not assumed)")
    fk = int(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    dfk = int(conn.execute("PRAGMA defer_foreign_keys").fetchone()[0])
    jm = conn.execute("PRAGMA journal_mode").fetchone()[0]
    bt = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    legacy = int(conn.execute("PRAGMA legacy_alter_table").fetchone()[0])
    say(f"  sqlite version        : {sqlite3.sqlite_version}")
    say(f"  foreign_keys          : {'ON' if fk else 'OFF'}   (production is ON, Restocker_db.py:26)")
    say(f"  defer_foreign_keys    : {'ON' if dfk else 'OFF'}  (set ON inside the write txn)")
    say(f"  journal_mode          : {jm}   (production WAL, Restocker_db.py:24)")
    say(f"  busy_timeout          : {bt} ms")
    say(f"  legacy_alter_table    : {'ON' if legacy else 'OFF'}")
    fkc = conn.execute("PRAGMA foreign_key_check").fetchall()
    say(f"  foreign_key_check     : {'CLEAN' if not fkc else f'{len(fkc)} PRE-EXISTING VIOLATION(S)'}")
    for v in fkc[:10]:
        say(f"      {tuple(v)}")
    if not fk:
        raise SystemExit(
            "\n✗ Refusing to run: PRAGMA foreign_keys could not be turned ON, so this run\n"
            "  would not reproduce production's constraint checking.  That is exactly how\n"
            "  the last migration passed on a copy and failed in production.")
    return len(fkc)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="land_money_migrate.py",
        description="Convert the Land Exchange money columns from REAL to INTEGER coins. "
                    "Dry run unless --apply is given.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, metavar="PATH",
                    help="Path to the SQLite database. REQUIRED — there is no default and "
                         "no guess. Point it at a COPY first.")
    ap.add_argument("--apply", action="store_true",
                    help="Actually write. Without this the script only reports.")
    ap.add_argument("--backup", metavar="PATH", nargs="?", const="AUTO",
                    help="Take a backup before applying. With no value, writes "
                         "<db>.land_money.<UTC timestamp>.bak next to the database. "
                         "Uses SQLite's online backup API, which is WAL-safe (a plain "
                         "file copy is not).")
    ap.add_argument("--no-backup", action="store_true",
                    help="Apply without a backup. Must be passed explicitly.")
    ap.add_argument("--include-settled", action="store_true",
                    help="Also rewrite historical settled figures: sold_price, every "
                         "money column on a closed listing, and the superseded bid log. "
                         "Changing a historical figure is a different risk class from "
                         "changing a live one, so it is opt-in.")
    ap.add_argument("--rounding", choices=sorted(POLICIES), default=DEFAULT_POLICY,
                    help=f"Rounding policy (default: {DEFAULT_POLICY}, which is what "
                         f"Python's round() does and therefore what every coin movement "
                         f"in land_exchange.py has already used).")
    ap.add_argument("--skip-anomalous-rows", action="store_true",
                    help="Copy NaN / Inf / >=2^53 cells through verbatim instead of "
                         "refusing to apply.")
    ap.add_argument("--busy-timeout", type=int, default=10000, metavar="MS",
                    help="busy_timeout in ms (default 10000; production uses 5000).")
    args = ap.parse_args(argv)

    db_path = Path(args.db).expanduser()
    say("=" * 78)
    say(" land_money_migrate.py — Land Exchange money columns: REAL -> INTEGER")
    say(f" migration id : {MIGRATION_ID}")
    say(f" mode         : {'APPLY (will write)' if args.apply else 'DRY RUN (no writes)'}")
    say(f" database     : {db_path}")
    say(f" started      : {utcnow_iso()} UTC")
    say("=" * 78)

    if not db_path.exists():
        raise SystemExit(f"\n✗ {db_path} does not exist. This script never creates a database.")
    if db_path.is_dir():
        raise SystemExit(f"\n✗ {db_path} is a directory.")

    wal = db_path.with_name(db_path.name + "-wal")
    if wal.exists() and wal.stat().st_size > 0:
        say(f"\n  NOTE: {wal.name} is {wal.stat().st_size:,} bytes — something has this")
        say("  database open, or it was not cleanly closed. If the bot is running, stop it.")

    conn = open_db(db_path, args.busy_timeout)
    try:
        pre_fk_violations = report_pragmas(conn)

        missing = [t for t in MONEY_COLUMNS if not table_exists(conn, t)]
        if len(missing) == len(MONEY_COLUMNS):
            raise SystemExit(
                f"\n✗ {db_path} has no land_listings and no land_bids table — this is not "
                f"the Land Exchange database.\n"
                f"  (Every restocker.db fixture I was given in earlier rounds also had no "
                f"land_* tables. Confirm you are pointing at the live file.)")
        if missing:
            say(f"\n  NOTE: table(s) absent, skipped: {', '.join(missing)}")

        prior = already_applied(conn)

        # ── idempotency ───────────────────────────────────────────────────────
        changes, anomalies, scanned, cells = scan(conn, args.rounding, args.include_settled)
        all_declared_int = all(
            c["type"].upper() == "INTEGER"
            for t, cols in MONEY_COLUMNS.items() if table_exists(conn, t)
            for c in table_info(conn, t) if c["name"] in cols)

        if prior:
            rule("ALREADY RECORDED")
            say(f"  bot_config[{CONFIG_KEY}] says this migration ran:")
            for k in sorted(prior):
                say(f"      {k:<20} {prior[k]}")

        # Anomalies deliberately do NOT block the no-op: they are cells this
        # migration never touches, so their presence is a standing note, not
        # pending work. Letting them block it would make every subsequent run
        # rebuild both tables for nothing — real risk, zero change.
        if all_declared_int and not changes:
            rule("NO-OP")
            say("  Every in-scope money column is already declared INTEGER and every")
            say("  in-scope value is already a whole number. Nothing to do.")
            say(f"  Scope of this check: rounding={args.rounding}, "
                f"include_settled={bool(args.include_settled)}")
            if not args.include_settled:
                nonint_settled = 0
                for table, cols in MONEY_COLUMNS.items():
                    if not table_exists(conn, table):
                        continue
                    present = {c["name"] for c in table_info(conn, table)}
                    for col in cols:
                        if col not in present:
                            continue
                        nonint_settled += conn.execute(
                            f"SELECT COUNT(*) FROM {table} WHERE {col} IS NOT NULL "
                            f"AND typeof({col}) != 'integer'").fetchone()[0]
                say(f"  Settled rows were NOT examined. {nonint_settled} money cell(s) in")
                say("  these tables still hold a non-integer value; re-run with")
                say("  --include-settled to see and price them.")
            if anomalies:
                say()
                say(f"  {len(anomalies)} cell(s) remain unroundable and were never in scope:")
                for c in anomalies:
                    say(f"      {c.table}#{c.pk}.{c.column} = {c.before!r}  — {c.anomaly}")
                say("  These need a human decision, not a migration.")
            say()
            say("  This run changed nothing, and did not open a write transaction.")
            if args.apply:
                say("  --apply was passed and was correctly ignored: there is nothing to apply.")
            return 0

        # ── preview, always ───────────────────────────────────────────────────
        policy_comparison(conn, args.include_settled)
        preview(conn, args, changes, anomalies, scanned, cells)

        if not args.apply:
            rule("DRY RUN COMPLETE")
            say("  Nothing was written. These are figures, not intentions — read them,")
            say("  then re-run with --apply --backup if the REALISED delta is what you")
            say("  expect it to be.")
            say()
            say("  Suggested next command:")
            extra = " --include-settled" if args.include_settled else ""
            say(f"    python3 land_money_migrate.py --db {db_path} --backup --apply{extra}")
            return 0

        # ── apply guards ──────────────────────────────────────────────────────
        rule("APPLY")
        if not args.backup and not args.no_backup:
            raise SystemExit(
                "\n✗ Refusing to --apply without a backup.\n"
                "  Pass --backup (recommended) or --no-backup to say you meant it.")
        if anomalies and not args.skip_anomalous_rows:
            raise SystemExit(
                f"\n✗ Refusing to --apply: {len(anomalies)} money cell(s) cannot be rounded "
                f"(NaN / Infinity / >= 2^53).\n"
                f"  They are listed above. Fix them by hand, or pass "
                f"--skip-anomalous-rows to copy them through untouched.")
        if pre_fk_violations:
            raise SystemExit(
                f"\n✗ Refusing to --apply: {pre_fk_violations} foreign key violation(s) "
                f"already exist in this database.\n"
                f"  A rebuild would commit with them still there and you would not be able "
                f"to tell whether this migration caused them. Resolve them first.")
        if args.rounding != DEFAULT_POLICY:
            say(f"  !! rounding={args.rounding} is NOT the policy the code has used. See the")
            say("     comparison above for the exact coin delta this choice costs.")

        backup_path = None
        if args.backup:
            if args.backup == "AUTO":
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                backup_path = db_path.with_name(f"{db_path.name}.land_money.{stamp}.bak")
            else:
                backup_path = Path(args.backup).expanduser()
            if backup_path.exists():
                raise SystemExit(f"\n✗ Backup target already exists: {backup_path}")
            bconn = sqlite3.connect(str(backup_path))
            try:
                conn.backup(bconn)          # WAL-safe, unlike shutil.copy of the .db alone
                bconn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                bconn.close()
            size = backup_path.stat().st_size
            say(f"  backup written: {backup_path}  ({size:,} bytes)")
            # Prove the backup is readable before touching the original.
            vconn = sqlite3.connect(str(backup_path))
            try:
                ic = vconn.execute("PRAGMA integrity_check").fetchone()[0]
                nl = vconn.execute("SELECT COUNT(*) FROM land_listings").fetchone()[0] \
                    if table_exists(vconn, "land_listings") else 0
                say(f"  backup verified: integrity_check={ic}, land_listings={nl:,} rows")
                if ic != "ok":
                    raise SystemExit("\n✗ Backup failed integrity_check. Not applying.")
            finally:
                vconn.close()
        else:
            say("  !! NO BACKUP (--no-backup). If this goes wrong there is nothing to")
            say("     restore from except the panel's own nightly, if it has one.")

        changes_by_cell = {(c.table, c.rowid, c.column): c.after for c in changes}
        skip_cells = {(c.table, c.rowid, c.column) for c in anomalies}
        report: list[str] = []

        before_snap = snapshot(conn)
        say(f"  snapshot taken: {sum(before_snap['counts'].values()):,} rows across "
            f"{len(before_snap['counts'])} tables, content fingerprinted for "
            f"{', '.join(sorted(before_snap['content']))}")

        # PRAGMA foreign_keys is a SILENT NO-OP inside a transaction (verified on
        # 3.45.1), so enforcement has to be settled before BEGIN. It must go OFF:
        # see rebuild_table's docstring — with it ON, DROP TABLE fires ON DELETE
        # CASCADE and eats the children. defer_foreign_keys does NOT prevent that.
        conn.execute("PRAGMA foreign_keys=OFF")
        fk_off = int(conn.execute("PRAGMA foreign_keys").fetchone()[0]) == 0
        if not fk_off:
            raise SystemExit("\n✗ Could not disable foreign_keys for the rebuild. Aborting.")
        say("  foreign_keys temporarily OFF for the rebuild (SQLite's documented")
        say("  table-rebuild procedure requires it; DROP TABLE would otherwise cascade).")
        say("  It is restored to ON after COMMIT, and foreign_key_check is run twice.")

        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("PRAGMA defer_foreign_keys=ON")
            dfk = int(conn.execute("PRAGMA defer_foreign_keys").fetchone()[0])
            say(f"  transaction open; defer_foreign_keys={'ON' if dfk else 'OFF'}, "
                f"foreign_keys={'ON' if int(conn.execute('PRAGMA foreign_keys').fetchone()[0]) else 'OFF'}")
            if not dfk:
                raise RuntimeError("defer_foreign_keys would not turn ON — aborting")

            for table, cols in MONEY_COLUMNS.items():
                if not table_exists(conn, table):
                    continue
                present = {c["name"] for c in table_info(conn, table)}
                rebuild_table(conn, table, tuple(c for c in cols if c in present),
                              changes_by_cell, skip_cells, report)

            # The guard that matters. Every table's row count and every rebuilt
            # table's non-money content, compared against the pre-transaction
            # snapshot, INSIDE the transaction so a mismatch rolls back.
            after_snap = snapshot(conn)
            problems = compare_snapshots(before_snap, after_snap)
            if problems:
                raise RuntimeError("data-preservation check FAILED:\n      "
                                   + "\n      ".join(problems))
            say(f"  data preserved: all {len(before_snap['counts'])} table row counts "
                f"unchanged, non-money content fingerprints match")

            fkc = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fkc:
                raise RuntimeError(f"foreign_key_check found {len(fkc)} violation(s): {fkc[:5]}")

            record_migration(conn, {
                "migration_id": MIGRATION_ID,
                "applied_at": utcnow_iso(),
                "rounding": args.rounding,
                "include_settled": bool(args.include_settled),
                "cells_rounded": len(changes),
                "cells_skipped_anomalous": len(anomalies),
                "realised_coin_delta": sum(
                    c.after - r_half_even(float(c.before)) for c in changes if c.realized),
                "recorded_figure_delta": round(sum(c.delta for c in changes), 6),
                "backup": str(backup_path) if backup_path else None,
                "tool": "land_money_migrate.py",
            }, report)

            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            conn.execute("PRAGMA foreign_keys=ON")
            say()
            for line in report[-10:]:
                say(line)
            say(f"  foreign_keys restored to "
                f"{'ON' if int(conn.execute('PRAGMA foreign_keys').fetchone()[0]) else 'OFF'}")
            raise SystemExit(f"\n✗ ROLLED BACK — nothing was changed.\n  {type(e).__name__}: {e}")

        say()
        for line in report:
            if line.startswith("      copied"):
                continue      # per-row markers kept in the report, summarised here
            say(line)
        per_row = sum(1 for line in report if line.startswith("      copied"))
        say(f"    ({per_row:,} per-row progress markers recorded during the copy)")

        # Enforcement back on, then check again WITH it on — production's state.
        conn.execute("PRAGMA foreign_keys=ON")
        fk_back = int(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        say(f"    foreign_keys restored: {'ON' if fk_back else 'OFF !!'}")
        fkc2 = conn.execute("PRAGMA foreign_key_check").fetchall()
        say(f"    foreign_key_check with enforcement ON: "
            f"{'CLEAN' if not fkc2 else f'{len(fkc2)} VIOLATION(S) — RESTORE THE BACKUP'}")
        for v in fkc2[:10]:
            say(f"      {tuple(v)}")
        ic = conn.execute("PRAGMA integrity_check").fetchone()[0]
        say(f"    integrity_check: {ic}")
        if ic != "ok":
            say("    !! integrity_check is NOT ok — restore the backup.")

        failures = verify(conn, args.include_settled, skip_cells)

        rule("RESULT")
        if failures == 0 and ic == "ok" and not fkc2 and fk_back:
            say("  APPLIED and VERIFIED.")
            say(f"  Recorded as {MIGRATION_ID}. Re-running is a no-op.")
            if backup_path:
                say(f"  Rollback: stop the bot, replace the database with {backup_path.name}.")
            return 0
        say(f"  APPLIED WITH {failures} VERIFICATION FAILURE(S).")
        if backup_path:
            say(f"  Consider restoring {backup_path}.")
        return 2
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
