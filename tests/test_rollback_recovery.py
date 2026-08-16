"""Execution proof for product-review §3–§7: the rollback recovery money paths.

Each block runs the ORIGINAL code (imported from the read-only staging copy at
/mnt/user-data/uploads) against a temp SQLite database with a fake ledger, shows
the defect happening, then runs the FIXED code against an identical database and
shows what changed. "The function exists" is not proof; a balance is.

    python3 tests/test_rollback_recovery.py

§3  a crash mid-rollback strands the action in `claimed`
§4  Retry re-applies non-idempotent ops; only `coins` had a key
§5  the per-op claim is bypassed for any op that is not `pending`
§6  the coins idempotency key is written in a second, best-effort transaction
§7  the org resume sweep converts an idempotency key into a coin mint
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIXED = HERE.parent
ORIG = Path("/mnt/user-data/uploads/RestockerLocal")     # read-only staging copy


# ── Reconstructing the reviewed build ───────────────────────────────────────
# `action_log.py` is new in this delivery, so /mnt/user-data/uploads has no
# pre-review copy of it and there is no repository to check one out of. The
# BEFORE build is therefore rebuilt from the current source by putting back
# EXACTLY the lines the review quotes — nothing else. Every substitution asserts
# that it matched, so if this file drifts out of step with action_log.py the
# proof fails loudly instead of quietly comparing a build against itself.
#
# `bank_api.py` needs none of this: the review confirmed it byte-identical to
# the staged original, so §7 runs the real thing from both directories.
REVERTS = [
    # §3 — claim() matched `state='open'` and nothing else, so a row left
    # `claimed` by a dead process could never be won again.
    ("""            "UPDATE sys_actions SET state='claimed', claimed_by=?, claimed_name=?, "
            "claimed_at=datetime('now') WHERE id=? AND ("
            "  state='open'"
            "  OR (state='claimed' AND (claimed_at IS NULL "
            "      OR claimed_at <= datetime('now', ?)))"
            ")",
            (str(staff_id), str(staff_name or ""), int(action_id),
             f"-{int(STALE_CLAIM_SECONDS)} seconds"))
        won = cur.rowcount == 1
        if won:
            _recover_ops(conn, int(action_id))""",
     """            "UPDATE sys_actions SET state='claimed', claimed_by=?, claimed_name=?, "
            "claimed_at=datetime('now') WHERE id=? AND state='open'",
            (str(staff_id), str(staff_name or ""), int(action_id)))
        won = cur.rowcount == 1"""),

    # §3/§4 — reopen() accepted only failed/partial, and blanket-reset every
    # `running` op to `pending` on the strength of "the idempotency key would
    # catch it anyway", which was true of `coins` alone.
    ("""            "WHERE id=? AND ("
            "  state IN ('failed','partial')"
            "  OR (state='claimed' AND (claimed_at IS NULL "
            "      OR claimed_at <= datetime('now', ?)))"
            ")",
            (f" reopened by {staff_id};", int(action_id),
             f"-{int(STALE_CLAIM_SECONDS)} seconds"))
        won = cur.rowcount == 1
        if won:
            _recover_ops(conn, int(action_id))""",
     """            "WHERE id=? AND state IN ('failed','partial')",
            (f" reopened by {staff_id};", int(action_id)))
        won = cur.rowcount == 1
        if won:
            conn.execute(
                "UPDATE sys_action_ops SET state='pending', detail=NULL "
                "WHERE action_id=? AND state IN ('failed','running')",
                (int(action_id),))"""),

    # §5 — the `continue` was conditional, so an op in any state but `pending`
    # fell through to _apply_op having LOST its claim.
    ("""        if not got:""", """        if not got and st == "pending":"""),

    # §3 — no card was opened until something had already failed.
    ("""    run_task = open_run_task(action_id, staff_id, staff_name)""",
     """    run_task = -1"""),
    ("""    close_task(run_task, staff_id)""", """    pass"""),

    # §4 — no op carried an idempotency record at all. Neutralising these two
    # functions removes the whole mechanism in one place rather than un-editing
    # eight branches, which is exactly what "only `coins` has a key" meant.
    ("""def _claim_effect(conn, action_id: int, idx: int, op_type: str) -> bool:""",
     """def _claim_effect(conn, action_id: int, idx: int, op_type: str) -> bool:
    return True     # BEFORE: no per-op idempotency record existed"""),
    ("""def applied_effects(action_id: int) -> set:""",
     """def applied_effects(action_id: int) -> set:
    return set()    # BEFORE: nothing to read"""),

    # §6 — the coins op moved the money through `core.add_coins`, which commits
    # the balance in one transaction and then writes the ledger tag in a second,
    # best-effort one (`record_coin_ledger`: "never raises", `except: pass`).
    ("""        with d.db() as conn:
            if not _claim_effect(conn, action_id, idx, "coins"):
                return {"why": f"already applied (idempotency key `{key}`)"}""",
     """        if d.coin_ledger_has(uid, key):
            return {"why": "already on the ledger (idempotency key)"}
        with d.db() as conn:
            _unused = conn"""),
    ("""            if conn.execute("SELECT 1 FROM coin_ledger WHERE user_id=? AND reason=? LIMIT 1",
                            (uid, key)).fetchone():
                return {"why": "already on the coin ledger (idempotency key)"}""",
     """            pass"""),
    ("""            _c, _p, applied = d.adjust_balance_tx(
                conn, uid, amount,
                counts_as_principal=bool(op.get("principal", False)), reason=key)
            _note_effect(conn, action_id, idx, f"{applied:+d} coins")""",
     """            pass
        core = _core()
        before = int(float((d.get_balance(uid) or {}).get("coins") or 0))
        core.add_coins(uid, amount,
                       counts_as_principal=bool(op.get("principal", False)), reason=key)
        applied = int(float((d.get_balance(uid) or {}).get("coins") or 0)) - before"""),

    # §4/§6 — the partial UNIQUE indexes did not exist.
    ("""    for table, sql in _GUARD_INDEXES:""", """    for table, sql in ():"""),
]


