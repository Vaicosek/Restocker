"""probe_ign_links.py — executes ign_links against a real sqlite database with the
real Restocker_db schema. Asserts on STATE (what the tables hold afterwards),
not on return values, except where the return value IS the contract.

    python3 tests/probe_ign_links.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_tmp = tempfile.mkdtemp(prefix="ignprobe_")
os.chdir(_tmp)

import Restocker_db as db                                    # noqa: E402
db.DB_PATH = Path(_tmp) / "probe.db"
db.init_db() if hasattr(db, "init_db") else None

import ign_links as L                                        # noqa: E402

PASS = FAIL = 0
FAILURES = []


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        FAILURES.append(f"{name} — {detail}")
        print(f"  FAIL {name} — {detail}")


def q(sql, args=()):
    with db.db() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


# ── bootstrap ───────────────────────────────────────────────────────────────
with db.db() as c:
    c.executescript(db.SCHEMA)
L.ensure_schema()

with db.db() as c:
    c.execute("INSERT INTO markets (market_id, name, owner_id, manager_ids, "
              "leader_discord_id) VALUES ('greyhames','GreyHames','100','[\"101\"]','100')")
    c.execute("INSERT INTO markets (market_id, name, owner_id, manager_ids) "
              "VALUES ('goblin_mart','Goblin Mart','200','[]')")

print("\n§1 evidence never touches the money-routing table")
L.observe("Vaicos", "9001", market_id="goblin_mart")
check("1.1 observation row exists",
      len(q("SELECT * FROM ign_observations WHERE ign='Vaicos'")) == 1)
check("1.2 ign_registry is STILL EMPTY after observing",
      q("SELECT * FROM ign_registry") == [],
      f"registry={q('SELECT * FROM ign_registry')}")
check("1.3 get_user_id_by_ign does not resolve an observed name",
      db.get_user_id_by_ign("Vaicos") is None)

print("\n§2 observe is an upsert, and hits accumulate rather than being rewritten")
L.observe("Vaicos", "9001", market_id="goblin_mart")
L.observe("Vaicos", "9001", market_id="goblin_mart")
row = q("SELECT * FROM ign_observations WHERE ign='Vaicos'")[0]
check("2.1 three uploads -> one row", len(q("SELECT * FROM ign_observations")) == 1)
check("2.2 hits == 3", row["hits"] == 3, f"hits={row['hits']}")
check("2.3 first_seen preserved, last_seen advanced",
      row["first_seen"] <= row["last_seen"])
check("2.4 case-insensitive: 'vaicos' is the same name",
      L.observe("vaicos", "9001")["hits"] == 4,
      "a second row would split the evidence for one player")

print("\n§3 confirm is the only promotion path, and it is audited in one transaction")
check("3.1 pending lists it before a decision",
      any(r["ign"].lower() == "vaicos" for r in L.pending()))
res = L.confirm("Vaicos", "500", "999", reason="probe", source_ref="9001")
check("3.2 returns 'bound'", res == "bound", res)
check("3.3 registry row written", db.get_user_id_by_ign("Vaicos") == "500")
lg = q("SELECT * FROM ign_registry_log WHERE ign='Vaicos'")
check("3.4 exactly one audit row, event=bound, actor recorded",
      len(lg) == 1 and lg[0]["event"] == "bound" and lg[0]["actor"] == "999", lg)
check("3.5 observation moved out of 'observed'",
      q("SELECT state FROM ign_observations WHERE ign='Vaicos'")[0]["state"] == "confirmed")
check("3.6 pending no longer offers it",
      not any(r["ign"].lower() == "vaicos" for r in L.pending()))

print("\n§4 idempotency and the 'taken' contract")
check("4.1 same user again -> 'exists'", L.confirm("Vaicos", "500", "999") == "exists")
check("4.2 ...and writes NO second audit row",
      len(q("SELECT * FROM ign_registry_log WHERE ign='Vaicos'")) == 1)
check("4.3 different user -> 'taken'", L.confirm("Vaicos", "501", "999") == "taken")
check("4.4 ...and the holder is unchanged", db.get_user_id_by_ign("Vaicos") == "500")
check("4.5 ...and no audit row was invented",
      len(q("SELECT * FROM ign_registry_log WHERE ign='Vaicos'")) == 1)

print("\n§5 the race add_ign cannot survive: two confirms, no exception escapes")
# add_ign() reads the owner, compares in Python, then INSERTs. Simulate the lost
# race by inserting the competing row between the read and the write — which is
# exactly what a second manager does. confirm() must return 'taken', not raise.
with db.db() as c:
    c.execute("INSERT INTO ign_registry (ign, user_id, registered_at) "
              "VALUES ('Contested','700','2026-01-01')")
try:
    r5 = L.confirm("Contested", "701", "999")
    raised = None
except Exception as e:                                       # noqa: BLE001
    r5, raised = None, e
check("5.1 confirm returns 'taken' instead of raising", r5 == "taken" and raised is None,
      f"raised={raised!r}")
try:
    db.add_ign("701", "Contested")
    ok = True
except Exception:
    ok = False
check("5.2 (control) add_ign's own path returns rather than raising here too", ok,
      "documented for contrast; the difference is the audit row, not this case")

print("\n§6 revoke: the unbind surface the tree does not have, and it keeps the fact")
freed = L.revoke("Vaicos", "999", reason="probe unbind")
check("6.1 returns the freed holder", freed == "500", freed)
check("6.2 registry row gone", db.get_user_id_by_ign("Vaicos") is None)
hist = L.history("Vaicos")
check("6.3 history survives the delete: bound then revoked",
      [h["event"] for h in hist] == ["bound", "revoked"], hist)
check("6.4 revoking nothing returns None and logs nothing",
      L.revoke("NeverBound", "999") is None
      and q("SELECT * FROM ign_registry_log WHERE ign='NeverBound'") == [])
check("6.5 the name is re-bindable afterwards, and that is a THIRD log row",
      L.confirm("Vaicos", "501", "999") == "bound"
      and len(L.history("Vaicos")) == 3)

print("\n§7 attribution — three outcomes, and the Goblin Mart shape")
a = L.check_attribution("greyhames", "Unseen")
check("7.1 unbound name -> unknown, not ok", a["verdict"] == "unknown", a)
L.confirm("Owner1", "100", "999")                 # greyhames' owner
L.confirm("Mgr1", "101", "999")                   # greyhames' manager
L.confirm("Stranger", "900", "999")
check("7.2 market owner -> ok",
      L.check_attribution("greyhames", "Owner1")["verdict"] == "ok")
check("7.3 market manager -> ok",
      L.check_attribution("greyhames", "Mgr1")["verdict"] == "ok")
g = L.check_attribution("goblin_mart", "Owner1")
check("7.4 GreyHames' owner's scan landing in goblin_mart -> foreign",
      g["verdict"] == "foreign", g)
check("7.5 ...and the reason names both the person and the market",
      "100" in g["reason"] and "goblin_mart" in g["reason"], g["reason"])
check("7.6 unregistered market -> unknown",
      L.check_attribution("no_such_market", "Owner1")["verdict"] == "unknown")
check("7.7 empty stamp -> unknown, never ok",
      L.check_attribution("greyhames", "")["verdict"] == "unknown")

print("\n§8 reject is distinct from 'not yet looked at'")
L.observe("Impostor", "9002", market_id="greyhames")
check("8.1 reject returns True on an undecided row",
      L.reject("Impostor", "9002", "999", reason="not them") is True)
check("8.2 state is rejected", q("SELECT state FROM ign_observations WHERE ign='Impostor'")[0]["state"] == "rejected")
check("8.3 rejecting again is False (nothing to claim)",
      L.reject("Impostor", "9002", "999") is False)
L.observe("Impostor", "9002", market_id="greyhames")
r8 = q("SELECT * FROM ign_observations WHERE ign='Impostor'")[0]
check("8.4 a rejected pair that reappears bumps hits and STAYS rejected",
      r8["hits"] == 2 and r8["state"] == "rejected", r8)
check("8.5 rejected pairs never enter ign_registry",
      db.get_user_id_by_ign("Impostor") is None)

print("\n§9 the wiring: manage_team's add path now writes the audit log")
src = (ROOT / "Restocker_main.py").read_text(encoding="utf-8")
check("9.1 _ai_tool_manage_ign_links exists", "async def _ai_tool_manage_ign_links" in src)
check("9.2 it is registered in the AI tool schema list", '"name": "manage_ign_links"' in src)
check("9.3 it is dispatchable", '"manage_ign_links":' in src)
check("9.4 manage_team no longer calls the read-then-write set_ign",
      "_db.set_ign(raw, ign)" not in src)
check("9.5 manage_team routes through ign_links.confirm",
      "_ignl.confirm(ign, raw" in src)
check("9.6 the CSN monthly branch observes the shop stamp",
      "_ignl.observe(_shop" in src)
check("9.7 ...and runs the attribution check",
      "_ignl.check_attribution(effective_market_id, _shop)" in src)
check("9.8 the ingest hook warns and does not reject",
      "nothing was rejected" in src)

print("\n§10 back-harvest attach is bounded to unpaid rows")
with db.db() as c:
    c.execute("INSERT INTO hive_harvests (market_id, ign, user_id, item, qty, unit_value, "
              "msg_id, line_no, paid) VALUES ('greyhames','LateGuy',NULL,'Honey',10,100,'m',0,0)")
    c.execute("INSERT INTO hive_harvests (market_id, ign, user_id, item, qty, unit_value, "
              "msg_id, line_no, paid) VALUES ('greyhames','LateGuy',NULL,'Honey',10,100,'m',1,1)")
n = db.set_hive_harvest_user("LateGuy", "800")
check("10.1 only the UNPAID row is attached", n == 1, f"attached={n}")
check("10.2 the paid row keeps user_id NULL — no re-pay is created",
      q("SELECT user_id FROM hive_harvests WHERE line_no=1")[0]["user_id"] is None)

print(f"\n{'='*62}\n  {PASS} passed, {FAIL} failed\n{'='*62}")
for f in FAILURES:
    print("  ✗", f)
sys.exit(1 if FAIL else 0)
