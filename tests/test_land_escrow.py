"""Executable proof for `land_escrow.py`, against a real temp SQLite database.

Two ledgers are driven, on purpose, because they prove different things:

  REAL   `ledger_v2` + `ledger_migrate`, over the SAME `restocker.db` the land
         tables live in. This is what proves the CONSERVATION properties — a bid
         places a hold and not a debit; one coin cannot back two bids — because
         those are properties of the real `INSERT … WHERE available >= amt` and
         of the real escrow triggers, not of anything this file could assert.
  FAKE   a scripted ledger (`FakeLedger`). This is what proves the FAILURE
         properties — hold refused, hold outcome unknown, release refused,
         capture refused — because a healthy ledger cannot be made to lose a
         response on demand, and those are precisely the states the old debit
         code never had to have an answer for.

Nothing here touches the real `restocker.db`: every test builds its own file in a
temp directory with the same pragmas production uses (WAL, busy_timeout=5000,
foreign_keys=ON — `Restocker_db._get_conn`), which is rule 7.

Run:  python3 tests/test_land_escrow.py
"""
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
# ledger_v2 / ledger_migrate are not deployed into RestockerLocal yet — that is
# P2, and it lands before land is allowed to call them (LAND_ESCROW_PLAN §3).
# The test reads them from the build tree so the REAL implementation is exercised
# rather than a transcription of it.
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


# ── the temp world ────────────────────────────────────────────────────────────

def fresh_db(with_ledger=True):
    """A brand-new restocker.db with the land tables, and optionally ledger v2."""
    tmp = tempfile.mkdtemp(prefix="land_escrow_")
    path = Path(tmp) / "restocker.db"
    import Restocker_db as db
    db.DB_PATH = path
    db._local.__dict__.clear()          # drop any thread-local connection
    db.init_db()
    if with_ledger:
        import ledger_migrate
        ledger_migrate.migrate(path, verbose=False)
        import ledger_v2
        ledger_v2._local.__dict__.clear()
    import land_escrow as esc
    esc.set_ledger(esc.LedgerV2InProcess())
    return db, path


def give(db, user_id, coins):
    with db.db() as conn:
        conn.execute("INSERT OR REPLACE INTO balances (user_id, coins) VALUES (?,?)",
                     (str(user_id), int(coins)))


def coins_of(db, user_id):
    with db.db() as conn:
        row = conn.execute("SELECT coins FROM balances WHERE user_id=?",
                           (str(user_id),)).fetchone()
    return int(row["coins"]) if row else 0


def a_listing(db, seller="900", reserve=1000, ends_in_days=7):
    from datetime import datetime, timedelta, timezone
    ends = (datetime.now(timezone.utc) + timedelta(days=ends_in_days)
            ).strftime("%Y-%m-%d %H:%M:%S")
    return db.create_land_listing(seller_id=str(seller), kind="land", title="A plot",
                                  mode="auction", reserve=float(reserve),
                                  ends_at=ends, status="active")


def holds(path, state=None):
    import sqlite3
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM ledger_holds"
    if state:
        sql += f" WHERE state='{state}'"
    rows = [dict(r) for r in conn.execute(sql).fetchall()]
    conn.close()
    return rows



def bid(esc, lid, bidder, amount, kind="bid", ttl=None):
    """The five calls `cogs.land_exchange._place_bid_core` makes, in its order.

    Written out here rather than imported because `cogs/land_exchange.py` imports
    discord.py at module scope and cannot be loaded in a test process. That makes
    this a TRANSCRIPTION, and a transcription that drifts proves nothing — so
    `t_the_cog_actually_calls_this_sequence` reads the shipped source and asserts
    the same five calls appear in it, in this order, with no `deduct_coins`
    anywhere near them.
    """
    row = esc.create_bid_row(lid, bidder, float(amount), int(amount), kind=kind)
    row_id = int(row["id"])
    if esc.claim_placement(row_id) is None:
        return {"ok": False, "row_id": row_id, "error": "claim lost"}
    try:
        held = esc.ledger().hold(str(bidder), int(amount), f"realestate:{kind}:{lid}",
                                 ttl or (esc.BUY_HOLD_SECONDS if kind == "buy"
                                         else esc.HOLD_GRACE_SECONDS),
                                 esc.hold_key(lid, kind, row_id))
    except Exception as e:
        code = esc.ledger().error_code(e)
        known = esc.outcome_known_for(code)
        landed = esc.fail_placement(row_id, f"{code or type(e).__name__}: {e}",
                                    outcome_known=known)
        return {"ok": False, "row_id": row_id, "status": landed, "error_code": code,
                "error": str(e), "outcome_known": known}
    esc.mark_held(row_id, str(held.get("hold_id") or ""), held.get("expires_at"))
    return {"ok": True, "row_id": row_id, "status": "held",
            "hold_id": held.get("hold_id"), "expires_at": held.get("expires_at"),
            "balance": held.get("balance"), "held": held.get("held"),
            "available": held.get("available")}


def release_row(esc, row, reason="outbid"):
    """`land_settle.release_row`'s sequence: claim (unless already claimed), call,
    mark. Same transcription caveat as `bid` above."""
    row_id = int(row["id"])
    claimed = row if str(row.get("status")) == "releasing" else esc.claim_release(row_id, reason)
    if claimed is None:
        return {"ok": False, "status": esc.bid_row(row_id)["status"]}
    try:
        out = esc.ledger().release(str(claimed["hold_id"]),
                                   esc.release_key(claimed["listing_id"],
                                                   claimed.get("kind") or "bid", row_id),
                                   reason=reason)
    except Exception as e:
        code = esc.ledger().error_code(e)
        return {"ok": False, "status": esc.unclaim_release(
            row_id, str(e), outcome_known=esc.outcome_known_for(code)),
            "error_code": code}
    esc.mark_released(row_id)
    return {"ok": True, "status": "released", "hold_id": claimed["hold_id"],
            "balance": out.get("balance"), "held": out.get("held"),
            "available": out.get("available")}


def capture_row(esc, row, amount=None):
    """`land_settle`'s capture sequence: claim, call, mark."""
    row_id = int(row["id"])
    claimed = esc.claim_capture(row_id)
    if claimed is None:
        return {"ok": False, "status": esc.bid_row(row_id)["status"]}
    try:
        esc.ledger().capture(str(claimed["hold_id"]),
                             int(amount if amount is not None else claimed["hold_amount"]),
                             esc.TREASURY,
                             esc.capture_key(claimed["listing_id"],
                                             claimed.get("kind") or "bid", row_id))
    except Exception as e:
        code = esc.ledger().error_code(e)
        return {"ok": False, "status": esc.unclaim_capture(
            row_id, str(e), outcome_known=esc.outcome_known_for(code)),
            "error_code": code}
    esc.mark_captured(row_id)
    return {"ok": True, "status": "captured"}


# ══════════════════════════════════════════════════════════════════════════
# 1. A bid places a HOLD and not a debit
# ══════════════════════════════════════════════════════════════════════════

def t_bid_holds_not_debits():
    db, path = fresh_db()
    import land_escrow as esc
    lid = a_listing(db)
    give(db, "111", 50_000)

    before = coins_of(db, "111")
    res = bid(esc, lid, "111", 10_000)
    eq(res["ok"], True, "place ok")

    # The balance row is untouched. Under the old model this line read 40,000.
    eq(coins_of(db, "111"), before, "balance after a bid")
    eq(before, 50_000, "balance is the full amount")

    open_holds = holds(path, "open")
    eq(len(open_holds), 1, "exactly one open hold")
    eq(int(open_holds[0]["amount"]), 10_000, "held amount")
    eq(open_holds[0]["user_id"], "111", "hold owner")
    eq(open_holds[0]["service"], "estates", "hold service")
    # The key is on the hold, minted from the bid row id, not a uuid4.
    eq(open_holds[0]["idempotency_key"], f"land:listing:{lid}:bid:{res['row_id']}",
       "hold carries the caller-minted key")

    # available fell by exactly the bid; balance did not.
    import ledger_v2
    snap = ledger_v2.get_balance("111")
    eq(snap["balance"], 50_000, "balance")
    eq(snap["held"], 10_000, "held")
    eq(snap["available"], 40_000, "available")

    # And the ROW carries its hold id and its key — both of them.
    row = esc.bid_row(res["row_id"])
    eq(row["status"], "held", "row status")
    eq(row["hold_id"], open_holds[0]["hold_id"], "row -> hold id")
    eq(row["idem_key"], f"land:listing:{lid}:bid:{res['row_id']}", "row idem_key")
    eq(row["capture_key"], f"land:listing:{lid}:bid:{res['row_id']}:capture",
       "row capture_key")
    eq(int(row["hold_amount"]), 10_000, "row hold_amount")

    # No coin_ledger debit was written by any of this.
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) n FROM coin_ledger WHERE user_id='111'"
                         ).fetchone()["n"]
    eq(int(n), 0, "coin_ledger rows for the bidder")


# ══════════════════════════════════════════════════════════════════════════
# 2. Being outbid releases EXACTLY that hold
# ══════════════════════════════════════════════════════════════════════════