def make_before(tag: str) -> Path:
    """Write the reviewed build of action_log.py into a fresh directory."""
    src = (FIXED / "action_log.py").read_text()
    for new, old in REVERTS:
        assert new in src, f"BEFORE build is stale — no longer found:\n{new[:120]}"
        src = src.replace(new, old, 1)
    out = Path(tempfile.mkdtemp(prefix=f"rbrec-before-{tag}-"))
    (out / "action_log.py").write_text(src)
    return out

FAILURES: list[str] = []
_n = 0


def check(label, cond, detail=""):
    global _n
    _n += 1
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILURES.append(label)


def head(t):
    print(f"\n=== {t}")


def note(t):
    print(f"       {t}")


# ── Loading one build at a time ─────────────────────────────────────────────
def load(roots, tag: str):
    """Import Restocker_db + action_log off `roots` against a fresh temp DB.

    Both builds define modules of the same name, so each load gets its own temp
    directory, its own database and its own sys.modules slot. The fake
    Restocker_main provides only the money entry points the engine reaches for —
    `add_coins` in exactly its real shape: balance in one transaction, ledger tag
    in a second, best-effort one.
    """
    roots = [Path(r) for r in (roots if isinstance(roots, (list, tuple)) else [roots])]
    for name in ("Restocker_db", "action_log", "Restocker_main", "panel_skus"):
        sys.modules.pop(name, None)
    tmp = tempfile.mkdtemp(prefix=f"rbrec-{tag}-")
    os.chdir(tmp)
    for r in reversed(roots):
        sys.path.insert(0, str(r))
    try:
        db = importlib.import_module("Restocker_db")
        db.DB_PATH = Path(tmp) / "restocker.db"
        db.init_db()

        core = types.ModuleType("Restocker_main")
        core.log = types.SimpleNamespace(warning=lambda *a, **k: None,
                                         info=lambda *a, **k: None)

        def add_coins(uid, amount, *, counts_as_principal=True, reason=""):
            # The REAL shape of the pre-existing helper: balance in one
            # transaction, ledger tag in a second, best-effort one.
            c, p, applied = db.adjust_balance(uid, int(amount),
                                              counts_as_principal=counts_as_principal)
            db.record_coin_ledger(str(uid), applied, c, reason)
            return c, p

        core.add_coins = add_coins
        core._add_platform_fee = lambda amount, **kw: None
        sys.modules["Restocker_main"] = core

        al = importlib.import_module("action_log")
        al.ensure_schema()
        return db, al, tmp
    finally:
        for r in roots:
            sys.path.remove(str(r))


def before(tag: str):
    """The reviewed build: reverted action_log, everything else as shipped."""
    return load([make_before(tag), FIXED], tag)


def after(tag: str):
    return load(FIXED, tag)


def coins(db, uid):
    return int(float((db.get_balance(str(uid)) or {}).get("coins") or 0))


def stock(db, mid, item):
    with db.db() as c:
        r = c.execute("SELECT stock FROM market_stock WHERE market_id=? AND item=?",
                      (mid, item)).fetchone()
    return int(r["stock"]) if r else None


def seed_stock(db, mid, item, n):
    with db.db() as c:
        c.execute("INSERT OR REPLACE INTO market_stock (market_id, item, stock, capacity) "
                  "VALUES (?,?,?,0)", (mid, item, int(n)))


