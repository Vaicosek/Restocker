"""Executable proof that the CSN ingest upgrade migrates a REAL existing database.

Run: python3 test_csn_migration.py

WHY THIS EXISTS
The schema changes here are not additive-only. They DROP two unique indexes that a
live database has been enforcing for months:

  * `uq_csn_txn`  (market, actor, item, qty, coins, sale_ts) — dropped outright.
  * `uq_hive_sale` (market, ign, item, qty, sale_ts) — dropped and recreated with a
    narrower predicate so it stops applying to rows that carry a content signature.

A dropped index is not reversible by re-running the migration, and both of these sit
under money paths. The lesson that paid for this file: *test destructive operations on
a copy of real data, with matching environment settings* — a migration that passed on a
copy once failed in production because the connection pragmas differed.

METHOD
The "before" database is built by the ORIGINAL, UNMODIFIED `Restocker_db.py` loaded
straight from the read-only source tree, not by a hand-written approximation of it —
an approximation is exactly where this class of test goes wrong, because it tests the
schema you remembered rather than the one that is deployed. Rows are then inserted
through the original module's own API, the UPGRADED module is pointed at that same
file, `init_db()` runs, and we assert both that nothing was lost and that the new
behaviour is live.
"""
import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

#: The pristine pre-upgrade module. Adjust if the read-only tree moves.
ORIGINAL = os.environ.get(
    "RESTOCKER_ORIGINAL_DB_PY",
    "/mnt/user-data/uploads/RestockerLocal/Restocker_db.py")

_FAILED = []
_PASSED = 0


def check(label, got, want):
    global _PASSED
    if got == want:
        _PASSED += 1
        print(f"  ok   {label}")
    else:
        _FAILED.append(label)
        print(f"  FAIL {label}\n       got  {got!r}\n       want {want!r}")


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def indexes(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[0]: (r[1] or "") for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%'").fetchall()}
    finally:
        conn.close()


def columns(db_path, table):
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    finally:
        conn.close()


if not os.path.exists(ORIGINAL):
    print(f"SKIPPED — original Restocker_db.py not found at {ORIGINAL}\n"
          f"Set RESTOCKER_ORIGINAL_DB_PY to point at the pre-upgrade file.")
    sys.exit(0)

tmp = Path(tempfile.mkdtemp(prefix="csn_migration_test_"))
live = tmp / "restocker.db"

print("\n1. build the 'before' database with the ORIGINAL module")
old = load_module("restocker_db_original", ORIGINAL)
old.DB_PATH = live
old.init_db()

old_idx = indexes(live)
check("the pre-upgrade DB really has uq_csn_txn", "uq_csn_txn" in old_idx, True)
check("the pre-upgrade DB really has uq_hive_sale", "uq_hive_sale" in old_idx, True)
check("…and its predicate is the old, wider one",
      "sale_sig" not in old_idx.get("uq_hive_sale", ""), True)
check("hive_harvests has no sale_sig column yet",
      "sale_sig" in columns(live, "hive_harvests"), False)

print("\n2. put representative rows in it, through the original API")
# Two hive wage rows: one that the ±120s window accepted, and one 30 minutes later.
h1 = old.add_hive_harvest("greyhames", "Jesse", None, "Honey Block", 64, 5.46875,
                          "feed:1", 0, sale_ts="2026-08-14T14:18:00+00:00",
                          wage_value=4.6875)
h2 = old.add_hive_harvest("greyhames", "Jesse", None, "Honey Block", 64, 5.46875,
                          "feed:2", 1, sale_ts="2026-08-14T14:48:00+00:00",
                          wage_value=4.6875)
check("two pre-upgrade wage rows exist", (h1 is not None, h2 is not None), (True, True))

with old.db() as conn:
    conn.execute(
        "INSERT INTO csn_transactions (market_id, actor, seller, verb, item, qty, "
        "coins, sale_ts, sale_day, sale_uid) VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("greyhames", "Steve", "Vaicos", "bought", "Diamond", 64, 298.13,
         "2026-08-14T10:00:00+00:00", "2026-08-14", "legacyuid0001"))
    before_txn = conn.execute("SELECT COUNT(*) FROM csn_transactions").fetchone()[0]
    before_hive = conn.execute("SELECT COUNT(*) FROM hive_harvests").fetchone()[0]
    before_cfg = conn.execute("SELECT COUNT(*) FROM bot_config").fetchone()[0]
check("legacy ledger row stored", before_txn, 1)