def t_outbid_releases_that_hold():
    db, path = fresh_db()
    import land_escrow as esc
    import ledger_v2
    lid = a_listing(db)
    give(db, "111", 50_000)
    give(db, "222", 50_000)

    first = bid(esc, lid, "111", 10_000)
    esc.promote_top_bid(lid, first["row_id"], "111", 10_000)
    eq(ledger_v2.get_balance("111")["available"], 40_000, "A available after bid")

    second = bid(esc, lid, "222", 20_000)
    promoted = esc.promote_top_bid(lid, second["row_id"], "222", 20_000)
    eq(promoted["ok"], True, "promotion won the row")
    eq([int(r["id"]) for r in promoted["displaced"]], [first["row_id"]],
       "exactly the previous top bid is displaced")

    rel = release_row(esc, promoted["displaced"][0], reason="outbid")
    eq(rel["ok"], True, "release ok")
    eq(rel["hold_id"], first["hold_id"], "released THAT hold, by id")

    # A's coins are byte-identical to where they started, and their reservation
    # is gone — not refunded, retired.
    snap = ledger_v2.get_balance("111")
    eq(snap["balance"], 50_000, "A balance")
    eq(snap["held"], 0, "A held")
    eq(snap["available"], 50_000, "A available restored exactly")

    states = {h["hold_id"]: h["state"] for h in holds(path)}
    eq(states[first["hold_id"]], "released", "first hold state")
    eq(states[second["hold_id"]], "open", "second hold state")
    eq(esc.bid_row(first["row_id"])["status"], "released", "first row status")

    # The release key names the bid, not the reason: outbid / cancel / expiry are
    # one money event and share it, so a cancel racing the sweeper replays.
    with __import__("sqlite3").connect(path) as c:
        c.row_factory = __import__("sqlite3").Row
        key = c.execute("SELECT terminal_key FROM ledger_holds WHERE hold_id=?",
                        (first["hold_id"],)).fetchone()["terminal_key"]
    eq(key, f"land:listing:{lid}:bid:{first['row_id']}:release", "release key")

    # And exactly one open hold remains on the lot: never two, never none.
    eq(len(esc.held_rows(lid)), 1, "one held row on the lot")


def t_outbid_cycle_conserves_exactly():
    """200 outbid cycles; every balance comes back byte-identical (§2.3 verify)."""
    db, path = fresh_db()
    import land_escrow as esc
    import ledger_v2
    lid = a_listing(db)
    give(db, "111", 10_000_000)
    give(db, "222", 10_000_000)
    start = {u: coins_of(db, u) for u in ("111", "222")}

    prev, amount = None, 1_000
    for i in range(200):
        bidder = "111" if i % 2 == 0 else "222"
        amount += 1_000
        res = bid(esc, lid, bidder, amount)
        eq(res["ok"], True, f"bid {i}")
        promoted = esc.promote_top_bid(lid, res["row_id"], bidder, amount)
        eq(promoted["ok"], True, f"promote {i}")
        for row in promoted["displaced"]:
            eq(release_row(esc, row)["ok"], True, f"release {i}")
        prev = res
    # Everyone's coins are untouched throughout — no refund was ever computed.
    for u in ("111", "222"):
        eq(coins_of(db, u), start[u], f"{u} balance after 200 cycles")
    eq(len(holds(path, "open")), 1, "one open hold at the end")
    eq(ledger_v2.get_balance(prev["hold_id"] and "111")["held"] +
       ledger_v2.get_balance("222")["held"], amount, "total held == top bid")


# ══════════════════════════════════════════════════════════════════════════
# 3. One coin cannot back two bids
# ══════════════════════════════════════════════════════════════════════════

def t_one_coin_one_bid():
    """A wallet with exactly one bid's worth of coins backs exactly one bid.

    Asserted against `ledger_holds`, not against the return value — §2.2's verify
    (i) says so, because the old code's return value was the thing that lied.
    """
    db, path = fresh_db()
    import land_escrow as esc
    lot_a, lot_b = a_listing(db), a_listing(db)
    give(db, "111", 10_000)

    first = bid(esc, lot_a, "111", 10_000)
    second = bid(esc, lot_b, "111", 10_000)

    eq(first["ok"], True, "first bid")
    eq(second["ok"], False, "second bid refused")
    eq(second["error_code"], "insufficient", "refused with the right code")
    eq(second["status"], "failed", "a DEFINITE refusal is `failed`, not unknown")
    eq(second["outcome_known"], True, "core provably moved nothing")

    open_holds = holds(path, "open")
    eq(len(open_holds), 1, "exactly one open hold in the ledger")
    eq(sum(int(h["amount"]) for h in open_holds), 10_000, "total reserved")
    eq(coins_of(db, "111"), 10_000, "the coins never moved")

    # The refused row is not a phantom high bid: it never reached `held`.
    eq(esc.held_rows(lot_b), [], "no held row on the second lot")


def t_partial_availability_is_the_write():
    """The availability test is inside the INSERT, so a drained wallet cannot bid.

    The audit's §2 race: the old code read the balance, then debited. Here there
    is no read to race — the check IS the write.
    """
    db, path = fresh_db()
    import land_escrow as esc
    lid = a_listing(db)
    give(db, "111", 10_000)
    a = bid(esc, lid, "111", 6_000)
    eq(a["ok"], True, "6,000 of 10,000")
    b = bid(esc, lid, "111", 6_000)
    eq(b["ok"], False, "second 6,000 refused — 4,000 available")
    eq(b["error_code"], "insufficient", "code")
    c = bid(esc, lid, "111", 4_000)
    eq(c["ok"], True, "4,000 fits exactly")
    eq(sum(int(h["amount"]) for h in holds(path, "open")), 10_000, "fully reserved")


# ══════════════════════════════════════════════════════════════════════════
# 4. A crash between recording the bid and placing the hold
# ══════════════════════════════════════════════════════════════════════════

class Crash(RuntimeError):
    pass


class CrashingLedger:
    """A ledger that dies mid-call, the way a killed process does."""

    def __init__(self, inner):
        self.inner = inner

    def available(self):
        return True

    def hold(self, *a, **kw):
        raise Crash("process died before the hold was placed")

    def __getattr__(self, name):
        return getattr(self.inner, name)


def t_crash_before_hold_leaves_no_phantom_bid():
    db, path = fresh_db()
    import land_escrow as esc
    lid = a_listing(db)
    give(db, "111", 50_000)

    real = esc.set_ledger(CrashingLedger(esc.ledger()))
    res = bid(esc, lid, "111", 10_000)
    esc.set_ledger(real)

    eq(res["ok"], False, "the bid failed")
    # An unknown outcome, NOT a refusal: a Crash carries no ledger error code, so
    # `outcome_known_for('')` is False and the row keeps its key.
    eq(res["status"], "place_unknown", "row landed in place_unknown")
    row = esc.bid_row(res["row_id"])
    eq(row["hold_id"], None, "no hold id was written")
    eq(row["idem_key"], f"land:listing:{lid}:bid:{res['row_id']}",
       "the key survives, which is what makes the replay possible")

    # THE ASSERTION THIS TEST EXISTS FOR: no phantom high bid.
    listing = db.get_land_listing(lid)
    eq(listing["current_bid"], None, "listing has no current_bid")
    eq(listing["current_bidder"], None, "listing has no current_bidder")
    eq(esc.held_rows(lid), [], "no row claims to hold coins")
    eq(holds(path, "open"), [], "no hold exists at core")
    eq(coins_of(db, "111"), 50_000, "no coins moved")

    # And the resume pass either places it or replays it — never a second hold.
    n = esc.replay_placements(older_than_minutes=0)
    eq(n, 1, "one placement replayed")
    eq(esc.bid_row(res["row_id"])["status"], "held", "row recovered to held")
    eq(len(holds(path, "open")), 1, "exactly ONE hold after the replay")


def t_replay_of_a_landed_hold_does_not_double_reserve():
    """The response was lost but the hold LANDED. The replay must not reserve twice."""
    db, path = fresh_db()
    import land_escrow as esc

    class LosesTheAnswer:
        def __init__(self, inner):
            self.inner = inner
            self.armed = True

        def available(self):
            return True

        def hold(self, *a, **kw):
            out = self.inner.hold(*a, **kw)      # it really lands at core
            if self.armed:
                self.armed = False
                raise Crash("connection dropped after commit")
            return out

        def __getattr__(self, name):
            return getattr(self.inner, name)

    lid = a_listing(db)
    give(db, "111", 15_000)
    real = esc.set_ledger(LosesTheAnswer(esc.ledger()))
    res = bid(esc, lid, "111", 10_000)
    eq(res["status"], "place_unknown", "unknown, because it might have landed")
    eq(len(holds(path, "open")), 1, "and it did land")

    esc.replay_placements(older_than_minutes=0)
    esc.set_ledger(real)
    eq(len(holds(path, "open")), 1, "STILL exactly one hold after the replay")
    row = esc.bid_row(res["row_id"])
    eq(row["status"], "held", "row recovered")
    eq(row["hold_id"], holds(path, "open")[0]["hold_id"], "and it names the real hold")
    import ledger_v2
    eq(ledger_v2.get_balance("111")["available"], 5_000, "reserved once, not twice")


# ══════════════════════════════════════════════════════════════════════════
# 5. Anti-snipe: a lot extended to the cap still has live holds
# ══════════════════════════════════════════════════════════════════════════