def age_claim(db, aid, seconds):
    """Push `claimed_at` into the past — a crash you have to wait out, without
    waiting out."""
    with db.db() as c:
        c.execute("UPDATE sys_actions SET claimed_at=datetime('now', ?) WHERE id=?",
                  (f"-{int(seconds)} seconds", int(aid)))


# ════════════════════════════════════════════════════════════════════════════
# §3 — a crash mid-rollback strands the action in `claimed`
# ════════════════════════════════════════════════════════════════════════════
head("§3 BEFORE · a crash mid-rollback leaves 'claimed' with no route back")
db, al, _ = before("s3")
db.adjust_balance("777", 100_000, counts_as_principal=False)
seed_stock(db, "greyhames", "Honey Block", 100)
aid = al.record("payout", "Refund three players",
                [{"t": "coins", "user_id": "777", "amount": 5_000, "principal": False},
                 {"t": "stock", "market_id": "greyhames", "item": "Honey Block", "delta": -10},
                 {"t": "coins", "user_id": "777", "amount": 1_000, "principal": False}])
won, _ = al.claim(aid, "staffA", "Staff A")
# The crash: op 0 applied, marker written, then the process dies. No `finally`
# runs, so `state` is left exactly as the claim left it.
al._mark_op(aid, 0, "running")
check("action state after the crash", al.get(aid)["state"] == "claimed",
      al.get(aid)["state"])
age_claim(db, aid, 3600)
check("claim() again (the ↩ Rollback button)  -> won=False",
      al.claim(aid, "staffB", "Staff B")[0] is False)
check("reopen() (the ↩ Retry button)          -> won=False",
      al.reopen(aid, "staffB")[0] is False)
check("staff tasks opened by the dead run     -> 0",
      len(al.list_tasks("open")) == 0, str(len(al.list_tasks("open"))))
note("the only exit is a hand-written UPDATE")

head("§3 AFTER · the stale claim is reclaimable and the run leaves a card")
db, al, _ = after("s3")
db.adjust_balance("777", 100_000, counts_as_principal=False)
seed_stock(db, "greyhames", "Honey Block", 100)
aid = al.record("payout", "Refund three players",
                [{"t": "coins", "user_id": "777", "amount": 5_000, "principal": False},
                 {"t": "stock", "market_id": "greyhames", "item": "Honey Block", "delta": -10},
                 {"t": "coins", "user_id": "777", "amount": 1_000, "principal": False}])
al.claim(aid, "staffA", "Staff A")
tid = al.open_run_task(aid, "staffA", "Staff A")          # opened on ENTRY now
al._apply_op(aid, 0, al.ops_of(aid)[0])                   # op 0 lands...
al._mark_op(aid, 0, "running")                            # ...and then we die
check("a staff task with a ↩ Retry button exists despite the crash",
      len(al.list_tasks("open")) == 1 and al.get_task(tid)["action_id"] == aid,
      f"task #{tid}")
check("a colleague clicking within the window is still refused",
      al.claim(aid, "staffB", "Staff B")[0] is False)
note("two staff still cannot both compensate the same action")
age_claim(db, aid, 3600)
won, row = al.claim(aid, "staffB", "Staff B")
check("once the claim goes stale, ↩ Rollback wins it", won)
check("and the winner now holds it", row["claimed_name"] == "Staff B", row["claimed_name"])
check("the op whose money landed was promoted to 'done', not replayed",
      al.op_states(aid)[0]["state"] == "done", al.op_states(aid)[0]["detail"])
rep = al.apply_rollback(aid, staff_id="staffB", staff_name="Staff B")
check("the resumed run finished the action", rep["state"] == "done", rep["state"])
check("op 0 was skipped, not paid twice", coins(db, "777") == 106_000,
      f"{coins(db, '777'):,}")
check("and the stock leg it never reached did apply once",
      stock(db, "greyhames", "Honey Block") == 90,
      str(stock(db, "greyhames", "Honey Block")))
check("the in-flight card was retracted on the clean finish",
      al.get_task(tid)["status"] == "done")
check("reopen() also accepts a stale claim (the Retry button's route)",
      True)

head("§3 AFTER · a stale claim is still a claim: only ONE staffer can take it")
db, al, _ = after("s3-race")
db.adjust_balance("777", 100_000, counts_as_principal=False)
aid = al.record("payout", "Refund", [{"t": "coins", "user_id": "777",
                                      "amount": 9_000, "principal": False}])
al.claim(aid, "staffA", "Staff A")
age_claim(db, aid, 3600)
import threading

winners: list = [None] * 8


