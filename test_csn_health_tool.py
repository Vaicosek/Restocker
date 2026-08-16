"""Executable proof for the CSN ingest operator surface.

Run: python3 test_csn_health_tool.py

`_ai_tool_csn_ingest_health` lives in Restocker_main.py, which cannot be imported
outside a running bot (discord.py, a token, a gateway). Rather than leave it
unexecuted — its SQL is hand-written and its formatting touches money — the function
is lifted out of the real file by AST SPAN and executed against a real SQLite database
with the two names it needs stubbed.

Lifting it by span rather than copying it matters: a copy drifts, and a test of a copy
proves nothing about what ships.
"""
import ast
import asyncio
import io
import os
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

_TMP = tempfile.mkdtemp(prefix="csn_health_test_")
import Restocker_db as db                                             # noqa: E402
db.DB_PATH = Path(_TMP) / "test.db"

_FAILED = []
_PASSED = 0


def check(label, ok, detail=""):
    global _PASSED
    if ok:
        _PASSED += 1
        print(f"  ok   {label}")
    else:
        _FAILED.append(label)
        print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ""))


# ── lift the function out of the shipping file ──────────────────────────────
SRC = io.open(os.path.join(HERE, "Restocker_main.py"), encoding="utf-8").read()
tree = ast.parse(SRC)
node = next((n for n in tree.body
             if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef))
             and n.name == "_ai_tool_csn_ingest_health"), None)
if node is None:
    print("FAIL — _ai_tool_csn_ingest_health is not defined in Restocker_main.py")
    sys.exit(1)

ns = {"_ai_is_manager": lambda user: bool(getattr(user, "is_manager", True))}
exec(compile(ast.Module(body=[node], type_ignores=[]), "Restocker_main.py", "exec"), ns)
tool = ns["_ai_tool_csn_ingest_health"]


class U:
    def __init__(self, is_manager=True):
        self.is_manager = is_manager


def run(**args):
    return asyncio.run(tool(None, None, U(), args))


db.init_db()
print()

print("1. gating and empty state")
out = asyncio.run(tool(None, None, U(is_manager=False), {}))
check("non-managers are refused", out.startswith("❌"), out)
out = run(market="greyhames")
# EMPTY STATES ARE EMPTY: one sentence, no table of zeroes, no decorated absence.
check("an empty store says so in one line and shows no counters",
      out == "No CSN sales have been ingested for `greyhames` yet.", out)


def row(actor, coins, occ=1, verb="bought", item="Diamond"):
    return {"actor": actor, "seller": "Vaicos", "verb": verb, "qty": 64,
            "item": item, "item_raw": item, "coins_str": coins,
            "coins": float(coins), "sale_date": "2026-08-14", "occ": occ,
            "sale_ts": "2026-08-14T12:00:00+00:00"}


print("\n2. figures, not intentions")
ids = db.csn_ingest_record("greyhames", [row("Steve", "100.00", 1),
                                         row("Steve", "100.00", 2),
                                         row("Ann", "50.50", 1)])["ids"]
check("three sales landed", len(ids) == 3, str(ids))
out = run(market="greyhames")
check("reports the held count", "3 sale(s) held" in out, out)
# 100.00 + 100.00 + 50.50 = 250.50, summed as INTEGER centi-coins in SQL.
check("reports turnover to the cent, from integer centi-coins",
      "250.50 coins" in out, out)
check("every consumer is listed as pending",
      all(f"{lbl}: 3 pending" in out
          for lbl in ("ledger", "earnings", "hive wages", "announcements")), out)

print("\n3. progress is per consumer")
db.csn_settle("txn", db.csn_claim("txn", ids[:2]), "done")
db.csn_settle("hive", db.csn_claim("hive", ids), "skip")
out = run(market="greyhames")
check("the ledger shows 1 pending / 2 done",
      "ledger: 1 pending, 2 done" in out, out)
check("hive wages show 3 skip, distinct from done",
      "hive wages: 3 skip" in out, out)
check("earnings are untouched by either", "earnings: 3 pending" in out, out)

print("\n4. clean state is stated, not implied")
out = run(market="greyhames", stuck_minutes=30)
check("says plainly that nothing needs a human",
      "No stuck claims older than 30 min" in out, out)

print("\n5. a stuck claim is surfaced with its real figures")
db.csn_claim("earn", [ids[2]])          # claimed and never settled = crashed mid-effect
out = run(market="greyhames", stuck_minutes=-1)
check("the stuck claim is reported", "1 stuck claim(s)" in out, out)
check("named by consumer", "earn" in out.split("stuck claim(s)")[1], out)
check("with the customer's real name", "Ann" in out, out)
check("with the item's real name, not an internal id", "Diamond" in out, out)
check("with the money to the cent", "50.50c" in out, out)
check("and says it is deliberately not auto-released",
      "NOT released automatically" in out, out)
check("and offers no button", "button here on purpose" in out, out)

print("\n6. scoping")
db.csn_ingest_record("vtech", [row("Bob", "7.00", 1)])
out_all = run()
check("unscoped covers every market", "4 sale(s) held" in out_all, out_all)
out_v = run(market="vtech", stuck_minutes=-1)
check("scoped excludes the other market", "1 sale(s) held" in out_v, out_v)
check("a scoped view hides the other market's stuck claim",
      "stuck claim(s)**" not in out_v and "Ann" not in out_v, out_v)

print(f"\n{'='*58}\n{_PASSED} passed, {len(_FAILED)} failed")
if _FAILED:
    for f in _FAILED:
        print(f"  - {f}")
    sys.exit(1)
print("ALL CHECKS PASSED")