def t_extension_tracks_the_lot_to_the_cap():
    """Bid repeatedly into the anti-snipe window; the hold tracks ends_at + 24h.

    The cap is what makes this provable: `max_auction_days` bounds `ends_at`, so
    the TTL asked for is bounded too and can never reach `MAX_HOLD_SECONDS`, which
    is the `bad_expiry` wall that would leave a lot unsettleable.
    """
    from datetime import datetime, timedelta, timezone
    db, path = fresh_db()
    import land_escrow as esc
    import ledger_v2

    def epoch_of(ts):
        return int(datetime.strptime(str(ts)[:19].replace("T", " "),
                                     "%Y-%m-%d %H:%M:%S")
                   .replace(tzinfo=timezone.utc).timestamp())

    now = datetime.now(timezone.utc)
    starts = now.strftime("%Y-%m-%d %H:%M:%S")
    lid = db.create_land_listing(seller_id="900", kind="land", title="Sniped plot",
                                 mode="auction", reserve=1000.0, starts_at=starts,
                                 ends_at=(now + timedelta(minutes=5)).strftime(
                                     "%Y-%m-%d %H:%M:%S"),
                                 anti_snipe_minutes=5, status="active")
    give(db, "111", 10_000_000)
    res = bid(esc, lid, "111", 10_000)
    esc.promote_top_bid(lid, res["row_id"], "111", 10_000)

    MAX_DAYS = 14                      # DEF["max_auction_days"] in the H4 hotfix
    hard_ts = epoch_of(starts) + MAX_DAYS * 86400
    ends_ts = epoch_of(db.get_land_listing(lid)["ends_at"])
    # Walk the lot forward in anti-snipe steps until the cap bites, extending the
    # hold each time exactly as `_place_bid_core` must.
    steps = 0
    while steps < 5000:
        want = ends_ts + 5 * 60          # a bid inside the window pushes it out
        capped = min(want, hard_ts)
        if capped <= ends_ts:
            break                        # the cap has bitten; extensions stop
        ends_ts = capped
        db.update_land_listing(lid, ends_at=datetime.fromtimestamp(
            ends_ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
        esc.extend_for_listing(lid, ends_ts, int(now.timestamp()))
        steps += 1
    if steps < 100:
        raise AssertionError(f"expected the lot to extend many times, got {steps}")
    eq(ends_ts, hard_ts, "ends_at walked exactly to the cap and stopped")

    # THE ASSERTION: at the cap, the hold is still OPEN and still outlives the lot.
    hold = ledger_v2.get_hold("estates", esc.bid_row(res["row_id"])["hold_id"])
    eq(hold["state"], "open", "hold is still open at the cap")
    if epoch_of(hold["expires_at"]) < ends_ts + esc.HOLD_GRACE_SECONDS - 5:
        raise AssertionError(
            f"hold expires {hold['expires_at']} but the lot closes at "
            f"{datetime.fromtimestamp(ends_ts, timezone.utc)} — the lot would "
            f"outlive its escrow")
    # The mirror on the row agrees with core, which is what the guard sweep reads.
    eq(esc.bid_row(res["row_id"])["hold_expires_at"], hold["expires_at"],
       "row mirrors core's expiry")
    # A capture at the cap therefore still works.
    eq(capture_row(esc, esc.bid_row(res["row_id"]))["ok"], True, "capture at the cap")


def t_guard_sweep_repairs_a_missed_extension():
    """Crash between the ends_at write and the extend: the sweep repairs it."""
    from datetime import datetime, timedelta, timezone
    db, path = fresh_db()
    import land_escrow as esc
    import ledger_v2

    def epoch_of(ts):
        return int(datetime.strptime(str(ts)[:19].replace("T", " "),
                                     "%Y-%m-%d %H:%M:%S")
                   .replace(tzinfo=timezone.utc).timestamp())

    now = datetime.now(timezone.utc)
    lid = db.create_land_listing(seller_id="900", kind="land", title="Plot",
                                 mode="auction", reserve=1000.0,
                                 ends_at=(now + timedelta(hours=2)).strftime(
                                     "%Y-%m-%d %H:%M:%S"), status="active")
    give(db, "111", 100_000)
    res = bid(esc, lid, "111", 10_000)
    esc.promote_top_bid(lid, res["row_id"], "111", 10_000)

    # The lot is extended by ten days; the process dies before extend_hold.
    new_end = now + timedelta(days=10)
    db.update_land_listing(lid, ends_at=new_end.strftime("%Y-%m-%d %H:%M:%S"))
    listing = db.get_land_listing(lid)
    before = ledger_v2.get_hold("estates", res["hold_id"])["expires_at"]
    if epoch_of(before) > epoch_of(listing["ends_at"]):
        raise AssertionError("setup wrong: the hold already outlives the lot")

    fixed = esc.sweep_hold_extensions([listing], epoch_of)
    eq(fixed, 1, "the sweep repaired one lot")
    after = ledger_v2.get_hold("estates", res["hold_id"])["expires_at"]
    if epoch_of(after) < epoch_of(listing["ends_at"]) + esc.HOLD_GRACE_SECONDS - 5:
        raise AssertionError(f"hold {after} still does not outlive the lot")
    eq(esc.bid_row(res["row_id"])["hold_expires_at"], after, "row mirrors core")


# ══════════════════════════════════════════════════════════════════════════
# 6. Instant buy: hold then capture, and the compensating path
# ══════════════════════════════════════════════════════════════════════════

def t_instant_buy_hold_then_capture_then_seller():
    db, path = fresh_db()
    import land_escrow as esc
    lid = a_listing(db, seller="900")
    give(db, "111", 100_000)

    res = bid(esc, lid, "111", 40_000, kind="buy")
    eq(res["ok"], True, "hold placed")
    eq(coins_of(db, "111"), 100_000, "no debit at hold time")
    eq(esc.bid_row(res["row_id"])["kind"], "buy", "kind is buy")
    eq(holds(path, "open")[0]["idempotency_key"], f"land:listing:{lid}:buy:{res['row_id']}",
       "the buy key, not the bid key")

    cap = capture_row(esc, esc.bid_row(res["row_id"]))
    eq(cap["ok"], True, "captured")
    eq(coins_of(db, "111"), 60_000, "buyer paid exactly once")
    eq(coins_of(db, "treasury:estates"), 40_000, "the house holds the hammer")

    commission = 2_000
    esc.ledger().transfer(esc.TREASURY, "900", 40_000 - commission,
                          esc.seller_key(lid), reason=f"realestate:sale:{lid}")
    eq(coins_of(db, "900"), 38_000, "seller net")
    eq(coins_of(db, "treasury:estates"), commission,
       "commission stays as REAL coins in a real account")

    # The whole sale conserves: nothing was minted and nothing burned.
    eq(coins_of(db, "111") + coins_of(db, "900") + coins_of(db, "treasury:estates"),
       100_000, "coin supply unchanged")

    # A replayed settlement moves nothing more.
    again = capture_row(esc, esc.bid_row(res["row_id"]))
    eq(again["ok"], False, "a captured row cannot be claimed for capture again")
    eq(again["status"], "captured", "and it is already captured")
    again = esc.ledger().transfer(esc.TREASURY, "900", 38_000, esc.seller_key(lid))
    eq(again.get("replayed"), True, "the seller transfer replays on the same key")
    eq(coins_of(db, "900"), 38_000, "seller still paid exactly once")


def t_aborted_buy_releases_rather_than_refunds():
    """A settlement that aborts before the capture releases. No compensating credit."""
    db, path = fresh_db()
    import land_escrow as esc
    import ledger_v2
    lid = a_listing(db)
    give(db, "111", 100_000)
    res = bid(esc, lid, "111", 40_000, kind="buy")
    eq(ledger_v2.get_balance("111")["available"], 60_000, "reserved")

    rel = release_row(esc, esc.bid_row(res["row_id"]), reason="settlement aborted")
    eq(rel["ok"], True, "released")
    eq(coins_of(db, "111"), 100_000, "balance never changed at any point")
    eq(ledger_v2.get_balance("111")["available"], 100_000, "fully available again")
    with db.db() as conn:
        n = conn.execute("SELECT COUNT(*) n FROM coin_ledger").fetchone()["n"]
    eq(int(n), 0, "no refund row: there was nothing to compensate")


# ══════════════════════════════════════════════════════════════════════════
# 7. The four states the old code never had to handle
# ══════════════════════════════════════════════════════════════════════════

class FakeLedger:
    """A ledger that answers however the test needs, including badly."""

    class Error(Exception):
        def __init__(self, code, detail=""):
            super().__init__(f"{code}: {detail}")
            self.code = code

    def __init__(self):
        self.script = {}
        self.state = {}
        self.n = 0
        self.calls = []

    def available(self):
        return True

    def error_code(self, exc):
        return str(getattr(exc, "code", "") or "")

    def _maybe_raise(self, op):
        self.calls.append(op)
        outcome = self.script.get(op)
        if outcome is None:
            return
        if isinstance(outcome, list):
            outcome = outcome.pop(0) if outcome else None
        if outcome:
            raise outcome

    def hold(self, user_id, amount, reason, expires_in, key):
        self._maybe_raise("hold")
        self.n += 1
        hid = f"h{self.n}"
        self.state[hid] = "open"
        return {"hold_id": hid, "expires_at": "2026-09-01 00:00:00",
                "balance": 100, "held": amount, "available": 100 - amount}

    def release(self, hold_id, key, reason=""):
        self._maybe_raise("release")
        self.state[hold_id] = "released"
        return {"hold_id": hold_id, "state": "released"}

    def capture(self, hold_id, amount, to_user, key, reason=""):
        self._maybe_raise("capture")
        self.state[hold_id] = "captured"
        return {"hold_id": hold_id, "state": "captured"}

    def transfer(self, *a, **kw):
        self._maybe_raise("transfer")
        return {"ok": True}

    def extend(self, hold_id, expires_in):
        self._maybe_raise("extend")
        return {"hold_id": hold_id, "expires_at": "2026-09-02 00:00:00"}

    def get(self, hold_id):
        return {"hold_id": hold_id, "state": self.state.get(hold_id, "open")}


def _fake_world():
    db, path = fresh_db(with_ledger=False)
    import land_escrow as esc
    fake = FakeLedger()
    esc.set_ledger(fake)
    return db, esc, fake


def t_hold_refused_is_failed_with_figures():
    db, esc, fake = _fake_world()
    lid = a_listing(db)
    fake.script["hold"] = FakeLedger.Error("insufficient", "111 has 10 available, needs 500")
    res = bid(esc, lid, "111", 500)
    eq(res["ok"], False, "refused")
    eq(res["status"], "failed", "a definite refusal is terminal")
    eq(res["outcome_known"], True, "core provably moved nothing")
    eq(esc.bid_row(res["row_id"])["hold_id"], None, "no hold id")
    eq("has 10 available" in res["error"], True, "the figures come back for the user")


def t_hold_unknown_is_never_failed():
    db, esc, fake = _fake_world()
    lid = a_listing(db)
    fake.script["hold"] = FakeLedger.Error("internal_error", "core blew up")
    res = bid(esc, lid, "111", 500)
    eq(res["status"], "place_unknown", "unknown, not failed")
    eq(res["outcome_known"], False, "no answer is not an answer")
    # idempotency_in_progress is retryable and equally not a refusal.
    fake.script["hold"] = FakeLedger.Error("idempotency_in_progress", "in flight")
    res2 = bid(esc, lid, "111", 500)
    eq(res2["status"], "place_unknown", "in_progress is not a refusal either")
    # ... while a validation refusal IS one.
    fake.script["hold"] = FakeLedger.Error("bad_expiry", "out of range")
    res3 = bid(esc, lid, "111", 500)
    eq(res3["status"], "failed", "bad_* is matched by prefix")


def t_release_refused_and_unknown():
    db, esc, fake = _fake_world()
    lid = a_listing(db)
    res = bid(esc, lid, "111", 500)

    # hold_not_open is NOT a definite refusal: core terminated it, and only core
    # knows whether that was a capture.
    fake.script["release"] = FakeLedger.Error("hold_not_open", "already terminal")
    out = release_row(esc, esc.bid_row(res["row_id"]))
    eq(out["status"], "release_unknown", "unknown, never back to held")

    # The reconciler asks core and records the answer FORWARD.
    fake.state[res["hold_id"]] = "captured"
    eq(esc.reconcile_holds(older_than_minutes=0), 1, "one row reconciled")
    eq(esc.bid_row(res["row_id"])["status"], "captured",
       "a captured hold makes the row captured, so the settlement can see the coin")

    # A DEFINITE refusal returns to held and parks after MAX_HOLD_REFUSALS.
    db2, esc2, fake2 = _fake_world()
    lid2 = a_listing(db2)
    r2 = bid(esc2, lid2, "111", 500)
    fake2.script["release"] = [FakeLedger.Error("frozen", "wallet frozen")] * 5
    landed = []
    for _ in range(esc2.MAX_HOLD_REFUSALS):
        landed.append(release_row(esc2, esc2.bid_row(r2["row_id"]))["status"])
    eq(landed[:-1], ["held"] * (esc2.MAX_HOLD_REFUSALS - 1), "retries stay held")
    eq(landed[-1], "release_refused", "and then it parks for a human")
    eq([int(r["id"]) for r in esc2.refused_rows()], [r2["row_id"]],
       "the parked row is listed, not lost")
    # A parked release is still claimable by a release: the hold is still open and
    # the coins are still the bidder's.
    eq("capture_refused" in esc2.RELEASABLE_STATUSES, True,
       "a refused CAPTURE can still be released — the punter gets their coins back")
    eq("release_refused" in esc2.RELEASABLE_STATUSES, False,
       "a refused RELEASE is not re-claimable — that is the loop parking stops")


def t_capture_refused_and_unknown():
    db, esc, fake = _fake_world()
    lid = a_listing(db)
    res = bid(esc, lid, "111", 500)
    fake.script["capture"] = FakeLedger.Error("rate_limited", "core shedding load")
    out = capture_row(esc, esc.bid_row(res["row_id"]))
    eq(out["status"], "capture_unknown", "a lost capture is never `held` again")
    # Which is the whole point: a `held` row here would be released forever
    # against a hold core had already captured.
    eq(esc.rows_in_doubt(lid)[0]["id"], res["row_id"], "and it blocks the settlement")
    fake.state[res["hold_id"]] = "captured"
    esc.reconcile_holds(older_than_minutes=0)
    eq(esc.bid_row(res["row_id"])["status"], "captured", "resolved forward")
    eq(esc.rows_in_doubt(lid), [], "no longer in doubt")


def t_reconcile_never_moves_a_row_backwards():
    db, esc, fake = _fake_world()
    lid = a_listing(db)
    res = bid(esc, lid, "111", 500)
    capture_row(esc, esc.bid_row(res["row_id"]))
    try:
        esc.reconcile_hold(res["row_id"], "open")
        raise AssertionError("reconciling a captured row back to held was allowed")
    except ValueError as e:
        eq("history, not a draft" in str(e), True, "it says why")
    try:
        esc.reconcile_hold(res["row_id"], "banana")
        raise AssertionError("an unknown core state was accepted")
    except ValueError as e:
        eq("does not know how to record" in str(e), True, "ask, do not guess")


def t_keys_are_stable_across_rereads():
    db, esc, fake = _fake_world()
    lid = a_listing(db)
    res = bid(esc, lid, "111", 500)
    rid = res["row_id"]
    row1, row2 = esc.bid_row(rid), esc.bid_row(rid)
    eq(row1["idem_key"], row2["idem_key"], "same key on re-read")
    eq(row1["idem_key"], esc.hold_key(lid, "bid", rid), "and it is derivable")
    eq(row1["capture_key"], esc.capture_key(lid, "bid", rid), "capture key too")
    eq(esc.release_key(lid, "bid", rid), row1["idem_key"] + ":release",
       "the release key needs no column — it is the hold key plus a suffix")
    eq("uuid" in row1["idem_key"].lower(), False, "no uuid4 anywhere")


def t_promote_refuses_a_lower_bid():
    """Two bidders race one lot. Both get holds; the board never goes backwards."""
    db, esc, fake = _fake_world()
    lid = a_listing(db)
    hi = bid(esc, lid, "111", 5_000)
    lo = bid(esc, lid, "222", 3_000)
    eq(esc.promote_top_bid(lid, hi["row_id"], "111", 5_000)["ok"], True, "high wins")
    late = esc.promote_top_bid(lid, lo["row_id"], "222", 3_000)
    eq(late["ok"], False, "a lower bid cannot take the board")
    eq(float(db.get_land_listing(lid)["current_bid"]), 5_000.0, "board shows the high bid")
    # The loser's hold is the CALLER's to release — say so loudly in the result.
    eq("was not the highest" in late["error"], True, "and it says why")


def t_legacy_rows_are_untouchable():
    """A pre-escrow bid row is in no set, so no sweep can ever act on it."""
    db, esc, fake = _fake_world()
    lid = a_listing(db)
    old_id = db.add_land_bid(lid, "111", 4321.5)      # the two-year-old call shape
    row = esc.bid_row(old_id)
    eq(row["status"], "legacy", "it takes the schema default")
    eq(row["hold_id"], None, "and it has no hold")
    eq(esc.held_rows(lid), [], "held_rows ignores it")
    eq(esc.rows_in_doubt(lid), [], "rows_in_doubt ignores it")
    eq(esc.needing_attention(0), [], "no sweep will pick it up")
    eq(release_row(esc, row)["ok"], False, "and releasing it is refused")
    eq(esc.claim_release(old_id) is None, True, "`legacy` is in no releasable set")


def t_capture_refuses_a_row_with_no_stored_integer():
    """`hold_amount` is the instruction. A row without one is not capturable."""
    db, esc, fake = _fake_world()
    lid = a_listing(db)
    res = bid(esc, lid, "111", 500)
    with db.db() as conn:
        conn.execute("UPDATE land_bids SET hold_amount=NULL WHERE id=?",
                     (res["row_id"],))
    try:
        esc.row_coins(esc.bid_row(res["row_id"]))
        raise AssertionError("a row with no hold_amount was given one")
    except ValueError as e:
        eq("refusing to derive one from the float column" in str(e), True,
           "rather than rounding a float at capture time")



# ══════════════════════════════════════════════════════════════════════════
# 8. The wiring — a mechanism built is not a mechanism wired
# ══════════════════════════════════════════════════════════════════════════

def _cog_source():
    return (ROOT / "cogs" / "land_exchange.py").read_text(encoding="utf-8")


def _func_source(name, src=None):
    """Lift ONE function out of the cog by AST span — not by copying it.

    `cogs/land_exchange.py` imports discord.py at module scope, so the file
    cannot be imported in a test process. Lifting the span means the bytes under
    test are the shipped bytes; a copy would drift the moment somebody edits one
    of the two.
    """
    import ast
    src = src or _cog_source()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} is not a function in the cog any more")


def _func_body_source(name, src=None):
    """The same span with the docstring removed — for "does it CALL x" questions.

    A docstring that names `deduct_coins` in order to say it is gone would
    otherwise fail the very check it is describing.
    """
    import ast
    src = src or _cog_source()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                                     and isinstance(node.body[0].value, ast.Constant)
                                     and isinstance(node.body[0].value.value, str)) else node.body
            return "\n".join(ast.get_source_segment(src, st) for st in body)
    raise AssertionError(f"{name} is not a function in the cog any more")