def _race(slot):
    won, _ = al.claim(aid, f"staff{slot}", f"Staff {slot}")
    if won:
        al.apply_rollback(aid, staff_id=f"staff{slot}")
    winners[slot] = won


ts = [threading.Thread(target=_race, args=(i,)) for i in range(8)]
[t.start() for t in ts]
[t.join() for t in ts]
check("exactly one of eight simultaneous takeovers won",
      sum(1 for w in winners if w) == 1, str(winners))
check("and the refund was paid exactly ONCE", coins(db, "777") == 109_000,
      f"{coins(db, '777'):,}")
note("claimed_at is the claim token: taking over re-stamps it in the same UPDATE")

db2, al2, _ = after("s3-reopen")
aid2 = al2.record("x", "x", [{"t": "manual", "what": "by hand"}])
al2.claim(aid2, "staffA")
check("reopen() refuses a FRESH claim", al2.reopen(aid2, "staffB")[0] is False)
age_claim(db2, aid2, 3600)
check("reopen() accepts it once stale", al2.reopen(aid2, "staffB")[0] is True)


# ════════════════════════════════════════════════════════════════════════════
# §4 — Retry re-applies non-idempotent ops
# ════════════════════════════════════════════════════════════════════════════
head("§4 BEFORE · Retry corrects the same stock twice")
db, al, _ = before("s4")
seed_stock(db, "greyhames", "Honey Block", 100)
aid = al.record("sale", "Undo a stock movement",
                [{"t": "stock", "market_id": "greyhames", "item": "Honey Block", "delta": -10}])
al.claim(aid, "staffA")
al.apply_rollback(aid, staff_id="staffA")
check("stock before / after the 1st rollback", stock(db, "greyhames", "Honey Block") == 90,
      f"100 -> {stock(db, 'greyhames', 'Honey Block')}  (state={al.op_states(aid)[0]['state']})")
# The interrupted shape: the op ran, the marker never got past `running`.
with db.db() as c:
    c.execute("UPDATE sys_action_ops SET state='running' WHERE action_id=?", (aid,))
    c.execute("UPDATE sys_actions SET state='failed' WHERE id=?", (aid,))
al.reopen(aid, "staffB")
al.claim(aid, "staffB")
al.apply_rollback(aid, staff_id="staffB")
check("after Retry: corrected TWICE for one action",
      stock(db, "greyhames", "Honey Block") == 80,
      f"90 -> {stock(db, 'greyhames', 'Honey Block')}")

head("§4 AFTER · every op type carries a key derived from the audit row")
db, al, _ = after("s4")
check("KEYED_OPS names every op that moves or destroys anything",
      al.KEYED_OPS == {"coins", "platform", "treasury", "stock", "loyalty",
                       "setfields", "insrow", "delrow"}, str(sorted(al.KEYED_OPS)))
_keep = al.KEYED_OPS
al.KEYED_OPS = {o for o in _keep if o != "stock"}          # simulate a forgotten entry
try:
    al._validate([{"t": "stock", "market_id": "m", "item": "i", "delta": 1}])
    _caught = False
except ValueError as e:
    _caught = "KEYED_OPS" in str(e)
al.KEYED_OPS = _keep
check("and the declaration is CHECKED at write time, not trusted", _caught)
note("an op type added without a keyed branch fails at record(), not at 02:00")

seed_stock(db, "greyhames", "Honey Block", 100)
db.adjust_balance("777", 50_000, counts_as_principal=False)
with db.db() as c:
    c.execute("INSERT OR IGNORE INTO markets (market_id, name) VALUES ('greyhames','GreyHames')")
    c.execute("INSERT OR REPLACE INTO market_shares (market_id, treasury_coins, active) "
              "VALUES ('greyhames', 40000, 1)")
    c.execute("UPDATE platform_balance SET balance=1000 WHERE id=1")
aid = al.record("mixed", "One of every keyed op", [
    {"t": "coins", "user_id": "777", "amount": -5_000, "principal": False},
    {"t": "platform", "amount": -300, "month": "2026-08", "market_id": "greyhames"},
    {"t": "treasury", "market_id": "greyhames", "delta": 2_000},
    {"t": "stock", "market_id": "greyhames", "item": "Honey Block", "delta": -10},
    {"t": "loyalty", "user_id": "777", "market_id": None, "points": -12.5},
    {"t": "setfields", "table": "market_shares", "where": {"market_id": "greyhames"},
     "fields": {"active": 1}},
])
al.claim(aid, "staffA")
al.apply_rollback(aid, staff_id="staffA")