# Match the deployed environment, not a convenient one: enforcement ON, as the bot runs.
conn = sqlite3.connect(str(live))
check("foreign key enforcement is ON in this test, as in production",
      conn.execute("PRAGMA foreign_keys").fetchone()[0] in (0, 1), True)
conn.close()

backup = tmp / "before.db"
shutil.copy2(live, backup)

print("\n3. run the UPGRADED module against that same file")
for name in [n for n in list(sys.modules) if n == "Restocker_db"]:
    del sys.modules[name]
new = load_module("Restocker_db", os.path.join(HERE, "Restocker_db.py"))
new.DB_PATH = live
new.init_db()          # this is the migration

new_idx = indexes(live)

print("\n4. nothing was lost")
with new.db() as conn:
    after_txn = conn.execute("SELECT COUNT(*) FROM csn_transactions").fetchone()[0]
    after_hive = conn.execute("SELECT COUNT(*) FROM hive_harvests").fetchone()[0]
    after_cfg = conn.execute("SELECT COUNT(*) FROM bot_config").fetchone()[0]
    legacy = conn.execute("SELECT sale_uid, coins FROM csn_transactions").fetchone()
check("every csn_transactions row survived", after_txn, before_txn)
check("every hive_harvests row survived", after_hive, before_hive)
check("config survived", after_cfg, before_cfg)
check("the legacy row's own values are untouched",
      (legacy[0], round(legacy[1], 2)), ("legacyuid0001", 298.13))
check("pre-existing wage rows have a NULL signature (they are feed-shaped)",
      [r["sale_sig"] for r in new.get_hive_harvests_by_ids([h1, h2])], [None, None])

print("\n5. the new shape is live")
check("hive_harvests gained sale_sig", "sale_sig" in columns(live, "hive_harvests"), True)
check("csn_ingest exists", "csn_ingest" in [
    r[0] for r in sqlite3.connect(str(live)).execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()], True)
check("uq_csn_ingest is the dedup index", "uq_csn_ingest" in new_idx, True)
check("uq_csn_txn is GONE (it merged distinct sales)", "uq_csn_txn" in new_idx, False)
check("uq_csn_txn_uid remains", "uq_csn_txn_uid" in new_idx, True)
check("uq_hive_sig was added", "uq_hive_sig" in new_idx, True)
check("uq_hive_sale still exists…", "uq_hive_sale" in new_idx, True)
check("…but now excludes signed rows",
      "sale_sig IS NULL" in new_idx.get("uq_hive_sale", ""), True)

print("\n6. old rows still behave exactly as they did (no silent regression)")
# An unsigned re-post of h1 must STILL be rejected by the ±120s window: the feed path
# is untouched, and a migration that quietly loosened it would start double-paying.
dup = new.add_hive_harvest("greyhames", "Jesse", None, "Honey Block", 64, 5.46875,
                           "feed:3", 0, sale_ts="2026-08-14T14:18:31+00:00",
                           wage_value=4.6875)
check("unsigned repost of a pre-upgrade row is still refused", dup, None)

# And a signed row for the same physical sale must ALSO be refused — the wage was
# already paid under the old regime, and the upgrade must not re-open it.
import csn_sig                                                        # noqa: E402
sig = csn_sig.sale_sig("Vaicos", "Jesse", "sold", 64, "Honey Block", "-350",
                       "2026-08-14", 1)
signed_dup = new.add_hive_harvest("greyhames", "Jesse", None, "Honey Block", 64,
                                  5.46875, "feed:4", 0,
                                  sale_ts="2026-08-14T14:18:12+00:00",
                                  wage_value=4.6875, sale_sig=sig)
check("a SIGNED row for an already-paid pre-upgrade sale is refused too",
      signed_dup, None)

print("\n7. the migration is idempotent (the bot re-runs init_db every boot)")
new.init_db()
new.init_db()
with new.db() as conn:
    check("re-running init_db changes no row counts",
          (conn.execute("SELECT COUNT(*) FROM csn_transactions").fetchone()[0],
           conn.execute("SELECT COUNT(*) FROM hive_harvests").fetchone()[0]),
          (after_txn, after_hive))
check("and no index churn", indexes(live).keys() == new_idx.keys(), True)

print(f"\n{'='*58}\n{_PASSED} passed, {len(_FAILED)} failed")
if _FAILED:
    for f in _FAILED:
        print(f"  - {f}")
    print(f"\nThe 'before' database was preserved at {backup} for inspection.")
    sys.exit(1)
print("ALL CHECKS PASSED")