def _assign_source(name, src=None):
    """The source span of a module-level assignment, by AST — for table checks."""
    import ast
    src = src or _cog_source()
    for node in ast.parse(src).body:
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        for t in targets:
            if isinstance(t, ast.Name) and t.id == name:
                return ast.get_source_segment(src, node)
    raise AssertionError(f"{name} is not a module-level assignment in the cog any more")


def _code_only(src_seg):
    """`src_seg` with every comment cut off, and nothing else moved.

    EVERY substring check against a lifted span must go through this, and that is
    not tidiness — it is a defect this file already shipped once. The first draft
    of the `/realestate close` test searched the raw span, and the long comment
    explaining the new reader mentions "no longer reserved", "still reserved" and
    `sweep_terminal_listing_holds`. A mutation that deleted the SENTENCES and left
    the comment passed every check: the test was measuring its own explanation.
    The same thing then hid a mutation of `_post_bid`.

    Truncating each line at the comment's own start column keeps the rest of the
    source byte-identical — rejoining tokens would break up the string literals
    these checks look for. `tokenize` rather than a regex, because a `#` inside a
    string literal is not a comment.
    """
    import io
    import tokenize
    lines = src_seg.splitlines()
    try:
        for tok in tokenize.generate_tokens(io.StringIO(src_seg).readline):
            if tok.type == tokenize.COMMENT:
                row, col = tok.start
                lines[row - 1] = lines[row - 1][:col]
    except (tokenize.TokenError, IndentationError) as e:
        # A LIFTED SPAN IS NOT A MODULE. `_func_body_source` joins statement
        # segments, so the first line of each sits at column 0 while its
        # continuations keep the original indentation — no amount of dedenting
        # makes that a legal block. Strip the WHOLE module instead and lift from
        # the stripped text: `_cog_source_nocomments()` does exactly that, and
        # truncating in place keeps every line number and column intact, so the
        # spans come out identical minus the prose.
        raise AssertionError(
            f"_code_only was handed a fragment that does not tokenize ({e}). "
            f"Lift from _cog_source_nocomments() instead of stripping a span.")
    return "\n".join(lines)