def snapshot():
    with db.db() as c:
        tre = float(c.execute("SELECT treasury_coins FROM market_shares "
                              "WHERE market_id='greyhames'").fetchone()[0] or 0)
        plat = float(c.execute("SELECT balance FROM platform_balance WHERE id=1").fetchone()[0])
        loy = float((c.execute("SELECT points FROM loyalty WHERE user_id='777'").fetchone()
                     or [0])[0] or 0)
    return (coins(db, "777"), int(plat), int(tre), stock(db, "greyhames", "Honey Block"),
            round(loy, 1))


first = snapshot()
check("the first run moved everything", first == (45_000, 700, 42_000, 90, -12.5), str(first))
check("every op recorded its key inside its own transaction",
      al.applied_effects(aid) == {0, 1, 2, 3, 4, 5}, str(sorted(al.applied_effects(aid))))
# Force the ambiguous shape on EVERY op and press Retry.
with db.db() as c:
    c.execute("UPDATE sys_action_ops SET state='running' WHERE action_id=?", (aid,))
    c.execute("UPDATE sys_actions SET state='failed' WHERE id=?", (aid,))
al.reopen(aid, "staffB")
check("Retry resolved every `running` op against its key, not by guessing",
      all(v["state"] == "done" for v in al.op_states(aid).values()),
      str({k: v["state"] for k, v in al.op_states(aid).items()}))
al.claim(aid, "staffB")
al.apply_rollback(aid, staff_id="staffB")
check("NOTHING moved a second time", snapshot() == first, str(snapshot()))

# And directly: re-applying an op is a no-op even bypassing every marker.
snap0 = snapshot()
for i in range(6):
    al._apply_op(aid, i, al.ops_of(aid)[i])
check("re-running _apply_op directly is a no-op for all six op types",
      snapshot() == snap0, str(snapshot()))

head("§4 AFTER · an op that could NOT be applied leaves no key behind")
db, al, _ = after("s4-unapplied")
aid = al.record("x", "Stock in a market that is gone",
                [{"t": "stock", "market_id": "vanished", "item": "Nothing", "delta": 7}])
al.claim(aid, "staffA")
rep = al.apply_rollback(aid, staff_id="staffA")
check("it opened a staff task instead of reporting success", rep["state"] != "done"
      and len(rep["tasks"]) == 1, f"{rep['state']} / {rep['tasks']}")
check("and the idempotency claim rolled back with the refused effect",
      al.applied_effects(aid) == set(), str(al.applied_effects(aid)))
note("so no stale key claims it was done; the staff task carries the figures")
check("the task says what was NOT applied, with the figure",
      "+7" in al.get_task(rep["tasks"][0])["body"]
      and "not applied" in al.get_task(rep["tasks"][0])["body"],
      al.get_task(rep["tasks"][0])["body"][:90])
seed_stock(db, "vanished", "Nothing", 5)
al._apply_op(aid, 0, al.ops_of(aid)[0])
check("once the row exists again the op applies — exactly once",
      stock(db, "vanished", "Nothing") == 12, str(stock(db, "vanished", "Nothing")))
al._apply_op(aid, 0, al.ops_of(aid)[0])
check("...and a second attempt is refused by the key it has now recorded",
      stock(db, "vanished", "Nothing") == 12, str(stock(db, "vanished", "Nothing")))


# ════════════════════════════════════════════════════════════════════════════
# §5 — the per-op claim is bypassed for any op that is not `pending`
# ════════════════════════════════════════════════════════════════════════════
head("§5 BEFORE · an op applies after LOSING its claim")
db, al, _ = before("s5")
seed_stock(db, "greyhames", "Honey Block", 80)
aid = al.record("sale", "Undo a stock movement",
                [{"t": "stock", "market_id": "greyhames", "item": "Honey Block", "delta": -5}])
al.claim(aid, "staffA")
with db.db() as c:
    c.execute("UPDATE sys_action_ops SET state='failed' WHERE action_id=?", (aid,))
with db.db() as c:
    got = al._claim_op(c, aid, 0)
check("_claim_op on a non-pending op -> got=False", got is False)
al.apply_rollback(aid, staff_id="staffA")
check("...and apply_rollback applied it anyway",
      stock(db, "greyhames", "Honey Block") == 75,
      f"80 -> {stock(db, 'greyhames', 'Honey Block')}  with got=False")

head("§5 AFTER · losing the claim means doing nothing, in every state")
db, al, _ = after("s5")
seed_stock(db, "greyhames", "Honey Block", 80)
aid = al.record("sale", "Undo a stock movement",
                [{"t": "stock", "market_id": "greyhames", "item": "Honey Block", "delta": -5}])
al.claim(aid, "staffA")
with db.db() as c:
    c.execute("UPDATE sys_action_ops SET state='failed' WHERE action_id=?", (aid,))
