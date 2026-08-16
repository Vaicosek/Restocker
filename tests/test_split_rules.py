"""Executable proof for `split_rules.py`, against a real temp SQLite database.

Real `Restocker_db` schema, real `ledger_migrate` (escrow triggers included), real
`ledger_v2._debit` / `_credit`. Nothing here asserts against a return value where it
could assert against `balances`, `ledger_holds`, `ledger_entries` or `split_runs` —
a function that returns `{"success": true}` while destroying a coin is precisely the
bug this module was written to remove, so return values are checked only for the
things that ARE the contract (the outcome word, the run id, the shortfall).

THE INVARIANT, checked on every scenario that moves money:

    total coins in the world before  ==  total coins in the world after

`world()` sums every row in `balances`, so a mint shows up as a rise and a
destroyed coin as a fall, whichever account it happened on.

Run:  python3 tests/test_split_rules.py
"""
import json
import sqlite3
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
for candidate in (Path("/home/claude/build"), ROOT.parent / "build"):
    if (candidate / "ledger_v2.py").exists():
        sys.path.insert(0, str(candidate))
        break

PASS, FAIL = [], []


def check(name, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  ok   {name}")
    except Exception as e:
        FAIL.append((name, e))
        print(f"  FAIL {name}: {e}")
        traceback.print_exc()


def eq(a, b, what=""):
    if a != b:
        raise AssertionError(f"{what}: {a!r} != {b!r}")


def truthy(v, what=""):
    if not v:
        raise AssertionError(f"{what}: expected truthy, got {v!r}")


# ── the temp world ────────────────────────────────────────────────────────────

DB = {"path": None}


def fresh():
    tmp = tempfile.mkdtemp(prefix="split_")
    path = Path(tmp) / "restocker.db"
    import Restocker_db as db
    db.DB_PATH = path
    db._local.__dict__.clear()
    db.init_db()
    import ledger_migrate
    ledger_migrate.migrate(path, verbose=False)
    import ledger_v2
    ledger_v2._local.__dict__.clear()
    import split_rules as sr
    with ledger_v2._tx() as conn:
        sr.ensure_schema(conn)
    DB["path"] = path
    return db, sr, ledger_v2


def give(user_id, coins):
    import Restocker_db as db
    with db.db() as conn:
        conn.execute("INSERT OR REPLACE INTO balances (user_id, coins) VALUES (?,?)",
                     (str(user_id), int(coins)))


def coins(user_id):
    import Restocker_db as db
    with db.db() as conn:
        r = conn.execute("SELECT coins FROM balances WHERE user_id=?",
                         (str(user_id),)).fetchone()
    return int(r["coins"]) if r else 0


def world():
    """Every coin in existence, as an integer."""
    conn = sqlite3.connect(DB["path"])
    n = conn.execute("SELECT COALESCE(SUM(CAST(coins AS INTEGER)),0) FROM balances").fetchone()[0]
    conn.close()
    return int(n)


def q(sql, args=()):
    conn = sqlite3.connect(DB["path"])
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    conn.close()
    return rows


def entries(key_like):
    return q("SELECT * FROM ledger_entries WHERE idempotency_key LIKE ? ORDER BY id",
             (key_like,))


def members(mapping):
    """A resolver over a plain dict. `None` in the dict means CANNOT SAY."""
    return lambda role: mapping.get(role, None)


# ══════════════════════════════════════════════════════════════════════════
# 1. The planner, in isolation. Integers, conservation, remainders.
# ══════════════════════════════════════════════════════════════════════════

def _rule(rid, kind, ref, bps, seq=0, floor=0):
    return {"id": rid, "beneficiary_kind": kind, "beneficiary_ref": ref,
            "bps": bps, "floor_coins": floor, "seq": seq, "label": ""}


def t_planner_conserves_on_random_bps():
    """Property test: legs total the allocation, and the allocation never exceeds
    the income, for random bps sets and amounts — including sets summing to exactly
    10000 and to 1."""
    import random
    import split_rules as sr
    random.seed(20260816)
    for trial in range(3000):
        n = random.randint(1, 6)
        if trial % 7 == 0:
            # a set summing to EXACTLY 10000
            cuts = sorted(random.sample(range(1, 10000), n - 1)) if n > 1 else []
            edges = [0] + cuts + [10000]
            bpss = [edges[i + 1] - edges[i] for i in range(n)]
        elif trial % 11 == 0:
            bpss = [1] * n              # 0.01% each — everything rounds to nothing
        else:
            bpss = [random.randint(1, 10000 // n) for _ in range(n)]
        rules = [_rule(i, "account", f"u{i}", b) for i, b in enumerate(bpss)]
        amt = random.choice([0, 1, 3, 7, 99, 100, 999, 1000, 40000, 123457])
        plan = sr.plan_split(rules, amt)
        legs = sum(int(l["amount"]) for l in plan["legs"])
        eq(legs, plan["allocated"], f"trial {trial}: legs != allocated")
        truthy(plan["allocated"] <= amt, f"trial {trial}: allocated > amount_in")
        eq(plan["allocated"] + plan["retained"], amt, f"trial {trial}: retained")
        for l in plan["legs"]:
            truthy(isinstance(l["amount"], int) and l["amount"] > 0,
                   "leg amount must be a positive int")


def t_planner_role_remainder_goes_to_first_by_stable_sort():
    """3 members, 100 coins: 34/33/33 and not 33/33/33 with a coin deleted."""
    import split_rules as sr
    rules = [_rule(1, "role", "R", 10000)]
    plan = sr.plan_split(rules, 100, members({"R": ["u3", "u1", "u2"]}))
    eq(plan["allocated"], 100, "allocated")
    got = {l["to_account"]: l["amount"] for l in plan["legs"]}
    eq(got, {"u1": 34, "u2": 33, "u3": 33}, "stable-sorted remainder")
    # and it is STABLE: replanning gives the same member the same coin
    plan2 = sr.plan_split(rules, 100, members({"R": ["u2", "u3", "u1"]}))
    eq({l["to_account"]: l["amount"] for l in plan2["legs"]}, got, "not stable")


def t_planner_role_smaller_than_membership():
    """2 coins across 5 members: two members get 1, three get nothing, and the sum
    is still 2. Their version credits floor(2/5)=0 five times and debits 2."""
    import split_rules as sr
    plan = sr.plan_split([_rule(1, "role", "R", 10000)], 2,
                         members({"R": ["e", "d", "c", "b", "a"]}))
    eq(plan["allocated"], 2, "allocated")
    eq(sum(l["amount"] for l in plan["legs"]), 2, "legs")
    eq(sorted(l["to_account"] for l in plan["legs"]), ["a", "b"], "earliest two")


def t_planner_empty_role_allocates_nothing():
    """B12. A known-empty role contributes ZERO — the source is not debited for it."""
    import split_rules as sr
    plan = sr.plan_split([_rule(1, "role", "R", 2500),
                          _rule(2, "account", "vt", 1000)], 1000,
                         members({"R": []}))
    eq(plan["allocated"], 100, "only the account rule allocates")
    eq([l["to_account"] for l in plan["legs"]], ["vt"], "legs")
    truthy(any(n.get("skipped") == "role_empty" for n in plan["notes"]),
           "the empty role must be NAMED in the notes, not silent")


def t_planner_unknown_membership_raises():
    """NEW-5's rule: `== 0` is a fact, `None` is not, and they are never the same."""
    import split_rules as sr
    try:
        sr.plan_split([_rule(1, "role", "R", 2500)], 1000, members({}))
    except sr.MembersUnknown:
        return
    raise AssertionError("an unresolvable role must raise, not silently skip")


def t_planner_floor_coins():
    import split_rules as sr
    plan = sr.plan_split([_rule(1, "account", "a", 100, floor=50)], 1000)
    eq(plan["allocated"], 0, "10 coins is below the 50-coin floor")
    plan = sr.plan_split([_rule(1, "account", "a", 100, floor=50)], 10000)
    eq(plan["allocated"], 100, "100 coins clears the floor")


def t_planner_refuses_over_100_percent():
    import split_rules as sr
    try:
        sr.plan_split([_rule(1, "account", "a", 8000), _rule(2, "account", "b", 8000)], 1000)
    except sr.SplitError as e:
        truthy("mint" in str(e), "must say why")
        return
    raise AssertionError("a plan allocating more than the income must refuse")


# ══════════════════════════════════════════════════════════════════════════
# 2. Rule management
# ══════════════════════════════════════════════════════════════════════════

def t_rules_reject_over_allocation_at_write():
    db, sr, lv = fresh()
    sr.add_rule("treasury:estates", "account", "treasury:vtech", 6000)
    try:
        sr.add_rule("treasury:estates", "account", "u1", 5000)
    except sr.SplitError:
        pass
    else:
        raise AssertionError("110% of an account must be refused at write time")
    # and the refused insert must NOT be in the table — the guard is inside the tx
    eq(len(q("SELECT * FROM split_rules WHERE source_account='treasury:estates'")), 1,
       "the rolled-back insert survived")
    eq(sr.list_rules("treasury:estates")["total_bps"], 6000, "total")


def t_deactivate_is_a_flag_not_a_delete():
    db, sr, lv = fresh()
    r = sr.add_rule("src", "account", "a", 1000)
    truthy(sr.deactivate_rule(r["rule_id"]), "first deactivate wins")
    eq(sr.deactivate_rule(r["rule_id"]), False, "second is a definite no (rowcount 0)")
    eq(len(q("SELECT * FROM split_rules WHERE id=?", (r["rule_id"],))), 1,
       "the row must survive for the audit trail")


def t_editing_rules_bumps_the_version_and_therefore_the_run_id():
    db, sr, lv = fresh()
    a = sr.add_rule("src", "account", "a", 1000)
    v1 = sr.list_rules("src")["version"]
    sr.add_rule("src", "account", "b", 1000)
    v2 = sr.list_rules("src")["version"]
    truthy(v2 > v1, "version must bump")
    eq(sr.run_id_for("k", 1, "src", v1) == sr.run_id_for("k", 1, "src", v2), False,
       "a different ruleset must not replay the old run's answer")


# ══════════════════════════════════════════════════════════════════════════
# 3. Execution — conservation on every scenario
# ══════════════════════════════════════════════════════════════════════════

def t_happy_path_three_way_split():
    """The hive shape: harvester role 17%, hive owner 32%, V Tech keeps the rest."""
    db, sr, lv = fresh()
    give("treasury:hive", 100000)
    give("owner", 0)
    for u in ("h1", "h2", "h3"):
        give(u, 0)
    sr.add_rule("treasury:hive", "role", "ROLE_HARV", 1700, seq=0, label="harvesters")
    sr.add_rule("treasury:hive", "account", "owner", 3200, seq=1, label="hive owner")
    before = world()

    res = sr.run_split("hive_payout", "batch-7", "treasury:hive", 10000,
                       resolver=members({"ROLE_HARV": ["h2", "h3", "h1"]}))
    eq(res["outcome"], "applied", "outcome")
    eq(world(), before, "CONSERVATION: coins were minted or destroyed")
    eq(coins("treasury:hive"), 100000 - 1700 - 3200, "source debited exactly the allocation")
    eq(coins("owner"), 3200, "owner leg")
    # 1700 / 3 = 566 r2  ->  567, 567, 566 by ascending id
    eq((coins("h1"), coins("h2"), coins("h3")), (567, 567, 566), "role legs + remainder")
    eq(coins("h1") + coins("h2") + coins("h3"), 1700, "role legs total the rule's gross")
    run = sr.get_run(res["run_id"])
    eq(run["state"], "applied", "run state")
    eq(int(run["allocated"]), 4900, "allocated")
    eq([l["state"] for l in run["leg_rows"]], ["applied"] * 4, "per-leg markers")


def t_the_crumb_stays_with_the_source():
    """A percentage that does not divide. 1 coin at 33.33% is 0 — and the coin is
    still in the source account, not in a rounding void."""
    db, sr, lv = fresh()
    give("src", 1000)
    give("a", 0)
    sr.add_rule("src", "account", "a", 3333)
    before = world()
    res = sr.run_split("t", "x", "src", 7)
    eq(res["outcome"], "applied", "outcome")
    eq(world(), before, "CONSERVATION")
    eq(coins("a"), 2, "7 * 3333 // 10000 == 2")
    eq(coins("src"), 998, "the source keeps the crumb")

    res2 = sr.run_split("t", "y", "src", 1)
    eq(res2["outcome"], "refused", "1 coin at 33.33% allocates nothing")
    eq(res2["reason"], "nothing_to_pay", "and says so")
    eq(world(), before, "CONSERVATION")
    eq(coins("src"), 998, "nothing moved")


def t_idempotent_replay_pays_once():
    """B17. The same trigger, five times."""
    db, sr, lv = fresh()
    give("src", 10000)
    give("a", 0)
    sr.add_rule("src", "account", "a", 2500)
    before = world()
    first = sr.run_split("consignment", 42, "src", 1000)
    eq(first["outcome"], "applied", "first")
    eq(first["replayed"], False, "first is not a replay")
    for _ in range(4):
        again = sr.run_split("consignment", 42, "src", 1000)
        eq(again["outcome"], "applied", "replay outcome")
        eq(again["replayed"], True, "must be marked as a replay")
        eq(again["run_id"], first["run_id"], "same durable run id")
    eq(coins("a"), 250, "paid exactly once")
    eq(coins("src"), 9750, "debited exactly once")
    eq(world(), before, "CONSERVATION")
    eq(len(q("SELECT * FROM split_runs")), 1, "one run row")
    eq(len(entries("split:%:leg:%")), 1, "one leg entry in the ledger")


def t_run_id_does_not_move_between_attempts():
    """The key is derived from durable rows only — never uuid4, never the clock."""
    import time
    import split_rules as sr
    a = sr.run_id_for("hive_payout", "batch-7", "treasury:hive", 3)
    time.sleep(0.01)
    b = sr.run_id_for("hive_payout", "batch-7", "treasury:hive", 3)
    eq(a, b, "the same durable inputs must give the same key")
    truthy(a != sr.run_id_for("hive_payout", "batch-8", "treasury:hive", 3), "differs by trigger")


# ── short source ──────────────────────────────────────────────────────────

def t_strict_refuses_the_whole_run():
    db, sr, lv = fresh()
    give("src", 100)
    for u in ("a", "b", "c"):
        give(u, 0)
    for u in ("a", "b", "c"):
        sr.add_rule("src", "account", u, 3000)
    before = world()
    res = sr.run_split("t", 1, "src", 1000)          # plan wants 900, only 100 there
    eq(res["outcome"], "refused", "outcome")
    eq(res["retryable"], False, "strict is terminal")
    truthy("insufficient" in res["reason"], "reason names it")
    eq((coins("a"), coins("b"), coins("c")), (0, 0, 0), "NOBODY may be paid")
    eq(coins("src"), 100, "the source is untouched")
    eq(world(), before, "CONSERVATION")


def t_prorate_scales_and_records_the_shortfall():
    db, sr, lv = fresh()
    give("src", 100)
    for u in ("a", "b", "c"):
        give(u, 0)
    for u in ("a", "b", "c"):
        sr.add_rule("src", "account", u, 3000)
    sr.set_short_policy("src", "prorate")
    before = world()
    res = sr.run_split("t", 1, "src", 1000)
    eq(res["outcome"], "applied", "outcome")
    eq(res["allocated"], 100, "the whole available balance is distributed")
    eq(res["shortfall_coins"], 800, "the underpayment is written down, not hidden")
    eq(coins("a") + coins("b") + coins("c"), 100, "legs total the debit")
    eq(coins("src"), 0, "source drained to exactly the allocation")
    eq(world(), before, "CONSERVATION")
    # 100 across 3 equal rules: 33/33/34, last absorbs the remainder
    eq(sorted([coins("a"), coins("b"), coins("c")]), [33, 33, 34], "prorate remainder")


def t_defer_parks_and_the_sweep_finishes_it_after_a_top_up():
    db, sr, lv = fresh()
    give("src", 10)
    give("a", 0)
    sr.add_rule("src", "account", "a", 5000)
    sr.set_short_policy("src", "defer")
    before = world()
    res = sr.run_split("t", 1, "src", 1000)
    eq(res["state"], "pending_funds", "parked")
    eq(res["retryable"], True, "the sweep will come back")
    eq(coins("a"), 0, "nothing paid yet")
    eq(world(), before, "CONSERVATION while parked")
    # the sweep with no money still does nothing
    sr.resume_pending()
    eq(coins("a"), 0, "still nothing")
    eq(q("SELECT state FROM split_runs")[0]["state"], "pending_funds", "still parked")
    # top up, sweep again
    give("src", 1000)
    before2 = world()
    out = sr.resume_pending()
    eq(out["applied"], 1, "the sweep applied it")
    eq(coins("a"), 500, "paid in full on the retry")
    eq(world(), before2, "CONSERVATION on the resume")


def t_escrowed_coins_are_not_available_to_a_split():
    """A split spends AVAILABLE, never raw balance. Someone else's hold is not
    ours to distribute."""
    db, sr, lv = fresh()
    give("src", 1000)
    give("a", 0)
    sr.add_rule("src", "account", "a", 10000)
    lv.place_hold("estates", "src", 900, "somebody else's escrow", 3600, key="k1")
    eq(len(q("SELECT * FROM ledger_holds WHERE state='open'")), 1, "hold is open")
    before = world()
    res = sr.run_split("t", 1, "src", 1000)
    eq(res["outcome"], "refused", "a hold makes the source short")
    eq(coins("a"), 0, "nothing paid")
    eq(coins("src"), 1000, "and the escrow is intact")
    eq(world(), before, "CONSERVATION")


# ── interruption + resume ─────────────────────────────────────────────────

def t_killed_mid_run_resumes_and_pays_once():
    """Phase 2 is killed after the claim. The plan is pinned; the sweep finishes it.

    The kill is real: `_credit` is replaced with one that raises on the third leg,
    so the transaction dies with a debit and two credits already issued INSIDE it.
    """
    db, sr, lv = fresh()
    give("src", 100000)
    for u in ("a", "b", "c", "d"):
        give(u, 0)
    for i, u in enumerate("abcd"):
        sr.add_rule("src", "account", u, 1000, seq=i)
    before = world()

    real_credit = lv._credit
    calls = {"n": 0}

    def dying_credit(conn, user_id, amount, **kw):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("process killed mid-run")
        return real_credit(conn, user_id, amount, **kw)

    lv._credit = dying_credit
    try:
        res = sr.run_split("t", 1, "src", 10000)
    finally:
        lv._credit = real_credit

    # The transaction rolled back, so NOTHING is half-done.
    eq(res["outcome"], "unknown", "an ambiguous death is UNKNOWN, not refused")
    eq(world(), before, "CONSERVATION across the kill")
    eq(coins("src"), 100000, "the debit rolled back with the credits")
    eq([coins(u) for u in "abcd"], [0, 0, 0, 0], "no beneficiary is part-paid")
    eq(q("SELECT state FROM split_runs")[0]["state"], "unknown", "parked as unknown")
    truthy(sr.stuck_runs(0), "and it is VISIBLE to an operator")

    # The sweep resolves it from ledger evidence (none) and re-runs it.
    out = sr.resume_pending()
    eq(out["applied"], 1, "resumed and applied")
    eq(world(), before, "CONSERVATION after the resume")
    eq([coins(u) for u in "abcd"], [1000, 1000, 1000, 1000], "everyone paid once")
    eq(coins("src"), 96000, "debited once")
    eq(len(entries("split:%:leg:%")), 4, "four leg entries, not eight")

    # And a second sweep changes nothing.
    out2 = sr.resume_pending()
    eq(out2["applied"], 0, "nothing left to do")
    eq(coins("a"), 1000, "not paid twice")


def t_unknown_is_resolved_from_ledger_evidence_when_it_DID_land():
    """The other half of UNKNOWN: the transaction committed and the answer was lost.

    Simulated exactly — apply the run for real, then rewrite the run row back to
    `unknown` as if the process had died believing it had failed. The sweep must
    read the leg keys in `ledger_entries`, conclude it landed, and NOT pay again.
    """
    db, sr, lv = fresh()
    give("src", 10000)
    give("a", 0)
    sr.add_rule("src", "account", "a", 5000)
    res = sr.run_split("t", 1, "src", 1000)
    eq(res["outcome"], "applied", "setup")
    eq(coins("a"), 500, "setup")
    after_first = world()
    with lv._tx() as conn:
        conn.execute("UPDATE split_runs SET state='unknown', settled_at=NULL, "
                     "reason='lost the answer' WHERE run_id=?", (res["run_id"],))

    out = sr.resume_pending()
    eq(out["applied"], 1, "resolved as applied")
    eq(coins("a"), 500, "NOT paid a second time")
    eq(coins("src"), 9500, "NOT debited a second time")
    eq(world(), after_first, "CONSERVATION")
    eq(q("SELECT reason FROM split_runs")[0]["reason"],
       "resolved: ledger evidence found", "resolved from evidence, not a guess")


def t_two_concurrent_runs_pay_once():
    """Two threads, one trigger. SQLite's write lock plus the claim's rowcount are
    what decide it; one of them must see a replay and no coin may move twice."""
    import threading
    db, sr, lv = fresh()
    give("src", 100000)
    give("a", 0)
    sr.add_rule("src", "account", "a", 2500)
    before = world()
    results = []
    barrier = threading.Barrier(2)

    def go():
        import ledger_v2 as _lv
        _lv._local.__dict__.clear()      # each thread gets its own connection
        barrier.wait()
        try:
            results.append(sr.run_split("hive_payout", "batch-9", "src", 40000))
        except Exception as e:            # noqa: BLE001
            results.append({"outcome": "error", "reason": repr(e)})

    ts = [threading.Thread(target=go) for _ in range(2)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()

    eq(coins("a"), 10000, "paid exactly once")
    eq(coins("src"), 90000, "debited exactly once")
    eq(world(), before, "CONSERVATION under concurrency")
    eq(len(q("SELECT * FROM split_runs")), 1, "one run row")
    eq(len(entries("split:%:leg:%")), 1, "one leg entry")
    truthy(all(r["outcome"] in ("applied", "unknown", "refused") for r in results),
           f"unexpected outcomes: {results}")
    # At least one must have actually applied it.
    truthy(any(r.get("outcome") == "applied" for r in results),
           f"neither thread applied: {results}")


def t_two_different_triggers_both_pay():
    """The concurrency guard must not be a lock that drops real work."""
    db, sr, lv = fresh()
    give("src", 100000)
    give("a", 0)
    sr.add_rule("src", "account", "a", 1000)
    before = world()
    r1 = sr.run_split("hive_payout", "batch-1", "src", 10000)
    r2 = sr.run_split("hive_payout", "batch-2", "src", 10000)
    eq((r1["outcome"], r2["outcome"]), ("applied", "applied"), "both apply")
    eq(coins("a"), 2000, "paid for both batches")
    eq(world(), before, "CONSERVATION")


def t_empty_role_end_to_end_debits_nothing():
    """B12 against real balances: a known-empty role must not cost the source a coin."""
    db, sr, lv = fresh()
    give("src", 10000)
    give("vt", 0)
    sr.add_rule("src", "role", "R_EMPTY", 2500, seq=0)
    sr.add_rule("src", "account", "vt", 1000, seq=1)
    before = world()
    res = sr.run_split("t", 1, "src", 1000, resolver=members({"R_EMPTY": []}))
    eq(res["outcome"], "applied", "the rest of the plan still runs")
    eq(coins("vt"), 100, "the account rule paid")
    eq(coins("src"), 9900, "the source was debited 100, NOT 350")
    eq(world(), before, "CONSERVATION")


def t_unresolvable_role_moves_nothing_and_is_retryable():
    db, sr, lv = fresh()
    give("src", 10000)
    give("vt", 0)
    sr.add_rule("src", "role", "R", 2500)
    sr.add_rule("src", "account", "vt", 1000)
    before = world()
    res = sr.run_split("t", 1, "src", 1000, resolver=members({}))   # cannot say
    eq(res["outcome"], "refused", "fail safe")
    eq(res["retryable"], True, "and retryable — this is not a permanent no")
    eq(coins("vt"), 0, "no leg may be paid on a plan that could not be computed")
    eq(coins("src"), 10000, "source untouched")
    eq(world(), before, "CONSERVATION")
    eq(len(q("SELECT * FROM split_runs")), 0, "no run row pinned from an unknown")
    # once the gateway can answer, the same trigger works
    res2 = sr.run_split("t", 1, "src", 1000, resolver=members({"R": ["m1", "m2"]}))
    eq(res2["outcome"], "applied", "and then it applies")
    eq(coins("m1") + coins("m2"), 250, "role paid")
    eq(world(), before, "CONSERVATION")


def t_frozen_source_refuses_everything():
    db, sr, lv = fresh()
    give("src", 10000)
    give("a", 0)
    sr.add_rule("src", "account", "a", 5000)
    lv.set_frozen("src", True, reason="investigation")
    before = world()
    res = sr.run_split("t", 1, "src", 1000)
    eq(res["outcome"], "refused", "a frozen source is a DEFINITE refusal, not unknown")
    eq(res["retryable"], True, "and a freeze lifts, so the sweep keeps it")
    eq(coins("a"), 0, "a frozen source pays nobody")
    eq(world(), before, "CONSERVATION")


def t_no_rules_is_a_refusal_not_a_crash():
    db, sr, lv = fresh()
    give("src", 1000)
    res = sr.run_split("t", 1, "src", 500)
    eq(res["outcome"], "refused", "outcome")
    eq(res["reason"], "no_rules", "reason")
    eq(coins("src"), 1000, "untouched")


def t_a_hundred_percent_split_leaves_the_source_at_zero_and_conserves():
    db, sr, lv = fresh()
    give("src", 12345)
    for u in ("a", "b", "c"):
        give(u, 0)
    sr.add_rule("src", "account", "a", 3333, seq=0)
    sr.add_rule("src", "account", "b", 3333, seq=1)
    sr.add_rule("src", "role", "R", 3334, seq=2)
    before = world()
    res = sr.run_split("t", 1, "src", 12345, resolver=members({"R": ["c", "b"]}))
    eq(res["outcome"], "applied", "outcome")
    eq(world(), before, "CONSERVATION")
    moved = 12345 - coins("src")
    eq(moved, coins("a") + (coins("b")) + coins("c"), "debit == sum of credits")
    truthy(moved <= 12345, "never more than the income")


def t_prorate_scales_a_role_leg_without_re_resolving_it():
    """`_scale_pinned` works off `plan_json` only.

    Two things are proved. (1) The pinned expansion is used: the resolver is not
    called again during the money transaction, so a members fetch can never happen
    inside an open `BEGIN IMMEDIATE`, and a member who joins the role between the
    plan and the retry cannot change who gets paid. (2) It still conserves.
    """
    db, sr, lv = fresh()
    give("src", 300)
    for u in ("m1", "m2", "m3", "vt"):
        give(u, 0)
    sr.add_rule("src", "role", "R", 6000, seq=0)
    sr.add_rule("src", "account", "vt", 4000, seq=1)
    sr.set_short_policy("src", "prorate")

    calls = {"n": 0}

    def counting(role):
        calls["n"] += 1
        return ["m3", "m1", "m2"]

    before = world()
    res = sr.run_split("t", 1, "src", 10000, resolver=counting)
    eq(calls["n"], 1, "the role must be enumerated exactly ONCE, at plan time")
    eq(res["outcome"], "applied", "outcome")
    eq(res["allocated"], 300, "everything available is distributed")
    eq(res["shortfall_coins"], 10000 - 300, "shortfall recorded against the full plan")
    eq(coins("src"), 0, "source drained to exactly the allocation")
    eq(coins("m1") + coins("m2") + coins("m3") + coins("vt"), 300, "legs total the debit")
    eq(world(), before, "CONSERVATION")
    # 300 across 6000/4000 bps -> 180 role / 120 vt; 180/3 = 60 each, no remainder
    eq(coins("vt"), 120, "account leg")
    eq((coins("m1"), coins("m2"), coins("m3")), (60, 60, 60), "role legs")


def t_scale_pinned_conserves_on_random_caps():
    """Property test for the prorate arithmetic: the scaled legs total the cap
    exactly, for random plans and random caps — never cap-minus-a-coin-per-rule."""
    import random
    import split_rules as sr
    random.seed(816)
    for trial in range(2000):
        nrules = random.randint(1, 4)
        rules, legs, rid = [], [], 0
        for r in range(nrules):
            bps = random.randint(1, 2500)
            nmem = random.randint(1, 5)
            rules.append({"id": r, "bps": bps, "kind": "role", "ref": f"R{r}",
                          "gross": 0, "label": ""})
            for m in range(nmem):
                legs.append({"seq": rid, "rule_id": r, "kind": "role",
                             "to_account": f"r{r}m{m}", "amount": 1})
                rid += 1
        plan = {"legs": legs, "rules_used": rules}
        cap = random.choice([0, 1, 2, 7, 99, 100, 1001, 99999])
        out = sr._scale_pinned(plan, cap)
        total = sum(int(l["amount"]) for l in out["legs"])
        eq(total, out["allocated"], f"trial {trial}: legs != allocated")
        truthy(out["allocated"] <= cap, f"trial {trial}: allocated {out['allocated']} > cap {cap}")
        if cap > 0:
            # everything available is distributed — the last rule absorbs the
            # remainder, so nothing is stranded by rounding
            eq(out["allocated"], cap, f"trial {trial}: {cap - out['allocated']} coins stranded")


def t_the_conservation_checks_actually_bite():
    """A control. Give the module THEIR rounding bug and the probes must fail.

    `_expand` is replaced with the shape at `AutoSplitService.ts:179` — credit
    `floor(amount/size)` to each member while the caller debits the full leg. If
    the assertions in this file can pass with that in place, they are decoration.
    """
    import split_rules as sr
    real = sr._expand

    def their_expand(rule, gross, resolver):
        pairs, note = real(rule, gross, resolver)
        if rule["beneficiary_kind"] == "role" and pairs:
            per = gross // len(pairs)
            return [(a, per) for a, _ in pairs if per > 0], note
        return pairs, note

    sr._expand = their_expand
    try:
        try:
            sr.plan_split([_rule(1, "role", "R", 10000)], 100,
                          members({"R": ["a", "b", "c"]}))
        except sr.SplitError as e:
            # A raise, not an `assert` — so this control still bites under
            # `python -O`, which is where a stripped assertion would let the leak
            # straight through. Run this file with -O and it must still pass.
            truthy("refusing to lose" in str(e), f"unhelpful message: {e}")
            return
        raise AssertionError("the planner accepted legs that do not total the gross "
                             "— the conservation check is not doing anything")
    finally:
        sr._expand = real


# ══════════════════════════════════════════════════════════════════════════
# 4. Wiring — the mechanism is actually called
# ══════════════════════════════════════════════════════════════════════════

def t_land_settle_calls_the_split_on_its_commission():
    """A mechanism built is not a mechanism wired. Read the shipped source."""
    src = (ROOT / "land_settle.py").read_text()
    truthy("split_rules" in src, "land_settle.py does not import split_rules")
    truthy("commission_split_run" in src or "run_split" in src,
           "land_settle.py never executes a split run")
    truthy("COMMISSION_SOURCE" in src or "esc.TREASURY" in src, "no source account")


def t_the_resume_sweep_is_started_by_the_loops_cog():
    src = (ROOT / "cogs" / "loops.py").read_text()
    truthy("split_rules" in src, "cogs/loops.py does not import split_rules")
    truthy("resume_pending" in src, "cogs/loops.py never calls resume_pending")
    truthy("split_resume_loop" in src, "no loop is defined")
    truthy("set_member_resolver" in src,
           "nothing ever registers a role resolver, so every role rule refuses")
    truthy("_register_split_resolver" in src.split("async def on_ready", 1)[1],
           "the resolver is defined but never registered on ready")
    body_res = src.split("def _register_split_resolver", 1)[1].split("@commands", 1)[0]
    truthy('getattr(intents, "members", False)' in body_res,
           "the resolver must check the members intent — an uncached role is NOT empty")
    # The one that has bitten this project: a loop defined and never started.
    body = src.split("def _all_loops", 1)[1].split("def ", 1)[0]
    truthy("self.split_resume_loop" in body,
           "the loop exists but is not in _all_loops, so it never starts")


def t_land_commission_split_end_to_end():
    """The real function `land_settle.commission_split_run`, on a real ledger.

    THIS TEST USED TO EDIT NOTHING BETWEEN THE TWO SETTLES, and that is how F1
    survived: it certified "settle twice -> pays once" under an UNCHANGED ruleset
    while `commission_split_run`'s docstring claimed idempotency by the lot. One
    `set_short_policy` in the gap and the same commission paid twice. The gap is
    now part of the test, because a test whose guarantee is narrower than the
    docstring above it is worse than no test.
    """
    db, sr, lv = fresh()
    import land_settle as ls
    give("treasury:estates", 2000)
    give("treasury:vtech", 0)
    give("mkt_owner", 0)
    sr.add_rule(ls.COMMISSION_SOURCE, "account", "treasury:vtech", 7000, seq=0)
    r_owner = sr.add_rule(ls.COMMISSION_SOURCE, "account", "mkt_owner", 3000, seq=1)
    before = world()
    res = ls.commission_split_run(77, 2000)
    eq(res["outcome"], "applied", "outcome")
    eq(coins("treasury:vtech"), 1400, "platform leg")
    eq(coins("mkt_owner"), 600, "market owner leg")
    eq(coins("treasury:estates"), 0, "commission left the holding account")
    eq(world(), before, "CONSERVATION")
    # settle runs twice (a resumed settlement) -> pays once
    res2 = ls.commission_split_run(77, 2000)
    eq(res2["replayed"], True, "second settle replays")
    eq(coins("treasury:vtech"), 1400, "not paid twice")
    eq(world(), before, "CONSERVATION")

    # ── the gap that was missing (F1) ────────────────────────────────────
    # Other lots' commissions accumulate in the same house account — the normal
    # state of `treasury:estates`, not a contrivance. It is what a second run for
    # lot #77 would be paid OUT OF.
    give("treasury:estates", 50000)
    now = world()
    v1 = sr.list_rules(ls.COMMISSION_SOURCE)["version"]
    sr.set_short_policy(ls.COMMISSION_SOURCE, "prorate")     # bump 1
    sr.deactivate_rule(r_owner["rule_id"])                   # bump 2
    truthy(sr.list_rules(ls.COMMISSION_SOURCE)["version"] > v1,
           "the operator's edits must really have bumped the version")
    res3 = ls.commission_split_run(77, 2000)                 # the SAME lot again
    eq(res3["replayed"], True, "a settle after a rule edit still REPLAYS lot #77")
    eq(coins("treasury:vtech"), 1400, "lot #77's commission pays V Tech ONCE")
    eq(coins("mkt_owner"), 600, "and the market owner ONCE")
    eq(coins("treasury:estates"), 50000, "no second run drew on the house account")
    eq(world(), now, "CONSERVATION across the rule edit")
    eq(len(q("SELECT run_id FROM split_runs WHERE trigger_row_id='77'")), 1,
       "ONE income event, ONE run row, whatever the ruleset version")

    # …and the control: the edit is not ignored, it governs the NEXT lot.
    res4 = ls.commission_split_run(78, 2000)
    eq(res4["outcome"], "applied", "lot #78 runs on the CURRENT rules")
    eq(coins("treasury:vtech"), 1400 + 1400, "70% of the new lot")
    eq(coins("mkt_owner"), 600, "the retired rule pays nothing on a NEW event")
    eq(world(), now, "CONSERVATION")


def t_a_rule_edit_between_two_offers_pays_once():
    """F1 at the module level: `run_split`, one trigger, a version bump between."""
    db, sr, lv = fresh()
    give("src", 100000)
    give("a", 0)
    sr.add_rule("src", "account", "a", 5000)
    before = world()
    r1 = sr.run_split("land_commission", 77, "src", 10000)
    eq(r1["outcome"], "applied", "first offer")
    eq(coins("a"), 5000, "50% of 10000")
    sr.set_short_policy("src", "prorate")        # ONE operator action
    r2 = sr.run_split("land_commission", 77, "src", 10000)
    eq(r2["outcome"], "applied", "second offer is the stored answer")
    eq(r2["replayed"], True, "…replayed, not re-executed")
    eq(r2["run_id"], r1["run_id"], "the pinned run owns the trigger")
    eq(coins("a"), 5000, "the SAME income event must not pay twice")
    eq(coins("src"), 95000, "the source is debited once")
    eq(world(), before, "CONSERVATION")


def t_a_parked_run_plus_a_rule_edit_is_still_one_run():
    """The sequence that provokes the edit in the first place: a run parks for
    want of funds, the operator changes the rules because it parked, the money
    arrives. That must complete ONE run, not two."""
    db, sr, lv = fresh()
    give("src", 100)
    give("a", 0)
    give("b", 0)
    sr.set_short_policy("src", "defer")
    sr.add_rule("src", "account", "a", 5000)
    r1 = sr.run_split("land_commission", 5, "src", 10000)
    eq(r1["state"], "pending_funds", "parked waiting for funds")
    sr.add_rule("src", "account", "b", 1000)      # the operator edits -> bump
    give("src", 100000)          # `give` sets the balance; the float is topped up
    before = world()
    r2 = sr.run_split("land_commission", 5, "src", 10000)
    eq(r2["run_id"], r1["run_id"], "the parked run is resumed, not replaced")
    sr.resume_pending()
    eq(coins("a"), 5000, "one 10000-coin event pays 'a' 5000 once")
    eq(coins("b"), 0, "a rule written AFTER the event does not join its pinned plan")
    eq(coins("src"), 95000, "debited once")
    eq(world(), before, "CONSERVATION")
    eq([r["state"] for r in q("SELECT state FROM split_runs")], ["applied"],
       "exactly one run row, applied")


def t_find_run_answers_whether_anything_was_minted():
    """`find_run` is what tells a caller "definite refusal, nothing to retry"
    apart from "parked, the sweep has it" — see land_settle's SplitError arm."""
    db, sr, lv = fresh()
    give("src", 100000)
    give("a", 0)
    eq(sr.find_run("land_commission", 9, "src"), None, "nothing minted yet")
    sr.add_rule("src", "account", "a", 5000)
    r = sr.run_split("land_commission", 9, "src", 10000)
    found = sr.find_run("land_commission", 9, "src")
    eq((found or {}).get("run_id"), r["run_id"], "the run for this trigger")
    eq(found["state"], "applied", "and its state")
    eq(sr.find_run("land_commission", 9, "other_src"), None, "scoped by source")
    eq(sr.find_run("hive_payout", 9, "src"), None, "scoped by trigger kind")


def t_the_split_is_called_before_the_stage_marker_is_written():
    """F2, read off the shipped source. `settle_stage='seller_paid'` is what stops
    the next pass entering the block at all, and the split has no marker of its
    own until its run row exists — so a death between the marker and the call left
    the commission unrouted forever with nothing for the sweep to find. The call
    must therefore come FIRST; a second call is a replay, which is handled.

    Also asserted here: the stage claim's ROWCOUNT IS READ. It is a conditional
    UPDATE whose answer was discarded while `stage` was set locally regardless."""
    src = (ROOT / "land_settle.py").read_text()
    body = src.split('if not _reached(stage, "seller_paid"):', 1)[1].split("# ── 5.", 1)[0]
    i_mark = body.index('claim_listing_stage(listing_id, "paying_seller", "seller_paid")')
    i_split = body.index("commission_split_run(")
    truthy(i_split < i_mark,
           "commission_split_run is called AFTER settle_stage reaches 'seller_paid'; "
           "a crash in that window routes the commission never, with no run row")
    claim_line = body[body.rindex("\n", 0, i_mark) + 1:body.index("\n", i_mark)]
    truthy("if not _db.claim_listing_stage" in claim_line or
           "=" in claim_line.split("_db.claim_listing_stage")[0],
           "the stage claim's rowcount is discarded: " + claim_line.strip())


def t_a_definite_refusal_is_not_reported_as_unknown():
    """F4. `plan_split` raises before any run row exists — nothing moved and
    nothing ever will. Reporting that as UNKNOWN tells the operator to wait for a
    sweep to resolve a run that does not exist."""
    db, sr, lv = fresh()
    import land_settle as ls
    give("treasury:estates", 2000)
    sr.add_rule(ls.COMMISSION_SOURCE, "account", "a", 6000)
    # 120% total, forced past add_rule's guard — the shape a bad migration or a
    # hand-written INSERT leaves behind.
    with lv._tx() as conn:
        conn.execute("INSERT INTO split_rules (source_account, seq, beneficiary_kind,"
                     " beneficiary_ref, bps) VALUES (?,1,'account','b',6000)",
                     (ls.COMMISSION_SOURCE,))
    before = world()
    res = ls.commission_split_run(80, 2000)
    eq(res["outcome"], "refused", "a definite refusal, not UNKNOWN")
    eq(res["retryable"], False, "no run row exists, so no sweep can retry it")
    eq(coins("treasury:estates"), 2000, "nothing moved")
    eq(world(), before, "CONSERVATION")
    eq(len(q("SELECT run_id FROM split_runs")), 0, "and nothing was minted")


def t_the_rules_can_be_written_without_hand_typed_sql():
    """F3. The execution path was wired and the CONFIGURATION path was not: an
    engine John cannot turn on. `cogs/splits.py` is that surface, and this test
    asserts the two halves that have gone wrong in this project before — the file
    calls the real functions, and something actually LOADS the file."""
    cog = ROOT / "cogs" / "splits.py"
    truthy(cog.exists(), "no configuration surface exists for split_rules")
    src = cog.read_text()
    for fn in ("add_rule", "deactivate_rule", "reorder_rules", "set_short_policy",
               "list_rules", "stuck_runs", "parked_runs", "get_run"):
        truthy(f"split_rules.{fn}" in src, f"the surface never calls {fn}")
    truthy("_is_staff" in src and
           src.count("_is_staff(interaction)") >= src.count("@splits.command"),
           "every /splits command must be staff-gated")
    truthy("bps" in src and "10000 = 100%" in src,
           "shares must be typed in basis points, not as a float percentage")
    main = (ROOT / "Restocker_main.py").read_text()
    ext_block = main.split("for _ext in (", 1)[1].split("):", 1)[0]
    truthy('"cogs.splits"' in ext_block,
           "cogs/splits.py is never loaded — a surface that is not registered is "
           "the same defect as no surface at all")


def t_the_splits_cog_actually_imports_and_defines_its_commands():
    """The text scan above cannot see a decorator that raises at import. Run the
    import in a SUBPROCESS so the stubbed `Restocker_main` cannot leak into the
    rest of this suite."""
    import subprocess
    code = (
        "import sys, types;"
        "m=types.ModuleType('Restocker_main'); m.is_manager=lambda i: True; m.log=None;"
        "sys.modules['Restocker_main']=m;"
        "sys.path.insert(0, %r);"
        "import cogs.splits as s;"
        "print(','.join(sorted(c.name for c in s.SplitsCog.splits.commands)));"
        "print(s.DEFAULT_SOURCE)" % str(ROOT))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(ROOT))
    truthy(out.returncode == 0, f"cogs/splits.py does not import: {out.stderr[-600:]}")
    names, default_source = out.stdout.strip().splitlines()[:2]
    got = set(names.split(","))
    for want in ("add", "list", "policy", "remove", "run", "runs"):
        truthy(want in got, f"/splits {want} is missing (have: {sorted(got)})")
    import land_settle as ls
    eq(default_source, ls.COMMISSION_SOURCE,
       "the surface must default to the account the commission actually sits in")


def t_reorder_rules_is_all_or_nothing():
    """`reorder_rules` is new this round. It is whole-list on purpose: writing one
    rule's `seq` leaves every other rule on whatever number it happened to have,
    so "move this to the front" can land it mid-tie."""
    db, sr, lv = fresh()
    src = "treasury:estates"
    a = sr.add_rule(src, "account", "u1", 4000)["rule_id"]
    b = sr.add_rule(src, "account", "u2", 3000)["rule_id"]
    c = sr.add_rule(src, "account", "u3", 1000)["rule_id"]
    v0 = int(sr.list_rules(src)["version"])
    for bad in ([a], [a, b], [a, b, c, 99], [a, a, b, c], []):
        try:
            sr.reorder_rules(src, bad)
            raise AssertionError(f"partial/invalid order accepted: {bad}")
        except sr.SplitError:
            pass
    eq([int(r["id"]) for r in sr.list_rules(src)["rules"]], [a, b, c],
       "a refused re-order changes nothing")
    eq(int(sr.list_rules(src)["version"]), v0, "and does not bump the version")
    out = sr.reorder_rules(src, [c, b, a], by="staff")
    eq([int(r["id"]) for r in sr.list_rules(src)["rules"]], [c, b, a], "re-ordered")
    eq([int(r["seq"]) for r in sr.list_rules(src)["rules"]], [1, 2, 3],
       "seq is 1..n with no ties left for id to break")
    eq(int(out["ruleset_version"]), v0 + 1, "one bump for the whole re-order")
    # A rule retired between the read and the write loses the race, and the whole
    # re-order rolls back rather than half-applying.
    sr.deactivate_rule(b, by="someone else")
    try:
        sr.reorder_rules(src, [c, b, a])
        raise AssertionError("re-ordered a retired rule")
    except sr.SplitError:
        pass
    eq([int(r["id"]) for r in sr.list_rules(src)["rules"]], [c, a],
       "the surviving order is untouched")


def t_a_parked_run_has_a_query_of_its_own():
    """`stuck_runs` never returned `pending_funds`, and `cogs/loops.py` logs
    everything it returns at ERROR. So a run parked by the `defer` policy was
    invisible to every surface in the tree — including the log. `parked_runs`
    is the query `/splits runs` lists them from."""
    db, sr, lv = fresh()
    src = "treasury:estates"
    sr.set_short_policy(src, "defer")
    sr.add_rule(src, "account", "u1", 5000)
    give(src, 0)
    res = sr.run_split("land_commission", 91, src, 4000)
    eq(res["state"], "pending_funds", "the run parks")
    eq(sr.stuck_runs(0.0), [], "a parked run is NOT a run nobody can finish")
    parked = sr.parked_runs()
    eq(len(parked), 1, "…and it is not invisible either")
    eq(parked[0]["run_id"], res["run_id"], "the same run")
    eq(int(parked[0]["amount_in"]), 4000, "with its figure")
    eq(parked[0]["trigger_kind"], "land_commission", "and what triggered it")
    give(src, 4000)
    eq(sr.resume_pending(10)["applied"], 1, "the sweep still finishes it")
    eq(sr.parked_runs(), [], "and it leaves the parked list when it does")


def t_a_refusal_does_not_own_the_trigger_for_ever():
    """N1. A `refused` run moved no coins, so it is an answer about one ATTEMPT
    and not about the income event. When the reason for the refusal goes away —
    the source is topped up, the empty role is populated — the next offer of the
    same event must plan it again and route the commission."""
    db, sr, lv = fresh()
    src = "treasury:estates"
    sr.add_rule(src, "account", "u1", 10000)        # strict is the default
    give(src, 10)
    before = world()
    a = sr.run_split("land_commission", 700, src, 2000)
    eq(a["state"], "refused", f"a short source under strict refuses: {a}")
    eq(coins("u1"), 0, "and nobody is paid")
    eq(len(sr.unrouted_runs()), 1, "the unrouted event is visible immediately")
    eq(world(), before, "the refusal moved nothing at all")
    give(src, 5000)                                  # the coins arrive
    before = world()
    b = sr.run_split("land_commission", 700, src, 2000)
    eq(b["outcome"], "applied", f"the re-offer must route it: {b}")
    eq(b.get("replayed"), False, "and it is a fresh attempt, not a replayed refusal")
    eq(coins("u1"), 2000, "paid exactly the plan")
    eq(coins(src), 3000, "and the source is debited exactly once")
    eq(sr.unrouted_runs(), [], "it leaves the unrouted list once routed")
    eq(world(), before, "CONSERVATION")
    eq(len([r for r in q("SELECT * FROM split_runs") if r["state"] == "applied"]), 1,
       "one income event, one applied run")


def t_re_planning_a_refusal_still_pays_only_once():
    """The control on the control. Re-planning is only safe because a refused run
    moved no coins — so offer the same event FOUR more times after it applies and
    prove nothing moves again."""
    db, sr, lv = fresh()
    src = "treasury:estates"
    sr.add_rule(src, "account", "u1", 5000)
    give(src, 0)
    eq(sr.run_split("land_commission", 701, src, 1000)["state"], "refused", "refused")
    give(src, 4000)
    eq(sr.run_split("land_commission", 701, src, 1000)["outcome"], "applied", "routed")
    before = world()
    for _ in range(4):
        r = sr.run_split("land_commission", 701, src, 1000)
        eq(r["outcome"], "applied", "later offers replay")
        eq(r.get("replayed"), True, "…as a replay, not a new run")
    eq(coins("u1"), 500, "paid exactly once, whatever the offer count")
    eq(world(), before, "CONSERVATION")


def t_a_refusal_never_outranks_an_applied_or_live_run():
    """F1 MUST NOT REOPEN. `refused` no longer owns a trigger — but `applied` and
    live runs still do, at any ruleset version, and a stray refused row for the
    same event may not talk the engine into planning a second one."""
    db, sr, lv = fresh()
    src = "treasury:estates"
    sr.set_short_policy(src, "defer")
    sr.add_rule(src, "account", "u1", 10000)
    give(src, 0)
    parked = sr.run_split("land_commission", 702, src, 1000)
    eq(parked["state"], "pending_funds", "the run parks with a pinned plan")
    # A refused row for the SAME event, at another version, as a legacy database
    # or a hand-repair could leave behind.
    with lv._tx() as conn:
        conn.execute(
            "INSERT INTO split_runs (run_id, trigger_kind, trigger_row_id, "
            " source_account, amount_in, ruleset_version, short_policy, state, "
            " allocated, plan_json, service, created_at) "
            "VALUES ('split:legacyrefusal','land_commission','702',?,1000,1,"
            " 'strict','refused',0,'{}','core',1.0)", (src,))
    eq(sr._run_for_trigger.__name__, "_run_for_trigger", "sanity")
    give(src, 5000)
    sr.set_short_policy(src, "prorate")          # bump the version underneath it
    before = world()
    out = sr.run_split("land_commission", 702, src, 1000)
    eq(out["run_id"], parked["run_id"], "the LIVE run still owns the event")
    eq(out["outcome"], "applied", "and it is the one that executes")
    eq(coins("u1"), 1000, "paid once")
    eq(world(), before, "CONSERVATION")
    eq(len([r for r in q("SELECT * FROM split_runs") if r["state"] == "applied"]), 1,
       "exactly one applied run for one income event")


def t_an_unrouted_commission_is_visible_without_hand_typed_sql():
    """The other half of N1: `refused` is in neither `stuck_runs` nor
    `parked_runs`, so before `unrouted_runs` the operator's only view of a
    commission that never got routed was a hand-typed SELECT."""
    db, sr, lv = fresh()
    src = "treasury:estates"
    sr.set_member_resolver(lambda rid: [])
    sr.add_rule(src, "role", "555", 10000)
    give(src, 2000)
    res = sr.run_split("land_commission", 703, src, 2000)
    eq(res["state"], "refused", "an empty role pays nobody")
    eq(sr.stuck_runs(0.0), [], "it is not stuck — the sweep cannot help it")
    eq(sr.parked_runs(), [], "and it is not waiting on funds")
    rows = sr.unrouted_runs()
    eq(len(rows), 1, "…but it must not be invisible")
    eq(rows[0]["run_id"], res["run_id"], "the same run")
    eq(int(rows[0]["amount_in"]), 2000, "with the commission that went unrouted")
    eq(coins(src), 2000, "and the coins really are still in the source")


def t_the_configuration_surface_passes_its_own_probe():
    """F3's acceptance, by EXECUTION. `tests/probe_splits_surface.py` drives every
    `/splits` callback against a real database through a fake interaction and
    asserts against the rows. Run as a subprocess because the cog needs a stubbed
    `Restocker_main` in `sys.modules` and that must not leak into this suite."""
    import subprocess
    probe = HERE / "probe_splits_surface.py"
    truthy(probe.exists(), "the surface has no executable proof")
    out = subprocess.run([sys.executable, str(probe)], capture_output=True,
                         text=True, cwd=str(ROOT), timeout=600)
    tail = (out.stdout or "")[-1500:] + (out.stderr or "")[-1500:]
    truthy(out.returncode == 0, f"the /splits surface probe failed:\n{tail}")
    truthy(", 0 failed" in (out.stdout or ""), f"probe did not report clean:\n{tail}")


def t_land_commission_with_no_rules_is_a_no_op():
    """John must be able to deploy this with no rules configured and see NOTHING
    change. A split engine that alters behaviour before anyone writes a rule is a
    migration, not a feature."""
    db, sr, lv = fresh()
    import land_settle as ls
    give("treasury:estates", 2000)
    before = world()
    res = ls.commission_split_run(78, 2000)
    eq(res["outcome"], "refused", "no rules")
    eq(coins("treasury:estates"), 2000, "commission stays where it always did")
    eq(world(), before, "CONSERVATION")


# ══════════════════════════════════════════════════════════════════════════

def main():
    print("split_rules — planner")
    for n, f in [
        ("planner conserves on 3000 random bps sets", t_planner_conserves_on_random_bps),
        ("role remainder -> earliest member, stable", t_planner_role_remainder_goes_to_first_by_stable_sort),
        ("role smaller than membership still sums", t_planner_role_smaller_than_membership),
        ("empty role allocates nothing (B12)", t_planner_empty_role_allocates_nothing),
        ("unknown membership raises, never skips", t_planner_unknown_membership_raises),
        ("floor_coins suppresses a small leg", t_planner_floor_coins),
        ("a plan over 100% refuses", t_planner_refuses_over_100_percent),
    ]:
        check(n, f)

    print("split_rules — rules")
    for n, f in [
        ("over-allocation refused inside the write tx", t_rules_reject_over_allocation_at_write),
        ("deactivate is a flag, claim-first", t_deactivate_is_a_flag_not_a_delete),
        ("editing bumps version -> new run id", t_editing_rules_bumps_the_version_and_therefore_the_run_id),
    ]:
        check(n, f)

    print("split_rules — execution")
    for n, f in [
        ("three-way split, conserving", t_happy_path_three_way_split),
        ("the crumb stays with the source", t_the_crumb_stays_with_the_source),
        ("replay pays once (B17)", t_idempotent_replay_pays_once),
        ("run id is stable across attempts", t_run_id_does_not_move_between_attempts),
        ("short source, strict: nobody paid", t_strict_refuses_the_whole_run),
        ("short source, prorate: shortfall recorded", t_prorate_scales_and_records_the_shortfall),
        ("short source, defer: parks then finishes", t_defer_parks_and_the_sweep_finishes_it_after_a_top_up),
        ("escrowed coins are not available", t_escrowed_coins_are_not_available_to_a_split),
        ("killed mid-run, resumed, paid once", t_killed_mid_run_resumes_and_pays_once),
        ("unknown that DID land is resolved by evidence", t_unknown_is_resolved_from_ledger_evidence_when_it_DID_land),
        ("two concurrent runs pay once", t_two_concurrent_runs_pay_once),
        ("two different triggers both pay", t_two_different_triggers_both_pay),
        ("empty role end-to-end debits nothing", t_empty_role_end_to_end_debits_nothing),
        ("unresolvable role moves nothing, retryable", t_unresolvable_role_moves_nothing_and_is_retryable),
        ("frozen source pays nobody", t_frozen_source_refuses_everything),
        ("no rules is a refusal", t_no_rules_is_a_refusal_not_a_crash),
        ("100% split conserves", t_a_hundred_percent_split_leaves_the_source_at_zero_and_conserves),
        ("prorate scales the PINNED role expansion", t_prorate_scales_a_role_leg_without_re_resolving_it),
        ("prorate conserves on 2000 random caps", t_scale_pinned_conserves_on_random_caps),
        ("control: the conservation checks bite", t_the_conservation_checks_actually_bite),
    ]:
        check(n, f)

    print("split_rules — wiring")
    for n, f in [
        ("land_settle calls it", t_land_settle_calls_the_split_on_its_commission),
        ("loops cog runs the resume sweep", t_the_resume_sweep_is_started_by_the_loops_cog),
        ("land commission split, end to end (incl. a rule edit)", t_land_commission_split_end_to_end),
        ("a rule edit between two offers pays once", t_a_rule_edit_between_two_offers_pays_once),
        ("parked run + rule edit is still ONE run", t_a_parked_run_plus_a_rule_edit_is_still_one_run),
        ("find_run says whether anything was minted", t_find_run_answers_whether_anything_was_minted),
        ("the split runs BEFORE the stage marker", t_the_split_is_called_before_the_stage_marker_is_written),
        ("a definite refusal is not UNKNOWN", t_a_definite_refusal_is_not_reported_as_unknown),
        ("the rules have a configuration surface", t_the_rules_can_be_written_without_hand_typed_sql),
        ("/splits imports and defines its commands", t_the_splits_cog_actually_imports_and_defines_its_commands),
        ("/splits passes its execution probe", t_the_configuration_surface_passes_its_own_probe),
        ("reorder_rules is all-or-nothing", t_reorder_rules_is_all_or_nothing),
        ("a refusal does not own the trigger for ever", t_a_refusal_does_not_own_the_trigger_for_ever),
        ("re-planning a refusal still pays once", t_re_planning_a_refusal_still_pays_only_once),
        ("a refusal never outranks applied/live (F1)", t_a_refusal_never_outranks_an_applied_or_live_run),
        ("an unrouted commission is visible", t_an_unrouted_commission_is_visible_without_hand_typed_sql),
        ("a parked run has a query of its own", t_a_parked_run_has_a_query_of_its_own),
        ("no rules configured -> nothing changes", t_land_commission_with_no_rules_is_a_no_op),
    ]:
        check(n, f)

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    for n, e in FAIL:
        print(f"  FAIL {n}: {e}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