def _cog_source_nocomments():
    """The shipped cog with every comment blanked, line numbers unchanged."""
    return _code_only(_cog_source())


def t_code_only_actually_strips_comments():
    """The stripper is itself load-bearing, so it gets its own check.

    If `_code_only` silently became a pass-through, six assertions in this file
    would keep passing while measuring prose. That is exactly the failure it was
    added to stop, so it is not allowed to fail quietly.
    """
    src = ('def f():\n'
           '    # sentinel_in_a_comment\n'
           '    x = "sentinel_in_a_string"  # trailing_sentinel\n'
           '    return x  # noqa\n')
    out = _code_only(src)
    eq("sentinel_in_a_comment" in out, False, "_code_only left a whole-line comment")
    eq("trailing_sentinel" in out, False, "_code_only left a trailing comment")
    eq("sentinel_in_a_string" in out, True,
       "_code_only ate a string literal — every check that uses it is now blind")
    # Columns and indentation are preserved on purpose — the stripped text is
    # re-parsed by AST, so moving anything would break the span lift.
    eq(_code_only('if True:\n    x = "a # b"  # gone\n'),
       'if True:\n    x = "a # b"  ',
       "_code_only treated a `#` inside a string literal as a comment, or moved "
       "the text it was only supposed to truncate")


def t_the_cog_actually_calls_this_sequence():
    """`_place_bid_core` really does place a hold, in the order this file tests."""
    body = _func_body_source("_place_bid_core")
    order = ["_esc.escrow_available", "_esc.create_bid_row", "_esc.claim_placement",
             "_esc.ledger().hold", "_esc.mark_held", "_esc.promote_top_bid",
             "_settle.release_row", "_esc.extend_for_listing"]
    at = -1
    for call in order:
        found = body.find(call, at + 1)
        if found <= at:
            raise AssertionError(f"`{call}` is missing from _place_bid_core, or is "
                                 f"out of order relative to the call before it")
        at = found
    # The failure branch must exist and must not lie to the bidder.
    eq("_esc.fail_placement" in body, True, "an unplaced hold is recorded")
    eq("outcome_known=known" in body, True, "and its outcome judgement is passed on")
    # And the debit is gone — from this function and from the whole module.
    for banned in ("deduct_coins", "add_coins"):
        eq(banned in body, False, f"_place_bid_core still calls {banned}")
    # And nowhere else in the module either — asked of the AST, so a docstring
    # that MENTIONS the old calls in order to say they are gone does not trip it.
    import ast
    banned_names = {"add_coins", "deduct_coins"}
    for node in ast.walk(ast.parse(_cog_source())):
        if isinstance(node, ast.Call):
            fn = node.func
            nm = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
            if nm in banned_names:
                raise AssertionError(f"a live {nm}() call survives at line {node.lineno}")
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in banned_names:
                    raise AssertionError(
                        f"line {node.lineno} re-binds {t.id}; an unbound name is a "
                        f"NameError, which is the guard")


def t_the_sweep_is_wired():
    """The extension guard runs once a minute, before settlement, or it is decoration."""
    loop = _func_body_source("auction_sweep_loop", _cog_source())
    if loop is None:
        raise AssertionError("auction_sweep_loop is not module-level")
    eq("_esc.sweep_hold_extensions" in loop, True, "the guard sweep is called")
    eq(loop.index("_esc.sweep_hold_extensions") < loop.index("get_expired_active_listings"),
       True, "and it runs BEFORE settlement, so a repaired lot settles on this pass")


def _lift_min_next_bid():
    """Run the cog's real `_min_next_bid` / `_coin_amount` with a tiny namespace."""
    import math as _math
    src = _cog_source()
    import typing
    ns = {"math": _math, "Optional": typing.Optional}
    exec("def _num(v, d=0.0):\n"
         "    try:\n"
         "        return float(v)\n"
         "    except (TypeError, ValueError):\n"
         "        return d\n", ns)
    ns["DEF"] = {"min_increment_pct": 5.0, "min_increment_floor": 1000.0}
    exec(_func_source("_coin_amount", src), ns)
    exec(_func_source("_min_next_bid", src), ns)
    return ns["_min_next_bid"]


def t_min_next_bid_is_a_whole_number():
    """A hold amount is an integer by contract, so the minimum has to be one too."""
    f = _lift_min_next_bid()
    got = f({"reserve": 1000.6, "current_bid": None})
    eq(isinstance(got, int), True, f"first bid minimum is an int, got {type(got)}")
    eq(got, 1001, "and it rounds UP, so the seller's floor is always met")
    nxt = f({"reserve": 1000.6, "current_bid": 1000.6, "min_increment_pct": 5.0})
    eq(isinstance(nxt, int), True, "later minimums too")
    eq(nxt, 2001, "1000.6 + max(5%, 1000) floor, rounded up")
    eq(f({"reserve": float("nan"), "current_bid": None}), None,
       "a NaN reserve is refused, not silently accepted")
    eq(f({"reserve": float("inf"), "current_bid": None}), None,
       "and so is inf, which SQLite stores faithfully")
    eq(f({"reserve": 0, "current_bid": None}), None, "and a zero reserve")


# ══════════════════════════════════════════════════════════════════════════


def t_the_two_status_vocabularies_agree():
    """`TERMINAL_LISTING_STATUSES` and `CLOSED_STATUS_REASON` are ONE vocabulary.

    Two hand-maintained lists of the same thing, in two files, with nothing
    binding them. They agree today. The failure they permit is silent and it is
    the expensive direction: add a terminal status to `land_escrow` and forget
    the cog, and `_settle_gate` returns `None` for it — that lot stays bid-on-able
    and settleable after it is over, which is the exact re-settlement class
    `CLOSED_STATUS_REASON`'s own comment block says cost 40,000 minted coins.
    Forget it the other way and a player gets "that isn't active" instead of a
    sentence.

    IT CANNOT BE ASSERTED AT IMPORT in `land_escrow`: the cog imports discord.py,
    and `land_escrow` staying discord-free is what makes every rule in it
    testable against a temp SQLite file. So it is asserted here, off the shipped
    cog's AST — the bytes that deploy, not a copy of them.

    The relation, stated once: CLOSED_STATUS_REASON is TERMINAL plus exactly the
    two live-but-unbiddable statuses `draft` (not open yet) and `settling` (a
    transient claim), both of which the cog's own comments already name as
    non-terminal on purpose.
    """
    import ast
    from land_escrow import TERMINAL_LISTING_STATUSES

    src = _cog_source()
    closed = None
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "CLOSED_STATUS_REASON":
                    closed = node.value
    if closed is None:
        raise AssertionError("CLOSED_STATUS_REASON is not a module-level dict in the "
                             "cog any more — this test's premise is gone, fix the test")
    keys = set()
    for k in closed.keys:
        if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
            raise AssertionError("CLOSED_STATUS_REASON has a non-literal key; this "
                                 "check can no longer read it statically")
        keys.add(k.value)

    NOT_TERMINAL = {"draft", "settling"}
    eq(NOT_TERMINAL <= keys, True,
       "the cog stopped explaining `draft`/`settling`, which are live-but-unbiddable")
    missing = set(TERMINAL_LISTING_STATUSES) - keys
    if missing:
        raise AssertionError(
            f"{sorted(missing)} is terminal in land_escrow and has NO sentence in "
            f"cogs/land_exchange.CLOSED_STATUS_REASON — `_settle_gate` returns None "
            f"for it, so a lot in that status is still bid-on-able and settleable")
    extra = keys - NOT_TERMINAL - set(TERMINAL_LISTING_STATUSES)
    if extra:
        raise AssertionError(
            f"{sorted(extra)} has a 'that listing is closed' sentence in the cog but "
            f"is NOT in land_escrow.TERMINAL_LISTING_STATUSES — the escrow sweeps "
            f"treat it as live and will not retire holds on it. Add it there, or add "
            f"it to this test's NOT_TERMINAL set with the reason it is live")
    # Stated positively too, so the test says what the relation IS and not only
    # what it is not.
    eq(keys - NOT_TERMINAL, set(TERMINAL_LISTING_STATUSES),
       "CLOSED_STATUS_REASON minus {draft, settling} == TERMINAL_LISTING_STATUSES")
    # Every sentence is a real sentence, not a status name echoed back.
    for k in sorted(keys):
        val = [v for kk, v in zip(closed.keys, closed.values) if kk.value == k][0]
        text = " ".join(c.value for c in ast.walk(val)
                        if isinstance(c, ast.Constant) and isinstance(c.value, str))
        eq(len(text.split()) >= 4, True, f"{k}'s refusal is not a sentence: {text!r}")