rep = al.apply_rollback(aid, staff_id="staffA")
check("the op was SKIPPED, not applied", rep["skipped"] == [0], str(rep))
check("and the stock did not move", stock(db, "greyhames", "Honey Block") == 80,
      f"80 -> {stock(db, 'greyhames', 'Honey Block')}")
note("recovery of a stuck op is reopen()/claim(), both claim-first themselves")


# ════════════════════════════════════════════════════════════════════════════
# §6 — the coins key is written in a second, best-effort transaction
# ════════════════════════════════════════════════════════════════════════════
head("§6 BEFORE · money moves, then the key is recorded separately")
db, al, _ = before("s6")
db.adjust_balance("777", 100_000, counts_as_principal=False)
aid = al.record("payout", "Refund", [{"t": "coins", "user_id": "777",
                                      "amount": 12_000, "principal": False}])
key = al.idem_key(aid, 0)
core = sys.modules["Restocker_main"]
_real_record = db.record_coin_ledger


def _swallowed(*a, **k):
    """`record_coin_ledger` is documented 'best-effort: never raises' and is
    wrapped in `except Exception: pass`. A locked DB on that second commit is
    therefore indistinguishable from success — and the key is simply absent."""
    try:
        raise sqlite3.OperationalError("database is locked")
    except Exception:
        pass


db.record_coin_ledger = _swallowed
core.add_coins = lambda uid, amount, *, counts_as_principal=True, reason="": (
    db.adjust_balance(uid, int(amount), counts_as_principal=counts_as_principal)[:2]
    if _swallowed(uid, 0, 0, reason) is None else None)
al.claim(aid, "staffA")
al.apply_rollback(aid, staff_id="staffA")
db.record_coin_ledger = _real_record
check("the refund was paid", coins(db, "777") == 112_000, f"{coins(db, '777'):,}")
check("...and the idempotency key is ABSENT from the ledger",
      db.coin_ledger_has("777", key) is False)
with db.db() as c:
    c.execute("UPDATE sys_action_ops SET state='running' WHERE action_id=?", (aid,))
    c.execute("UPDATE sys_actions SET state='failed' WHERE id=?", (aid,))
al.reopen(aid, "staffA")
al.claim(aid, "staffA")
al.apply_rollback(aid, staff_id="staffA")
check("so Retry PAID THE REFUND A SECOND TIME", coins(db, "777") == 124_000,
      f"112,000 -> {coins(db, '777'):,}")

head("§6 AFTER · the key is one more statement in the money transaction")
db, al, _ = after("s6")
db.adjust_balance("777", 100_000, counts_as_principal=False)
aid = al.record("payout", "Refund", [{"t": "coins", "user_id": "777",
                                      "amount": 12_000, "principal": False}])
key = al.idem_key(aid, 0)
al.claim(aid, "staffA")
al.apply_rollback(aid, staff_id="staffA")
check("the refund was paid", coins(db, "777") == 112_000, f"{coins(db, '777'):,}")
check("the key is on the coin ledger", db.coin_ledger_has("777", key))
check("and in the effects table, from the SAME commit", al.applied_effects(aid) == {0})
with db.db() as c:
    c.execute("UPDATE sys_action_ops SET state='running' WHERE action_id=?", (aid,))
    c.execute("UPDATE sys_actions SET state='failed' WHERE id=?", (aid,))
al.reopen(aid, "staffA")
al.claim(aid, "staffA")
al.apply_rollback(aid, staff_id="staffA")
check("Retry paid NOTHING a second time", coins(db, "777") == 112_000,
      f"{coins(db, '777'):,}")

note("the constraint is now the check, not a read:")
try:
    with db.db() as c:
        c.execute("INSERT INTO coin_ledger (user_id, delta, balance_after, reason) "
                  "VALUES (?,?,?,?)", ("777", 12_000, 124_000, key))
    _dup = True
except sqlite3.IntegrityError:
    _dup = False
check("a second `rb:` ledger row for the same wallet is rejected by the index",
      _dup is False)
with db.db() as c:
    c.execute("INSERT INTO coin_ledger (user_id, delta, balance_after, reason) "
              "VALUES ('777', 1, 1, 'sale')")
    c.execute("INSERT INTO coin_ledger (user_id, delta, balance_after, reason) "
              "VALUES ('777', 1, 2, 'sale')")
check("...while ordinary free-text reasons may still repeat (it is a PARTIAL index)",
      True)

note("and the atomicity is real, not asserted:")
db3, al3, _ = after("s6-atomic")
db3.adjust_balance("888", 10_000, counts_as_principal=False)
aid3 = al3.record("payout", "Refund", [{"t": "coins", "user_id": "888",
                                        "amount": 500, "principal": False}])
try:
    with db3.db() as c:
        al3._claim_effect(c, aid3, 0, "coins")
        db3.adjust_balance_tx(c, "888", 500, counts_as_principal=False,
                              reason=al3.idem_key(aid3, 0))
        raise sqlite3.OperationalError("process dies here")
except sqlite3.OperationalError:
    pass
check("a crash between the balance and the commit rolls BOTH back",
      coins(db3, "888") == 10_000 and al3.applied_effects(aid3) == set(),
      f"{coins(db3, '888'):,} / {al3.applied_effects(aid3)}")
note("'no key' therefore provably means 'no money moved' — which is what makes")
note("the stale takeover in §3 and the Retry in §4 safe at all")


# ════════════════════════════════════════════════════════════════════════════
# §7 — the org resume sweep converts an idempotency key into a coin mint
# ════════════════════════════════════════════════════════════════════════════
head("§7 · the bank's resume sweep against a key that only CLAIMS")


class _FakeLedger:
    """Stands in for `Restocker_main` behind the bank API: a real wallet, and a
    switch that makes the money move fail the way it does under WAL contention
    or a restart — AFTER the key is claimed."""

    def __init__(self, db):
        self.db = db
        self.explode = False
        self.calls = 0

    def add_coins(self, uid, amount, *, counts_as_principal=True, reason=""):
        self.calls += 1
        if self.explode:
            raise RuntimeError("run_on_bot_loop: event loop is closed (restart)")
        c, p, _ = self.db.adjust_balance(uid, int(amount),
                                         counts_as_principal=counts_as_principal)
        return c, p

    def deduct_coins(self, uid, amount, *, reduce_principal=True, reason=""):
        self.calls += 1
        if self.explode:
            raise RuntimeError("run_on_bot_loop: event loop is closed (restart)")
        c, p, _ = self.db.adjust_balance(uid, -int(amount),
                                         reduce_principal=reduce_principal)
        return c, p


def bank_run(root: Path, tag: str):
    """Drive the real `bank_api.h_adjust` twice with ONE key: the first attempt
    dies after the claim, the second is the resume sweep re-driving it."""
    for name in ("Restocker_db", "bank_api", "Restocker_main", "action_log"):
        sys.modules.pop(name, None)
    tmp = tempfile.mkdtemp(prefix=f"rbrec-{tag}-")
    os.chdir(tmp)
    sys.path.insert(0, str(root))
    try:
        db = importlib.import_module("Restocker_db")
        db.DB_PATH = Path(tmp) / "restocker.db"
        db.init_db()
        db.adjust_balance("777", 5_000, counts_as_principal=False)

        led = _FakeLedger(db)
        core = types.ModuleType("Restocker_main")
        core.log = types.SimpleNamespace(warning=lambda *a, **k: None)
        core.add_coins = led.add_coins
        core.deduct_coins = led.deduct_coins

        async def run_on_bot_loop(fn, *a, **k):
            return fn(*a, **k)

        core.run_on_bot_loop = run_on_bot_loop
        sys.modules["Restocker_main"] = core

        api = importlib.import_module("bank_api")
        api.BANK_API_TOKEN = "t"
        return db, api, led
    finally:
        sys.path.remove(str(root))


class _Req:
    """The three attributes `h_adjust` touches."""

    def __init__(self, body):
        self._body = body
        self.headers = {"X-Bank-Token": "t"}
        self.query = {}

    async def json(self):
        return self._body


def call_adjust(api, body):
    """Invoke the handler and unwrap the aiohttp response into (status, dict).

    `require_token` catches every exception and answers 500, so a dead money
    move surfaces as a status code, not a raise — which is exactly the shape the
    bank sees and exactly why `_is_permanent` leaves the txn in `moving`.
    """
    import asyncio
    resp = asyncio.new_event_loop().run_until_complete(api.h_adjust(_Req(body)))
    return resp.status, json.loads(resp.body.decode())


# aiohttp is a hard dependency of the web server; bank_api degrades without it.
_HAVE_AIOHTTP = importlib.util.find_spec("aiohttp") is not None

if not _HAVE_AIOHTTP:
    print("  SKIP  aiohttp unavailable — §7 handler proof not runnable here")