def t_escrow_unavailable_is_raised_and_becomes_the_paused_sentence():
    """`EscrowUnavailable` is LIVE, and this is the caller check_wiring wanted.

    The review entry said "raised nowhere". It is raised — once, in
    `LedgerV2InProcess._mod()` — and nothing caught it BY NAME, which is what
    made it look dead. Both halves are pinned here so the next reader does not
    have to re-derive which of "delete the class" or "add a raise" is right:
    neither. The `deduct_coins` fallback it exists instead of is gone from the
    cog entirely (asserted above), the raise is real, and `available()` is the
    thing that turns it into "the exchange is briefly paused".
    """
    import ast
    import land_escrow as esc

    eq(issubclass(esc.EscrowUnavailable, RuntimeError), True,
       "it must stay catchable as a RuntimeError by the generic handlers")

    # 0. THE RAISE SITE ITSELF, off the shipped source. The behavioural half
    # below drives a subclass that raises it on purpose, so on its own it would
    # still pass if `_mod` were changed to raise a bare RuntimeError — and a
    # bare RuntimeError is indistinguishable from "core answered badly", which
    # is the one thing this class exists to keep separate from "core was never
    # reached". Read the real `_mod` instead of trusting the substitute.
    esc_src = (ROOT / "land_escrow.py").read_text(encoding="utf-8")
    mod_fn = None
    for node in ast.walk(ast.parse(esc_src)):
        if isinstance(node, ast.ClassDef) and node.name == "LedgerV2InProcess":
            for sub in node.body:
                if isinstance(sub, ast.FunctionDef) and sub.name == "_mod":
                    mod_fn = sub
    if mod_fn is None:
        raise AssertionError("LedgerV2InProcess._mod is gone — EscrowUnavailable's "
                             "only raise site went with it; delete the class or "
                             "restore the raise, do not leave it dangling")
    raises = [n for n in ast.walk(mod_fn)
              if isinstance(n, ast.Raise) and n.exc is not None
              and isinstance(n.exc, ast.Call)
              and getattr(n.exc.func, "id", getattr(n.exc.func, "attr", ""))
              == "EscrowUnavailable"]
    eq(len(raises) >= 1, True,
       "LedgerV2InProcess._mod no longer raises EscrowUnavailable — an unimportable "
       "ledger now looks like a ledger that answered, which is the distinction the "
       "class exists to make")
    eq(any(isinstance(n, ast.ExceptHandler) for n in ast.walk(mod_fn)), True,
       "the raise is not guarding an import failure any more")

    class NoLedger(esc.LedgerV2InProcess):
        def _mod(self):
            raise esc.EscrowUnavailable("ledger_v2 is not importable: test")

    broken = NoLedger()
    # 1. `_call` must not proceed against a ledger it could not import.
    raised = False
    try:
        broken.hold("1", 1, "test", 60, "k")
    except esc.EscrowUnavailable:
        raised = True
    except Exception as e:                      # noqa: BLE001
        raise AssertionError(f"a missing ledger raised {type(e).__name__}, not "
                             f"EscrowUnavailable: {e}")
    eq(raised, True, "a money call against an unimportable ledger must raise")
    # 2. `available()` swallows it into False rather than propagating.
    eq(broken.available(), False, "available() must answer the question, not raise it")
    # 3. ...which is what every money path reads, and what makes it a sentence.
    prev = esc.set_ledger(broken)
    try:
        eq(esc.escrow_available(), False, "escrow_available() reports the outage")
        import land_settle as st
        sentence = st.paused_sentence()
        eq(len(sentence.split()) >= 6 and "pause" in sentence.lower(), True,
           f"the player-facing outage sentence is missing or unhelpful: {sentence!r}")
    finally:
        esc.set_ledger(prev)

    # 4. AND THE LIST OF GATES IN `EscrowUnavailable`'s DOCSTRING IS COUNTED, not
    #    trusted. That paragraph used to carry two line numbers and the word
    #    "five"; by the next round the lines had drifted ~30 and the count was
    #    one too many. Nothing caught it, because `check_docstrings` reads claims
    #    and a line number is not a claim it can evaluate. So the names went into
    #    the docstring and the arithmetic came here: if somebody adds a seventh
    #    money path, or deletes one, this fails and the prose gets updated in the
    #    same commit.
    import ast
    expected = {
        ROOT / "cogs" / "land_exchange.py": {"_place_bid_core", "_instant_buy_core"},
        ROOT / "land_settle.py": {"settle_listing", "cancel_listing",
                                  "charge_listing_fee", "charge_rent"},
    }
    doc = esc.EscrowUnavailable.__doc__ or ""
    found_total = 0
    for path, want in expected.items():
        src = path.read_text(encoding="utf-8")
        found = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name != "paused_sentence" \
                    and "paused_sentence()" in (ast.get_source_segment(src, node) or ""):
                found.add(node.name)
        eq(found, want,
           f"{path.name}: the functions that refuse with paused_sentence() are "
           f"{sorted(found)}, but EscrowUnavailable's docstring names {sorted(want)}")
        found_total += len(found)
    eq(found_total, 6, "the gate count changed")
    for name in ("_place_bid_core", "_instant_buy_core", "settle_listing",
                 "cancel_listing", "charge_listing_fee", "charge_rent"):
        eq(name in doc, True,
           f"EscrowUnavailable's docstring no longer names the {name} gate")
    eq("five gates" in doc, False,
       "EscrowUnavailable's docstring still says 'five gates' — there are six")