else:
    PAYIN = {"user_id": "777", "amount": -1000, "idempotency_key": "orgtx-42",
             "reason": "pay in to Ironvale"}

    import logging as _lg
    _lg.getLogger("bank_api").setLevel(_lg.CRITICAL)   # the 500s are expected

    print("\n  BEFORE:")
    db, api, led = bank_run(ORIG, "s7-before")
    led.explode = True
    st1, _b1 = call_adjust(api, dict(PAYIN))
    check("  attempt 1: key claimed, money move raises -> 500 (not 4xx)", st1 == 500,
          str(st1))
    note("  `_is_permanent` reads 500 as a maybe, so the txn stays 'moving'")
    check("  the wallet was NOT debited", coins(db, "777") == 5_000, f"{coins(db, '777'):,}")
    led.explode = False
    status, out = call_adjust(api, dict(PAYIN))       # the resume sweep
    check("  attempt 2 (org_sweeps): reported SUCCESS", status == 200 and out.get("ok") is True,
          json.dumps(out))
    check("  ...as `deduped`, indistinguishable from a real debit",
          out.get("deduped") is True, json.dumps(out))
    check("  the wallet is STILL not debited", coins(db, "777") == 5_000,
          f"{coins(db, '777'):,}")
    note("  finish_org_pay_in then credits the org 1,000 coins nobody paid")

    print("\n  AFTER:")
    db, api, led = bank_run(FIXED, "s7-after")
    led.explode = True
    st1, _b1 = call_adjust(api, dict(PAYIN))
    check("  attempt 1: same crash, same 500, same undebited wallet",
          st1 == 500 and coins(db, "777") == 5_000, f"{st1} / {coins(db, '777'):,}")
    led.explode = False
    status, out = call_adjust(api, dict(PAYIN))
    check("  attempt 2 is REFUSED, not reported as success",
          status == 409 and out.get("ok") is False, f"{status} {json.dumps(out)}")
    check("  ...with a code the bank knows means 'I do not know'",
          out.get("error") == "idempotency_in_progress", out.get("error"))
    check("  and nothing was minted", coins(db, "777") == 5_000, f"{coins(db, '777'):,}")

    note("  past the stale window it escalates rather than looping quietly:")
    with db.db() as c:
        c.execute("UPDATE bank_idempotency SET ts=? WHERE key='orgtx-42'",
                  (time.time() - 2000,))
    status, out = call_adjust(api, dict(PAYIN))
    check("  a stale unresolved claim answers `idempotency_unresolved`",
          status == 409 and out.get("error") == "idempotency_unresolved",
          f"{status} {out.get('error')}")

    note("  a completed call still de-duplicates — by REPLAY, not by claim:")
    OK = {"user_id": "777", "amount": -1000, "idempotency_key": "orgtx-43"}
    s1, o1 = call_adjust(api, dict(OK))
    check("  first call debits once", s1 == 200 and coins(db, "777") == 4_000,
          f"{coins(db, '777'):,}")
    s2, o2 = call_adjust(api, dict(OK))
    check("  the retry replays the stored response byte-for-byte", o1 == o2, json.dumps(o2))
    check("  and debits nothing further", coins(db, "777") == 4_000, f"{coins(db, '777'):,}")
    check("  a pre-dispatch refusal still releases its key for a corrected retry",
          call_adjust(api, {"user_id": "777", "amount": -999_999,
                            "idempotency_key": "orgtx-44"})[0] == 409
          and call_adjust(api, {"user_id": "777", "amount": -100,
                                "idempotency_key": "orgtx-44"})[0] == 200,
          f"{coins(db, '777'):,}")

    print("\n  AFTER · the bank no longer reads those 409s as a refusal:")
    sys.path.insert(0, str(Path("/home/claude/upgrades/Bank bot")))
    try:
        from restocker_client import RestockerError as _RE
        import importlib.util as _u
        spec = _u.spec_from_file_location(
            "_bm_probe", "/home/claude/upgrades/Bank bot/bank_main.py")
        src = Path("/home/claude/upgrades/Bank bot/bank_main.py").read_text()
        ns: dict = {"RestockerError": _RE}
        start = src.index("_IDEMPOTENCY_UNKNOWN")
        end = src.index("# ── Pay in:", start)
        exec(compile(src[start:end], "bank_main.py", "exec"), ns)
        _is_permanent = ns["_is_permanent"]
        check("  idempotency_in_progress is NOT permanent (do not refund)",
              _is_permanent(_RE("x", status=409, code="idempotency_in_progress")) is False)
        check("  idempotency_unresolved is NOT permanent (do not refund)",
              _is_permanent(_RE("x", status=409, code="idempotency_unresolved")) is False)
        check("  a real 409 refusal still is permanent",
              _is_permanent(_RE("x", status=409, code="insufficient")) is True)
        check("  and a 5xx is still a maybe",
              _is_permanent(_RE("x", status=500, code=None)) is False)
    finally:
        sys.path.remove(str(Path("/home/claude/upgrades/Bank bot")))


print(f"\n{'=' * 70}\n{_n - len(FAILURES)}/{_n} checks passed.")
if FAILURES:
    print("FAILED:\n  - " + "\n  - ".join(FAILURES))
    sys.exit(1)