def t_the_deferred_key_reaches_a_human():
    """`release_all_holds`' `deferred` has a reader now, all the way to the reply.

    It used to stop at its own return statement. A manager cancelling a lot whose
    escrow row had an unconfirmed placement was told `released: []` — which reads
    as "this lot has no escrow left" — while that row was still reserving a
    bidder's coins. The rows are genuinely undeferrable on that path (no hold id
    to name, and re-sending the placement key on a user-facing click can RESERVE
    coins), so the fix is not to release them here; it is to say so.

    Asserted as a CHAIN, because any one hop dropping the key restores the
    original defect silently.
    """
    import ast

    # 1. The producer still produces it.
    eq("deferred" in _func_source("release_all_holds", (ROOT / "land_settle.py")
                                  .read_text(encoding="utf-8")), True,
       "release_all_holds no longer returns `deferred`")

    def _returns_deferred(src_text, fn_name):
        for node in ast.walk(ast.parse(src_text)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and node.name == fn_name:
                seg = ast.get_source_segment(src_text, node) or ""
                return '"deferred"' in seg
        raise AssertionError(f"{fn_name} is gone")

    settle_src = (ROOT / "land_settle.py").read_text(encoding="utf-8")
    escrow_src = (ROOT / "land_escrow.py").read_text(encoding="utf-8")
    cog_src = _cog_source()
    for src_text, fn in ((settle_src, "cancel_listing"),
                         (settle_src, "expire_unsold"),
                         (escrow_src, "retire_listing_escrow"),
                         (cog_src, "cancel_listing_core")):
        eq(_returns_deferred(src_text, fn), True,
           f"{fn} drops `deferred` — an unresolved reservation goes silent there")

    # 2. It ends at a human: the /realestate cancel reply mentions it.
    reply = None
    for node in ast.walk(ast.parse(cog_src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "cancel":
            seg = ast.get_source_segment(cog_src, node) or ""
            if "cancel_listing_core" in seg:
                reply = seg
    if reply is None:
        raise AssertionError("/realestate cancel no longer calls cancel_listing_core")
    eq('res.get("deferred")' in reply, True,
       "the cancel reply does not read `deferred`, so `released: []` is still "
       "shown to a manager as 'this lot is clean'")
    eq("reserved" in reply and "sweep" in reply, True,
       "the reply reads `deferred` but does not tell the manager what it means "
       "or that the sweep will finish it")

    # 3. AND THE CLOSE PATH, which is the half that was still open. `close` is
    #    checked separately from `cancel` because for a round the chain was
    #    closed on one and open on the other, and only mutation found it:
    #    deleting the cancel reader failed this file, deleting the producer in
    #    `close_listing_core` failed nothing at all.
    close_body = None
    for node in ast.walk(ast.parse(cog_src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "close":
            seg = ast.get_source_segment(cog_src, node) or ""
            if "close_listing_core" in seg:
                close_body = seg
    if close_body is None:
        raise AssertionError("/realestate close no longer calls close_listing_core")

    close_code = _func_source("close", _cog_source_nocomments()) or ""
    eq("#" not in close_code or "only mutation found it" not in close_code, True,
       "comment stripping is not working, so these checks can pass on prose")

    eq(_returns_deferred(cog_src, "close_listing_core"), True,
       "close_listing_core drops `deferred`, so the close reply below cannot "
       "know a reservation is still open")
    eq('res.get("deferred")' in close_code, True,
       "the /realestate close handler does not read `deferred` — it therefore "
       "posts 'the standing bidder's coins are no longer reserved' into a public "
       "channel over a lot whose escrow row may still be reserving them")
    eq("still reserved" in close_code and "sweep" in close_code, True,
       "the close handler reads `deferred` but does not say what is true: that "
       "those coins are still reserved and the escrow sweep ends them")
    # ...and it must still be a CONDITION, not a deletion. The reassuring
    # sentence is correct on a clean close and a manager needs it.
    eq("no longer reserved" in close_code, True,
       "the close handler no longer tells a manager the reservation ended even "
       "when it did — the fix for the false sentence is to branch on `deferred`, "
       "not to stop saying the true thing")


def t_the_pause_copy_is_true_for_the_reader_whose_coins_moved():
    """The freeze banner and the pause refusal, checked against all three states.

    Both sentences used to open "no coins have moved". That is true for a reader
    whose coins are RESERVED — a hold leaves them in the wallet — and false for a
    reader whose instant-buy was interrupted after the capture landed, whose
    coins are in `treasury:estates`. Right for 99% of readers, wrong for exactly
    the one with money at stake, and a ledger incident is both why the switch
    gets thrown and why captures get interrupted, so the two co-occur.

    Asserted as a PAIR of requirements, in both sentences, because either one
    alone is passable by a bad fix: truthfulness alone is satisfied by deleting
    the reassurance (a worse product), and reassurance alone is what was already
    there. The behavioural half — that the promised "it finishes on its own" is
    real, even while frozen — is `probe_gate_final` G1.3 and `probe_ungate` U5.5.
    """
    import land_settle as st

    # The cog imports discord.py at module scope and cannot be imported here, so
    # `freeze_notice` is LIFTED by AST span and executed — the shipped bytes, not
    # a transcription, and a real call rather than a substring hunt through
    # source. Passing `state` explicitly means the lifted function never reaches
    # `freeze_state`, which is the only name in it this namespace lacks.
    from typing import Optional  # noqa: F401 — the lifted signature annotates with it
    ns = {"Optional": Optional}
    exec(_func_source("freeze_notice"), ns)          # noqa: S102
    freeze_notice = ns["freeze_notice"]

    banner = freeze_notice(state={"frozen": True, "reason": "ledger maintenance"})
    refusal = st.paused_sentence()
    eq(freeze_notice(state={"frozen": False}), "",
       "an open board must show no banner at all")

    for label, text in (("freeze banner", banner), ("pause refusal", refusal)):
        low = text.lower()
        eq("no coins have moved" in low or "no coins moved" in low, False,
           f"the {label} still makes the flat claim that no coins have moved: "
           f"{text!r} — it is false for a buyer mid-capture")
        eq("reserv" in low, True,
           f"the {label} no longer tells a bidder their reserved coins are still "
           f"theirs: {text!r} — the reassurance was deleted rather than corrected")
        eq("escrow" in low, True,
           f"the {label} does not name the IN-FLIGHT state at all: {text!r} — "
           f"held, available and in-flight are three different things")
        eq("lost" in low, True,
           f"the {label} no longer says nothing is lost: {text!r}")
    eq("ledger maintenance" in banner, True,
       "the banner dropped the reason a manager typed into the kill switch")


def t_a_failed_instant_buy_describes_the_row_it_actually_left():
    """The sentence for a settlement that RAISED is chosen from the row, not fixed.

    The capture and the seller transfer are two separate ledger calls, so the
    `except` around `_finalize_sale_core` catches both "nothing happened" and
    "the capture landed and the transfer died". In the second, the buyer's coins
    are in `treasury:estates`, `held` is zero and no reservation exists — yet the
    handler used to answer, always:

        "Nothing has been taken from you — your coins were only reserved, and
         the reservation ends automatically. Try again in a moment."

    Three false clauses and one dangerous instruction, in the state this module
    is mostly about. The behaviour is measured in `adv/probe_copy_r5.py` §K5;
    what is pinned here is the SHAPE, which the behaviour test cannot see: the
    handler must not carry a player sentence at all, because a sentence written
    before the row is re-read is a sentence written before the fact is known.
    """
    import ast

    nc = _cog_source_nocomments()
    body = _func_body_source("_instant_buy_core", nc)
    import textwrap
    tree = ast.parse(textwrap.dedent(_func_source("_instant_buy_core", nc)))
    handler = None
    for node in ast.walk(tree):
        # the try whose BODY calls the settlement — not the one around the hold
        if isinstance(node, ast.Try) and any(
                "_finalize_sale_core" in ast.unparse(n) for n in node.body):
            handler = node
    if handler is None:
        raise AssertionError("_instant_buy_core no longer calls _finalize_sale_core "
                             "inside a try — this test cannot see the branch it pins")

    excerpt = "\n".join(ast.unparse(h) for h in handler.handlers)
    prose = [s for s in _string_constants(handler.handlers) if len(s) > 40]
    eq(prose, [],
       f"the except around _finalize_sale_core still writes a fixed player "
       f"sentence: {prose!r}. It runs before the row is re-read, so it cannot "
       f"know whether the capture landed — choose the sentence after "
       f"`_esc.bid_row(row_id)` instead")
    eq("settle_raised" in excerpt, True,
       "the except no longer flags the raise for the later sentence choice")

    # ...and the later choice really does distinguish the three states.
    for code in ("settle_failed_released", "settle_failed_reserved",
                 "settle_failed_in_escrow"):
        eq(code in body, True,
           f"_instant_buy_core no longer distinguishes `{code}` — a settlement "
           f"that raised over a captured row is being described as a released "
           f"reservation again")
    eq(body.index("_esc.bid_row(row_id)") < body.index("settle_failed_in_escrow"), True,
       "the sentence is chosen before the row is re-read")

    # The in-escrow sentence must not invite a retry: that is how a second lot of
    # a buyer's coins gets reserved against a purchase they have already paid for.
    tail = body[body.index("settle_failed_in_escrow"):]
    tail = tail[:tail.index("if not res.get(\"ok\")")] if "if not res.get(\"ok\")" in tail else tail
    eq("try again" in tail.lower(), False,
       "the in-escrow branch tells a buyer whose coins are already in the "
       "treasury to try again")

    # And the fail-closed fallback asserts no negative about money it cannot see.
    fallback = _assign_source("NON_SALE_FALLBACK", nc) or ""
    low = fallback.lower()
    eq("nothing has been taken from you" in low, False,
       "NON_SALE_FALLBACK fires on an outcome nobody taught this function, so it "
       "cannot know a capture did not happen — it must not say so")
    eq("only reserved" in low, False,
       "NON_SALE_FALLBACK still claims the coins were only reserved")


def t_the_outbid_surfaces_report_the_release_rather_than_assume_it():
    """"Their reservation is released" is a measurement now, not a hope.

    `_place_bid_core` released each displaced row and discarded `release_row`'s
    answer, while `prev_bidder` came back regardless. Both outbid surfaces then
    asserted the good case: a DM saying the coins "never left your balance and
    are spendable again", and a PUBLIC channel note saying "their `N` 🪙
    reservation is released" — over a `release_unknown` row with the coins still
    held. The displaced bidder comes back to bid and is refused for funds they
    were told they had.

    Behaviour is `adv/probe_copy_r5.py` §K6. What is pinned here is the WIRING,
    which the behaviour test sees only one end of: the core must keep the answer,
    and both surfaces must branch on it rather than on `prev_bidder` alone.
    """
    # Every span below goes through `_code_only` — see its docstring. The
    # comments in these very functions name `prev_released` and quote both
    # sentences, so a raw substring check here would pass on the explanation
    # after a mutation deleted the code. That is not hypothetical: it hid a
    # mutation of `_post_bid` while this test was being written.
    nc = _cog_source_nocomments()
    core = _func_body_source("_place_bid_core", nc)
    eq('_settle.release_row(displaced' in core and "prev_released" in core, True,
       "_place_bid_core no longer records what release_row answered for the "
       "displaced rows — every outbid sentence downstream is then a guess")
    eq('"prev_released": prev_released' in core, True,
       "_place_bid_core computes prev_released but does not return it")
    # the release must be judged against `released` specifically — `releasing`,
    # `release_unknown` and `release_refused` all mean the coins are still held.
    eq('!= "released"' in core or '== "released"' in core, True,
       "prev_released is not compared against the `released` status, so a row "
       "still in flight counts as freed")

    note = _func_body_source("_bid_note", nc)
    eq("prev_released" in note, True,
       "the public bid note still announces a release it did not check — this is "
       "the most-read sentence the exchange writes")
    eq("still reserved" in note, True,
       "the bid note reads prev_released but has no sentence for the case where "
       "the release did not land")
    eq("reservation is released" in note, True,
       "the bid note stopped saying the true thing in the ordinary case — the "
       "fix is a condition, not a deletion")

    dm = _func_source("_dm_outbid", nc) or ""
    eq("released=True" in dm, True,
       "_dm_outbid no longer takes the release outcome, so it cannot tell a "
       "bidder the truth about their own coins")
    eq("still reserved" in dm, True,
       "_dm_outbid has no sentence for a release that did not land")
    eq("spendable again" in dm, True,
       "_dm_outbid lost the reassuring sentence for the ordinary case")

    post = _func_source("_post_bid", nc) or ""
    eq("prev_released" in post, True,
       "_post_bid calls _dm_outbid without passing the release outcome, so the "
       "DM silently falls back to claiming success")


def _string_constants(nodes):
    """Every string literal under `nodes` that a PLAYER could read.

    Log format strings are excluded — an operator line in the container log is
    not a sentence sent to anybody, and the point of the check this feeds is that
    the branch must not compose a REPLY before it has re-read the row. Docstrings
    are excluded for the same reason.
    """
    import ast
    logged = set()
    out = []
    for n in nodes:
        for sub in ast.walk(n):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute) \
                    and isinstance(sub.func.value, ast.Name) and sub.func.value.id == "log":
                for arg in ast.walk(sub):
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        logged.add(id(arg))
        for sub in ast.walk(n):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                    and id(sub) not in logged:
                out.append(sub.value)
    return out


def t_instant_buy_refuses_a_lot_past_its_deadline():
    """The deadline guard, read off the shipped cog, ABOVE the first money line.

    The behaviour is proved in `tests/test_land_settle.py`; what is proved here
    is the POSITION, which the behaviour test cannot see: a guard that returns
    `ok: False` after `create_bid_row`/`hold` has still reserved the buyer's
    coins, and an unbounded reservation on a wedged lot is the whole finding.
    """
    body = _func_body_source("_instant_buy_core")
    for needle in ('listing.get("ends_at")', "_epoch(", "listing_ended"):
        eq(needle in body, True, f"the deadline guard is gone from _instant_buy_core "
                                 f"({needle!r} is missing)")
    eq(body.index("listing_ended") < body.index("_esc.create_bid_row"), True,
       "the deadline guard runs AFTER a bid row is created — the refusal is free "
       "only if nothing has been written or reserved when it fires")
    eq(body.index("listing_ended") < body.index("_esc.ledger().hold"), True,
       "the deadline guard runs AFTER the hold — the coins are already reserved")
    # And the auction path it was copied from still has its own.
    bid_body = _func_body_source("_place_bid_core")
    eq('listing.get("ends_at")' in bid_body, True,
       "_place_bid_core lost the guard _instant_buy_core was given")


def _settle_source():
    return (ROOT / "land_settle.py").read_text(encoding="utf-8")


def _ok_true_outcomes_of(func_name, src):
    """Every `outcome` string a function can return alongside `ok: True`.

    Read off the AST rather than the docstring, because the docstring is the
    thing that was already wrong. Walks the named function AND anything it
    `return`s through by name in the same module (`settle_listing` returns
    `_settle_claimed(...)` and `_claim_refused(...)`, and two of the four
    outcomes live in those), then collects, from every `return {...}` whose
    `"ok"` key is the constant True, the value of its `"outcome"` key. A `Name`
    value (`ALREADY_CLOSED`) is resolved against the module's own top-level
    assignments, so renaming the constant cannot make the outcome disappear.
    """
    import ast
    tree = ast.parse(src)
    consts, funcs = {}, {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    consts[t.id] = node.value.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.value, ast.Constant):
            if isinstance(node.target, ast.Name):
                consts[node.target.id] = node.value.value
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs[node.name] = node

    seen, todo, out = set(), [func_name], set()
    while todo:
        name = todo.pop()
        if name in seen or name not in funcs:
            continue
        seen.add(name)
        for node in ast.walk(funcs[name]):
            if not isinstance(node, ast.Return) or node.value is None:
                continue
            val = node.value
            # `return _settle_claimed(...)` — follow it.
            if isinstance(val, ast.Call) and isinstance(val.func, ast.Name):
                todo.append(val.func.id)
                continue
            if not isinstance(val, ast.Dict):
                continue
            pairs = {k.value: v for k, v in zip(val.keys, val.values)
                     if isinstance(k, ast.Constant)}
            ok = pairs.get("ok")
            if not (isinstance(ok, ast.Constant) and ok.value is True):
                continue
            oc = pairs.get("outcome")
            if isinstance(oc, ast.Constant):
                out.add(oc.value)
            elif isinstance(oc, ast.Name) and oc.id in consts:
                out.add(consts[oc.id])
            elif oc is not None:
                raise AssertionError(
                    f"{name} returns ok:True with an `outcome` this test cannot "
                    f"read statically (line {node.lineno}) — it must be a literal "
                    f"or a module constant, or the contract below cannot be checked")
    return out


def t_instant_buy_handles_every_ok_true_settle_outcome():
    """THE INSTANT-BUY RETURN CONTRACT, made enforceable instead of remembered.

    `_instant_buy_core` returns either a completed purchase carrying a `price` or
    a refusal. It used to return `settle_listing`'s dict verbatim for anything
    that was not `sold`, and three of `settle_listing`'s outcomes are `ok: True`
    with no `price` in them. Its three callers all branch on `ok` alone and all
    three then read a price: the Buy button and `/realestate buy` raised
    `KeyError: 'price'` with the interaction already deferred, so the click died
    with no reply at all, and the satellite announced a plot "bought via the
    network for `0` 🪙" in a partner channel and opened a deal room between the
    seller and someone who had not bought it.

    The defect was not a missing check — a guard was added for the past-deadline
    instance of it and the class survived, because the class is a CONTRACT. So
    this reads the set of `ok: True` outcomes off `settle_listing`'s own AST and
    asserts the cog names every one of them. Add a fifth outcome upstream and
    this test says so; nobody has to remember.
    """
    outcomes = _ok_true_outcomes_of("settle_listing", _settle_source())
    eq("sold" in outcomes, True,
       "settle_listing no longer has a `sold` outcome — this test is reading the "
       "wrong function")
    eq(len(outcomes) >= 4, True,
       f"expected settle_listing's four ok:True outcomes, found {sorted(outcomes)}")

    cog_src = _cog_source()
    table = _assign_source("NON_SALE_OUTCOMES", cog_src)
    body = _func_body_source("_instant_buy_core", cog_src)
    for outcome in sorted(outcomes - {"sold"}):
        eq(f'"{outcome}"' in table or outcome == "already_closed", True,
           f"`settle_listing` can return ok:True with outcome `{outcome}` and "
           f"NON_SALE_OUTCOMES has no sentence for it — a buyer would be told a "
           f"settlement result is a purchase, and the callers dereference "
           f"res['price']")
    # `already_closed` is spelled as the shared constant on both sides, which is
    # the point of the constant; assert that binding rather than the literal.
    if "already_closed" in outcomes:
        eq("_settle.ALREADY_CLOSED" in table, True,
           "NON_SALE_OUTCOMES lost its `already_closed` key")
    # The `sold` branch is the ONLY one that may return the settle dict, and it
    # is the only one that puts a `price` in it.
    eq('res["price"] = float(price_i)' in body, True,
       "the sold branch no longer sets the `price` its three callers read")
    eq("NON_SALE_OUTCOMES" in body, True,
       "_instant_buy_core no longer consults NON_SALE_OUTCOMES — it is passing "
       "settle_listing's dict back to callers that dereference res['price']")
    eq(body.rindex('res.get("outcome") == "sold"') < body.rindex("NON_SALE_OUTCOMES"), True,
       "the contract translation must run AFTER the sold branch, or a real sale "
       "is refused")
    # And the fallback exists, so an outcome nobody has taught it yet fails
    # CLOSED — a refusal — rather than reaching a caller as a purchase.
    eq("NON_SALE_FALLBACK" in body, True,
       "there is no fallback: an unhandled ok:True outcome would raise KeyError "
       "inside the buy path instead of refusing")
    fallback = _assign_source("NON_SALE_FALLBACK", cog_src)
    eq('"ok": True' in fallback or "'ok': True" in fallback, False,
       "the fallback must be a refusal")


def main():
    real_ledger_tests = [
        ("a bid places a HOLD and not a debit", t_bid_holds_not_debits),
        ("being outbid releases exactly that hold", t_outbid_releases_that_hold),
        ("200 outbid cycles conserve every balance", t_outbid_cycle_conserves_exactly),
        ("one coin cannot back two bids", t_one_coin_one_bid),
        ("the availability test is the write", t_partial_availability_is_the_write),
        ("crash before the hold leaves no phantom bid",
         t_crash_before_hold_leaves_no_phantom_bid),
        ("a replayed placement does not double-reserve",
         t_replay_of_a_landed_hold_does_not_double_reserve),
        ("a lot extended to the cap still has live holds",
         t_extension_tracks_the_lot_to_the_cap),
        ("the guard sweep repairs a missed extension",
         t_guard_sweep_repairs_a_missed_extension),
        ("instant buy: hold, capture, pay the seller",
         t_instant_buy_hold_then_capture_then_seller),
        ("an aborted buy releases rather than refunds",
         t_aborted_buy_releases_rather_than_refunds),
    ]
    fake_ledger_tests = [
        ("hold refused -> failed, with figures", t_hold_refused_is_failed_with_figures),
        ("hold unknown -> never failed", t_hold_unknown_is_never_failed),
        ("release refused / unknown", t_release_refused_and_unknown),
        ("capture refused / unknown", t_capture_refused_and_unknown),
        ("reconcile never moves a row backwards", t_reconcile_never_moves_a_row_backwards),
        ("keys are stable across re-reads", t_keys_are_stable_across_rereads),
        ("the board never goes backwards", t_promote_refuses_a_lower_bid),
        ("legacy rows are untouchable", t_legacy_rows_are_untouchable),
        ("capture refuses a row with no stored integer",
         t_capture_refuses_a_row_with_no_stored_integer),
    ]
    wiring_tests = [
        ("_place_bid_core really places a hold, in order",
         t_the_cog_actually_calls_this_sequence),
        ("the extension guard sweep is wired into the minute loop", t_the_sweep_is_wired),
        ("_min_next_bid returns a whole number of coins",
         t_min_next_bid_is_a_whole_number),
        ("the two terminal-status vocabularies are one vocabulary",
         t_the_two_status_vocabularies_agree),
        ("EscrowUnavailable is raised, and becomes the paused sentence",
         t_escrow_unavailable_is_raised_and_becomes_the_paused_sentence),
        ("release_all_holds' `deferred` key reaches a human, on BOTH the cancel "
         "and the close path",
         t_the_deferred_key_reaches_a_human),
        ("the pause copy is true for the reader whose coins actually moved",
         t_the_pause_copy_is_true_for_the_reader_whose_coins_moved),
        ("_code_only really strips comments — six checks depend on it",
         t_code_only_actually_strips_comments),
        ("_instant_buy_core refuses a lot past its deadline, before it reserves",
         t_instant_buy_refuses_a_lot_past_its_deadline),
        ("a failed instant buy describes the row it actually left",
         t_a_failed_instant_buy_describes_the_row_it_actually_left),
        ("the outbid DM and public note report the release, not assume it",
         t_the_outbid_surfaces_report_the_release_rather_than_assume_it),
        ("_instant_buy_core names every ok:True outcome settle_listing can return",
         t_instant_buy_handles_every_ok_true_settle_outcome),
    ]
    print("\nREAL ledger_v2, real escrow triggers, temp restocker.db")
    for name, fn in real_ledger_tests:
        check(name, fn)
    print("\nFAKE ledger — the failures a healthy ledger cannot be made to produce")
    for name, fn in fake_ledger_tests:
        check(name, fn)
    print("\nWIRING — read off the shipped cogs/land_exchange.py, not a copy")
    for name, fn in wiring_tests:
        check(name, fn)
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
